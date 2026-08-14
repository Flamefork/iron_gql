import importlib
import json
import pickle
import subprocess
import sys
from collections.abc import Callable
from typing import Any
from typing import cast
from typing import override

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.runtime import FileVar
from iron_gql.slots import GQLBindableFragment
from iron_gql.slots import GQLFragment
from tests.conftest import UPLOAD_SCALARS
from tests.conftest import GraphQLRequest
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server
from tests.conftest import make_subscription_app
from tests.conftest import read_type_erased
from tests.conftest import use_package_client

# Shared by every fixture below: a nullable union field so a slot
# discriminates by runtime __typename between two disjoint concrete types.
ATTACHMENT_UNION = """
union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""

# --- tuple binding with two disjoint fragments ---------------------

DISJOINT_SCHEMA = f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
}}

{ATTACHMENT_UNION}
"""

generated_package(
    "bindings_disjoint",
    schema=DISJOINT_SCHEMA,
    queries='''
    from tests.generated.bindings_disjoint.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url
        }
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {
            href
        }
        """
    )

    other_parts = api_gql(
        """
        fragment OtherParts on ImageAttachment {
            url
        }
        """
    )

    both = get_attachment.bind(attachment=(image_parts, link_parts))
    foreign = get_attachment.bind(attachment=other_parts)
    ''',
)

from tests.generated.bindings_disjoint import queries as disjoint_queries
from tests.generated.bindings_disjoint.gql.api import ImagePartsData
from tests.generated.bindings_disjoint.gql.api import LinkPartsData


def _resolve_disjoint_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    if id == "img":
        return {"id": id, "attachment": {"__typename": "ImageAttachment", "url": "u1"}}
    return {"id": id, "attachment": {"__typename": "LinkAttachment", "href": "h1"}}


async def test_disjoint_tuple_binding_reads_each_slice_and_rejects_foreign_definition(
    httpserver: HTTPServer,
):
    async with gql_server(
        httpserver, "bindings_disjoint", {"Query": {"post": _resolve_disjoint_post}}
    ):
        image_result = await disjoint_queries.both.execute(id="img")
        link_result = await disjoint_queries.both.execute(id="link")
        assert image_result.post is not None
        assert link_result.post is not None
        image_node = image_result.post.attachment
        link_node = link_result.post.attachment

        # Каждый definition читает свою projection напрямую.
        image = disjoint_queries.image_parts.read(image_node)
        link = disjoint_queries.link_parts.read(link_node)
        assert isinstance(image, ImagePartsData)
        assert isinstance(link, LinkPartsData)
        assert image.url == "u1"
        assert link.href == "h1"
        # Предложенный definition возвращает None для typename вне coverage;
        # definition, которого в этом slot не было, остаётся wiring bug.
        assert disjoint_queries.image_parts.read(link_node) is None
        assert disjoint_queries.link_parts.read(image_node) is None

        # `other_parts` was bound to a *different* binding of the same
        # template (`foreign`), so it was never offered to `both`'s slot
        # validation -- reading it against `both`'s own result is a wiring
        # bug, not a soft None.
        with pytest.raises(
            ValueError,
            match="is not part of the binding that produced slot 'attachment'",
        ):
            read_type_erased(disjoint_queries.other_parts, image_node)


async def test_a_second_definition_value_reads_the_bound_fragment(
    httpserver: HTTPServer,
):
    # Slot data is indexed by the generated definition class. A fresh value of
    # that class therefore reads the same projection; object identity and the
    # particular value returned by the original `api_gql()` call are irrelevant.
    async with gql_server(
        httpserver, "bindings_disjoint", {"Query": {"post": _resolve_disjoint_post}}
    ):
        result = await disjoint_queries.both.execute(id="img")
        assert result.post is not None
        node = result.post.attachment

        image = disjoint_queries.image_parts.read(node)
        assert image is not None
        assert image.url == "u1"

        twin = type(disjoint_queries.image_parts)()
        twin_image = twin.read(node)
        assert twin_image is not None
        assert twin_image.url == "u1"


# --- tuple binding with two overlapping fragments --------------------

OVERLAP_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    caption: String!
    width: Int!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_overlap",
    schema=OVERLAP_SCHEMA,
    queries='''
    from tests.generated.bindings_overlap.gql.api import api_gql

    image_caption = api_gql(
        """
        fragment ImageCaption on ImageAttachment {
            caption
        }
        """
    )

    image_size = api_gql(
        """
        fragment ImageSize on ImageAttachment {
            width
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    # Two fragments covering the SAME runtime type in one slot: each reads its
    # own slice independently, so overlap is legal (see the slot-read spec).
    both = get_attachment.bind(attachment=(image_caption, image_size))
    ''',
)

from tests.generated.bindings_overlap import queries as overlap_queries


def _resolve_overlap_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {
        "id": id,
        "attachment": {
            "__typename": "ImageAttachment",
            "caption": "sunset",
            "width": 800,
        },
    }


async def test_overlapping_fragments_in_one_slot_each_read_their_slice(
    httpserver: HTTPServer,
):
    # ImageCaption and ImageSize both cover ImageAttachment: each definition reads
    # its own slice of the same node independently, so overlapping coverage
    # in one slot is legal.
    async with gql_server(
        httpserver, "bindings_overlap", {"Query": {"post": _resolve_overlap_post}}
    ):
        result = await overlap_queries.both.execute(id="p-1")
        assert result.post is not None
        node = result.post.attachment

        caption = overlap_queries.image_caption.read(node)
        size = overlap_queries.image_size.read(node)
        assert caption is not None
        assert size is not None
        assert caption.caption == "sunset"
        assert size.width == 800


CONFLICTING_FIELDS_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    thumbnail(size: Int!): String!
}

type LinkAttachment {
    href: String!
}
"""


def test_overlapping_fragments_with_conflicting_fields_are_rejected(
    test_project: ProjectBuilder,
):
    # Overlap itself is legal (see the test above); what stays rejected is a
    # field-merge conflict in the expanded operation -- here, two fragments
    # directly bound to the same slot select `thumbnail` with different
    # arguments. Caught by `graphql.validate` on the expanded document, not
    # by any check bindings.py runs itself.
    test_project.prepare(
        schema=CONFLICTING_FIELDS_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        thumb_small = api_gql(
            '''
            fragment ThumbSmall on ImageAttachment {
                thumbnail(size: 100)
            }
            '''
        )

        thumb_large = api_gql(
            '''
            fragment ThumbLarge on ImageAttachment {
                thumbnail(size: 200)
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    attachment @slot { __typename }
                }
            }
            '''
        )

        both = get_attachment.bind(attachment=(thumb_small, thumb_large))
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Fields 'thumbnail' conflict"):
        test_project.generate()


# --- composition -- a bound fragment spreading another fragment ----

COMPOSITION_SCHEMA = f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
}}

{ATTACHMENT_UNION}
"""

generated_package(
    "bindings_composition",
    schema=COMPOSITION_SCHEMA,
    queries='''
    from tests.generated.bindings_composition.gql.api import api_gql

    base_parts = api_gql(
        """
        fragment BaseParts on ImageAttachment {
            url
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            ...BaseParts
        }
        """
    )

    foreign_parts = api_gql(
        """
        fragment ForeignParts on ImageAttachment {
            url
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    bound = get_attachment.bind(attachment=image_parts)
    # `foreign_parts` is a typed definition like every fragment of a package
    # with a template, and its own combination is enumerated whether or not
    # anybody writes it -- so it is foreign to `bound`'s closure while still
    # being a real definition, which is what "outside this binding's closure"
    # (rather than "outside every binding") needs.
    ''',
)

from tests.generated.bindings_composition import queries as composition_queries
from tests.generated.bindings_composition.gql import api as composition_api


def _resolve_composition_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {"id": id, "attachment": {"__typename": "ImageAttachment", "url": "u1"}}


async def test_composed_fragment_inner_definition_reads_its_own_slice(
    httpserver: HTTPServer,
):
    # `ImageParts` spreads `BaseParts` at its own root level, so `BaseParts`
    # is reachable only through that spread -- and its fields still land on
    # the slot's root payload, which is what makes it independently readable
    # through its own definition. That is the whole point of keeping a read layer
    # instead of exact per-binding models: the outer, bound fragment and the
    # inner, merged-in one both read their own model from the same node,
    # through their own definitions.
    async with gql_server(
        httpserver,
        "bindings_composition",
        {"Query": {"post": _resolve_composition_post}},
    ):
        result = await composition_queries.bound.execute(id="1")
        assert result.post is not None
        node = result.post.attachment

        outer = composition_queries.image_parts.read(node)
        assert outer is not None
        assert outer.url == "u1"

        # The inner brick reads its own slice directly through its own
        # definition, with real field data -- not merely merged into the outer
        # fragment's model.
        inner = composition_queries.base_parts.read(node)
        assert inner is not None
        assert inner.url == "u1"

        # Стирание типа definition убирает только static check: runtime
        # продолжает находить projection по generated definition class.
        erased: GQLFragment[pydantic.BaseModel, Any] = composition_queries.image_parts
        erased_read = read_type_erased(erased, node)
        assert isinstance(erased_read, composition_api.ImagePartsData)
        assert erased_read.url == "u1"

        # `foreign_parts` has a combination of its own (enumerated, a
        # *different* combination of this same template) but never spread by
        # anything reachable from `bound`'s own closure -- reading it against
        # `bound`'s result is still a wiring bug, not a soft None: the
        # closure rule has a boundary. Erasure is not what makes this raise:
        # the read just above went through the same seam and answered with
        # data.
        with pytest.raises(
            ValueError,
            match="is not part of the binding that produced slot 'attachment'",
        ):
            read_type_erased(composition_queries.foreign_parts, node)


def test_composed_fragment_definition_reaches_the_exec_source():
    # The positive half of composition: the closure fragment's own definition
    # text is present in what actually gets sent to the server, so the spread
    # `...BaseParts` the merged-read test above depends on is valid GraphQL.
    # `exec_source` is an instance attribute now (`bound__` fills it in at
    # `bind()` time), not a per-combination `ClassVar`.
    exec_source = composition_queries.bound.exec_source
    assert "...BaseParts" in exec_source
    assert "fragment BaseParts on ImageAttachment" in exec_source


def test_the_bind_constructs_a_reader_from_the_factory_definition():
    my_instance = fragvars_queries.image_parts.with_args(width=17)
    bound = fragvars_queries.get_attachment.bind(attachment=my_instance)
    [reader] = bound.slot_readers["attachment"]
    assert type(reader.definition) is type(fragvars_queries.image_parts)
    assert reader.definition is not fragvars_queries.image_parts
    assert reader.definition is not my_instance


# --- two-level spread chain -- the deepest definition still reads -----

CHAIN_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
    caption: String!
    altText: String!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_composition_chain",
    schema=CHAIN_SCHEMA,
    queries='''
    from tests.generated.bindings_composition_chain.gql.api import api_gql

    leaf_parts = api_gql(
        """
        fragment LeafParts on ImageAttachment {
            url
        }
        """
    )

    middle_parts = api_gql(
        """
        fragment MiddleParts on ImageAttachment {
            caption
            ...LeafParts
        }
        """
    )

    root_parts = api_gql(
        """
        fragment RootParts on ImageAttachment {
            altText
            ...MiddleParts
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    bound = get_attachment.bind(attachment=root_parts)
    ''',
)

from tests.generated.bindings_composition_chain import queries as chain_queries


def _resolve_chain_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {
        "id": id,
        "attachment": {
            "__typename": "ImageAttachment",
            "url": "leaf-url",
            "caption": "middle-caption",
            "altText": "root-alt",
        },
    }


async def test_two_level_spread_chain_deepest_definition_reads(
    httpserver: HTTPServer,
):
    # `RootParts` spreads `MiddleParts` spreads `LeafParts` -- only `RootParts`
    # is named in `bind()`. `LeafParts` sits two hops deep in the transitive
    # closure, proving the closure walk isn't limited to a single level of
    # spreading (the single-hop case is covered above by BaseParts/ImageParts).
    async with gql_server(
        httpserver,
        "bindings_composition_chain",
        {"Query": {"post": _resolve_chain_post}},
    ):
        result = await chain_queries.bound.execute(id="1")
        assert result.post is not None
        node = result.post.attachment

        root = chain_queries.root_parts.read(node)
        assert root is not None
        assert root.alt_text == "root-alt"

        middle = chain_queries.middle_parts.read(node)
        assert middle is not None
        assert middle.caption == "middle-caption"

        leaf = chain_queries.leaf_parts.read(node)
        assert leaf is not None
        assert leaf.url == "leaf-url"


# --- boundary validation covers closure-only fragments too --------

BOUNDARY_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

interface Node {
    id: ID!
}

type ImageAttachment implements Node {
    id: ID!
    url: String!
}

type LinkAttachment {
    href: String!
}

union Attachment = ImageAttachment | LinkAttachment
"""

BOUNDARY_QUERIES = '''
from sample_app.gql.api import api_gql

node_id = api_gql(
    """
    fragment NodeId on Node {{
        id
    }}
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {{
        url
        {spread}
    }}
    """
)

get_attachment = api_gql(
    """
    query GetAttachment($id: ID!) {{
        post(id: $id) {{
            id
            attachment @slot {{ __typename }}
        }}
    }}
    """
)

bound = get_attachment.bind(attachment=image_parts)
'''


# Boundary validation covers every fragment readable at a slot's root, not
# only the ones a caller happens to read -- so every one of them must actually
# be in the payload. A conditional spread breaks that: the server leaves
# `NodeId`'s required `id` out whenever the condition is false, and a
# completely correct response then fails validation. The literal-false form
# used to generate happily and blow up at execute(); the variable form, an
# ordinary GraphQL idiom, was worse -- the generator itself offered
# `with_args(with_id=...)` and every value of it produced a broken call. The
# wrapped forms are the same failure written one AST level up: looking for the
# directive on the spread node alone let an inline fragment carry it straight
# past the rule. All are rejected at generation now, naming the fragment and
# the fix.
@pytest.mark.parametrize(
    "spread",
    [
        "...NodeId @include(if: false)",
        "...NodeId @include(if: $withId)",
        "...NodeId @skip(if: $noId)",
        "... @include(if: $withId) { ...NodeId }",
        "... on ImageAttachment @skip(if: $noId) { ...NodeId }",
    ],
)
def test_conditional_spread_in_a_binding_closure_is_rejected(
    test_project: ProjectBuilder, spread: str
):
    test_project.prepare(
        schema=BOUNDARY_SCHEMA, queries=BOUNDARY_QUERIES.format(spread=spread)
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"fragment 'ImageParts' spreads 'NodeId' under @skip/@include",
    ):
        test_project.generate()


generated_package(
    "bindings_composition_boundary",
    schema=BOUNDARY_SCHEMA,
    queries=BOUNDARY_QUERIES.format(spread="...NodeId").replace(
        "sample_app.gql.api", "tests.generated.bindings_composition_boundary.gql.api"
    ),
)

from tests.generated.bindings_composition_boundary import queries as boundary_queries
from tests.generated.bindings_composition_boundary.gql import api as boundary_api

# Deliberately short of `NodeId`'s required `id`: a conforming server cannot
# produce this, since the rule above rules out the conditional spread that used
# to make it happen, so the payload is canned.
BOUNDARY_BODY = {
    "data": {
        "post": {
            "id": "1",
            "attachment": {"__typename": "ImageAttachment", "url": "u1"},
        }
    }
}

# The same response with the closure-only brick's own field in place, so
# boundary validation passes and the tests below get a result to look at.
COMPLETE_BOUNDARY_BODY = {
    "data": {
        "post": {
            "id": "1",
            "attachment": {"__typename": "ImageAttachment", "url": "u", "id": "n"},
        }
    }
}


async def test_unread_closure_only_brick_still_validates_eagerly(
    httpserver: HTTPServer,
):
    # `NodeId` is reachable only through `ImageParts`'s spread, and nobody
    # here calls `node_id.read(...)`. Boundary validation covers every
    # fragment in the binding's closure, not only the ones a caller
    # happens to read, so a payload missing a closure-only brick's field must
    # still raise at execute().
    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        BOUNDARY_BODY
    )
    async with use_package_client(
        "bindings_composition_boundary", httpserver.url_for("/graphql/")
    ):
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _ = await boundary_queries.bound.execute(id="1")
    assert exc_info.value.errors()[0]["loc"] == (
        "post",
        "attachment",
        "ImageAttachment",
        "id",
    )
    assert exc_info.value.errors()[0]["type"] == "missing"


async def test_bound_operation_validation_error_names_the_shared_result_class(
    httpserver: HTTPServer,
):
    # One bound class serves every combination of a template now, and its
    # `execute` always hands pydantic the same bare result class -- every
    # phantom at its `Never` default, `cast` to the caller's own binding's
    # promised type (see `render_template_bases`'s comment on that `cast`, and
    # `tests/test_slots_runtime.py` for the proof a bare class validates
    # identically to a parametrised one). The ValidationError title reflects
    # that: it no longer names a combination-specific parametrisation, because
    # no such runtime class exists to name any more.
    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        BOUNDARY_BODY
    )
    async with use_package_client(
        "bindings_composition_boundary", httpserver.url_for("/graphql/")
    ):
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _ = await boundary_queries.bound.execute(id="1")
    title = str(exc_info.value).splitlines()[0]
    assert title == "1 validation error for GetAttachmentResult"


async def test_execute_validates_with_the_templates_shared_result_class(
    httpserver: HTTPServer,
):
    # `execute` is written once, on the template's shared bound base, and
    # always validates against that one bare result class -- never a
    # per-combination parametrisation, which no longer exists as a runtime
    # object at all (only as the static phantom `bind()`'s own overload
    # promised). Pins that the object the client actually receives is that one
    # shared class, whichever combination produced it.
    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        COMPLETE_BOUNDARY_BODY
    )
    async with use_package_client(
        "bindings_composition_boundary", httpserver.url_for("/graphql/")
    ):
        result = await boundary_queries.bound.execute(id="1")
    assert type(result) is boundary_api.GetAttachmentResult


async def test_a_helper_generic_over_the_binding_reads_what_it_was_handed(
    httpserver: HTTPServer,
):
    # The shape shared infrastructure is written in: the helper owns the
    # operation and each caller owns the selection, so the helper spells the
    # phantom `Any` -- "whatever this binding offered" -- and reads with the
    # fragment it was given. That annotation is the whole of what it gives up:
    # the same `read`, and the runtime guard below still answers.
    def first_attachment[TData: pydantic.BaseModel](
        result: boundary_api.GetAttachmentResult[Any],
        fragment: GQLFragment[TData, Any],
    ) -> TData | None:
        assert result.post is not None
        return fragment.read(result.post.attachment)

    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        COMPLETE_BOUNDARY_BODY
    )
    async with use_package_client(
        "bindings_composition_boundary", httpserver.url_for("/graphql/")
    ):
        result = await boundary_queries.bound.execute(id="1")
    image = first_attachment(result, boundary_queries.image_parts)
    assert image is not None
    assert image.url == "u"
    # And the guard survives the erasure: a definition this binding never offered
    # is a wiring bug, not a type mismatch that reads back as None -- the
    # static check is gone, the runtime one is not.
    with pytest.raises(ValueError, match=r"is not part of the binding"):
        first_attachment(result, shape_queries.thumb_alt)


async def test_a_bound_result_with_a_populated_slot_does_not_pickle(
    httpserver: HTTPServer,
):
    # `execute` always validates against the template's one bare result class
    # (`GetAttachmentResult`, no combination-specific parametrisation exists
    # any more -- see `render_template_bases`'s comment on the `cast`), so a
    # populated path to the slot instantiates a nested model off that class's
    # own unbound slot phantom rather than off a concrete union of fragment
    # classes. Pydantic still creates it without a module-level name, and
    # pickle still cannot resolve it -- the class just carries a different,
    # less specific name in its error now (`Post[TypeVar]` rather than
    # `Post[ImageParts | NodeId]`), because there is no concrete
    # parametrisation left to report.
    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        COMPLETE_BOUNDARY_BODY
    )
    async with use_package_client(
        "bindings_composition_boundary", httpserver.url_for("/graphql/")
    ):
        result = await boundary_queries.bound.execute(id="1")
    with pytest.raises(pickle.PicklingError, match=r"Post\[TypeVar\]"):
        pickle.dumps(result)


def test_a_bound_result_with_a_null_slot_path_pickles_across_processes():
    # No nested parametrized model is instantiated when the nullable parent is
    # null, and the root class itself is the plain module-level
    # `GetAttachmentResult` -- `execute` never subscripts a
    # combination-specific parametrisation at module scope any more (there is
    # no more per-combination class whose base-list expression used to do
    # that; see `render_template_bases`'s comment on the `cast`), so a fresh
    # interpreter can import the bare class as-is and restore the result.
    result = boundary_api.GetAttachmentResult.model_validate({"post": None})
    assert result.post is None

    child = """
import pickle
import sys

result = pickle.loads(sys.stdin.buffer.read())
assert result.post is None
"""
    completed = subprocess.run(
        [sys.executable, "-c", child],
        input=pickle.dumps(result),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()


# --- fragment variables ---------------------------------------------

FRAGMENT_VARS_SCHEMA = """
type Query {
    post(id: ID!): Post
}

# Two slots of one type, so the schema's own enumeration holds the row where
# a single factory fills both of them -- the combination nobody has to write
# for it to exist, and the one whose two applications supply two values for
# the one `$width` the expanded document declares.
type Post {
    id: ID!
    attachment: Attachment
    preview: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

# `size` carries the one type a fragment variable can introduce that no other
# position in the package mentions: an enum reaches the module only through
# this binding's synthesized variable, so a collection order that reads
# fragment variables after the enums are already listed emits `size: Size`
# with no `Size` to go with it. Optional, so the variable stays a free rider
# on the tests either side of it.
type ImageAttachment {
    url(width: Int!, size: Size = SMALL): String!
}

enum Size {
    SMALL
    LARGE
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_fragment_vars",
    schema=FRAGMENT_VARS_SCHEMA,
    queries='''
    from tests.generated.bindings_fragment_vars.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
                preview @slot { __typename }
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url(width: $width, size: $size)
        }
        """
    )
    ''',
)

from tests.generated.bindings_fragment_vars import queries as fragvars_queries
from tests.generated.bindings_fragment_vars.gql import api as fragvars_api


def _fragment_vars_handler(
    seen: list[tuple[dict[str, object], dict[str, str]]],
) -> Callable[[Request], Response]:
    def handler(request: Request) -> Response:
        payload = GraphQLRequest.model_validate_json(request.get_data())
        variables = payload.variables or {}
        seen.append((variables, dict(request.headers)))
        width = variables["width"]
        # Both slots answer with the same `$width`, because the document
        # declares it once -- the server has no way to tell the two spreads
        # apart, which is the whole reason two disagreeing applications are
        # rejected before the request is built.
        attachment = {"__typename": "ImageAttachment", "url": f"img-{width}"}
        body = {
            "data": {
                "post": {
                    "id": variables["id"],
                    "attachment": attachment,
                    "preview": dict(attachment),
                }
            }
        }
        return Response(json.dumps(body), status=200, mimetype="application/json")

    return handler


async def test_applied_fragment_carries_its_arguments_to_the_request(
    httpserver: HTTPServer,
):
    # The point of attachment the design moved: `image_parts` is a factory
    # (its own `$width` has no schema default), so it has no `bind()` of its
    # own -- only `with_args`, called where the value is known, returns the
    # real application that `bind()` accepts and that reads the result. Headers
    # applied after `bind()` -- the existing `_copy` machinery -- still carry
    # the values through unchanged.
    seen: list[tuple[dict[str, object], dict[str, str]]] = []
    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        _fragment_vars_handler(seen)
    )
    async with use_package_client(
        "bindings_fragment_vars", httpserver.url_for("/graphql/")
    ):
        applied = fragvars_queries.image_parts.with_args(width=800)
        bound = fragvars_queries.get_attachment.bind(attachment=applied).with_headers({
            "X-Test": "y"
        })
        result = await bound.execute(id="1")
        assert result.post is not None
        image = applied.read(result.post.attachment)
        assert image is not None
        assert image.url == "img-800"

    variables, headers = seen[0]
    assert variables["width"] == 800
    assert headers["X-Test"] == "y"


async def test_two_applications_of_one_factory_share_a_projection(
    httpserver: HTTPServer,
):
    # Applications хранят request variables для `bind()`, но projection
    # принадлежит общей generated definition. Поэтому другая application той
    # же factory читает response независимо от своих arguments.
    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        _fragment_vars_handler([])
    )
    async with use_package_client(
        "bindings_fragment_vars", httpserver.url_for("/graphql/")
    ):
        bound_applied = fragvars_queries.image_parts.with_args(width=1)
        unbound_applied = fragvars_queries.image_parts.with_args(width=1)
        assert bound_applied is not unbound_applied
        result = await fragvars_queries.get_attachment.bind(
            attachment=bound_applied
        ).execute(id="1")
        assert result.post is not None
        node = result.post.attachment
        assert bound_applied.read(node) is not None
        assert unbound_applied.read(node) is not None
        assert fragvars_queries.image_parts.read(node) is not None


def test_with_args_rejects_extra_variables_and_keeps_assignment_immutable():
    with pytest.raises(TypeError, match="id"):
        cast("Callable[..., object]", fragvars_queries.image_parts.with_args)(
            width=800, id="injected"
        )

    applied = fragvars_queries.image_parts.with_args(width=800)
    with pytest.raises(TypeError):
        cast("dict[str, object]", applied.fragment_args__)["id"] = "injected"


def test_bindable_base_constructor_cannot_accept_fragment_variables():
    constructor = cast("Callable[..., object]", GQLBindableFragment)
    with pytest.raises(TypeError, match="_fragment_args"):
        constructor(
            fragment_name="ImageParts",
            adapter=pydantic.TypeAdapter(pydantic.BaseModel),
            _fragment_args={"id": "injected"},
        )


def test_generated_bound_cannot_be_created_without_binding_state():
    constructor = cast("Callable[[], object]", fragvars_api.GetAttachmentBound)
    with pytest.raises(TypeError):
        constructor()


def test_one_factory_filling_two_slots_with_different_values_is_rejected():
    # The combination is enumerated from the schema -- both slots take an
    # `ImageAttachment` fragment -- so it exists whether or not a call site
    # writes it, and both applications type-check against their own slot's
    # on-type base. What cannot exist is the request: the expanded document
    # declares `$width` once, so the two values cannot both be sent, and the
    # flat merge used to answer with whichever slot came last while
    # the old per-application readers kept both applications' own values.
    with pytest.raises(ValueError, match=r"conflicting values.*\$width") as exc_info:
        fragvars_queries.get_attachment.bind(
            attachment=fragvars_queries.image_parts.with_args(
                width=cast("int", cast("object", "secret-left"))
            ),
            preview=fragvars_queries.image_parts.with_args(
                width=cast("int", cast("object", "secret-right"))
            ),
        )
    assert "secret-left" not in str(exc_info.value)
    assert "secret-right" not in str(exc_info.value)


def test_one_factory_filling_two_slots_disagreeing_on_an_omitted_arg_is_rejected():
    # The same rule for the other way two applications part: `$size` is
    # omittable, and leaving it out is not "no opinion" but a request of its
    # own -- the variable stays out of `variables` so the schema's default
    # applies. One declaration of `$size` cannot both be absent and be
    # 'LARGE', and comparing only the keys both applications wrote let the
    # one that named it answer for the one that did not.
    with pytest.raises(ValueError, match=r"conflicting values.*\$size"):
        fragvars_queries.get_attachment.bind(
            attachment=fragvars_queries.image_parts.with_args(width=100),
            preview=fragvars_queries.image_parts.with_args(width=100, size="LARGE"),
        )


async def test_one_factory_filling_two_slots_with_equal_values_binds(
    httpserver: HTTPServer,
):
    # The other half of the rule above: agreeing applications ask for a
    # request the document *can* express, so they bind, send the one value
    # both spreads share, and stay two applications reading their own slot.
    seen: list[tuple[dict[str, object], dict[str, str]]] = []
    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        _fragment_vars_handler(seen)
    )
    async with use_package_client(
        "bindings_fragment_vars", httpserver.url_for("/graphql/")
    ):
        first = fragvars_queries.image_parts.with_args(width=300)
        second = fragvars_queries.image_parts.with_args(width=300)
        bound = fragvars_queries.get_attachment.bind(attachment=first, preview=second)
        result = await bound.execute(id="1")

    variables, _headers = seen[0]
    assert variables["width"] == 300
    assert result.post is not None
    attachment = first.read(result.post.attachment)
    preview = second.read(result.post.preview)
    assert attachment is not None
    assert preview is not None
    assert (attachment.url, preview.url) == ("img-300", "img-300")


def test_binding_a_factory_itself_is_rejected():
    # A factory carries no values -- `with_args` builds the application that does
    # -- so binding it would send a document declaring `$width` with nothing
    # to fill it, and the server would be the first to notice. The generated
    # signatures already refuse it (no on-type base, and a literal tuple names
    # the applied class), so the path this guards is the type-erased one:
    # here, `bind` reached as a plain callable.
    bind = cast("Callable[..., object]", fragvars_queries.get_attachment.bind)
    with pytest.raises(TypeError):
        bind(attachment=fragvars_queries.image_parts)


def test_user_on_type_subclass_cannot_select_a_generated_combination():
    class ForgedImageParts(
        fragvars_api.OnImageAttachment[fragvars_api.ImagePartsData, Any]
    ):
        @override
        def __init__(self) -> None:
            super().__init__(
                fragment_name="ImageParts",
                definition_type=ForgedImageParts,
                adapter=pydantic.TypeAdapter(fragvars_api.ImagePartsData),
            )

    with pytest.raises(LookupError, match="unknown bind combination"):
        fragvars_queries.get_attachment.bind(attachment=ForgedImageParts())


# Четыре позиции, два поведения. `$width` nullable без default, поэтому ведёт
# себя как operation variable в `execute`: keyword обязателен, а `None`
# отправляется как явный null. `$height`, `$pad` и `$slots` имеют schema default
# и могут быть пропущены. `$slots` намеренно совпадает с именем runtime-модуля,
# который импортирует generated code: допустимое GraphQL-имя не должно менять
# разрешение sentinel в теле `with_args`.
FRAGMENT_VAR_NULLABILITY_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url(width: Int, height: Int! = 10, pad: Int = 7, slots: Int! = 13): String!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_fragment_var_nullability",
    schema=FRAGMENT_VAR_NULLABILITY_SCHEMA,
    queries='''
    from tests.generated.bindings_fragment_var_nullability.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url(width: $width, height: $height, pad: $pad, slots: $slots)
        }
        """
    )
    ''',
)

from tests.generated.bindings_fragment_var_nullability import (
    queries as nullability_queries,
)


def _resolve_nullability_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {"id": id, "attachment": {"__typename": "ImageAttachment"}}


def _resolve_sized_url(
    _root: object,
    _info: GraphQLResolveInfo,
    *,
    width: int | None,
    height: int,
    pad: int | None,
    slots: int,
) -> str:
    return f"{width}-{height}-{pad}-{slots}"


async def test_only_the_defaulted_fragment_variable_may_be_omitted(
    httpserver: HTTPServer,
):
    # `$height` and `$pad` carry a schema default and drop out of the request
    # when the keyword is left out of `with_args` entirely, so the schema's
    # own `= 10` and `= 7` apply; `$width` has no default and an explicit
    # `None` crosses the wire as a real null.
    async with gql_server(
        httpserver,
        "bindings_fragment_var_nullability",
        {
            "Query": {"post": _resolve_nullability_post},
            "ImageAttachment": {"url": _resolve_sized_url},
        },
    ):
        applied = nullability_queries.image_parts.with_args(width=None)
        bound = nullability_queries.get_attachment.bind(attachment=applied)
        result = await bound.execute(id="1")
        assert result.post is not None
        image = applied.read(result.post.attachment)
        assert image is not None
        assert image.url == "None-10-7-13"

        sized_applied = nullability_queries.image_parts.with_args(
            width=5, height=20, pad=1, slots=21
        )
        sized_bound = nullability_queries.get_attachment.bind(attachment=sized_applied)
        result = await sized_bound.execute(id="1")
        assert result.post is not None
        image = sized_applied.read(result.post.attachment)
        assert image is not None
        assert image.url == "5-20-1-21"

        # Каждая application задаёт весь набор: пропущенные здесь `height` и
        # `pad` возвращаются к schema defaults независимо от предыдущей
        # application. Повторное применение создаёт новую application и bind, а не
        # изменяет предыдущий.
        again_applied = nullability_queries.image_parts.with_args(width=5)
        again_bound = nullability_queries.get_attachment.bind(attachment=again_applied)
        result = await again_bound.execute(id="1")
        assert result.post is not None
        image = again_applied.read(result.post.attachment)
        assert image is not None
        assert image.url == "5-10-7-13"

        null_applied = nullability_queries.image_parts.with_args(width=5, pad=None)
        null_bound = nullability_queries.get_attachment.bind(attachment=null_applied)
        result = await null_bound.execute(id="1")
        assert result.post is not None
        image = null_applied.read(result.post.attachment)
        assert image is not None
        assert image.url == "5-10-None-13"


# --- two applications judged by the request, not by `==` --------------

# A JSON scalar is the position where the distinction is visible: it accepts
# `1` and `true` alike, so two applications can ask for genuinely different
# requests with values Python calls equal (`1 == True`). Two slots of one
# type, so a single factory fills both and their values meet in the merge.
#
# `[Upload]` is the position where the *other* erasure is visible: a file
# serializes to `null` and rides in the multipart body instead, so what tells
# two upload arguments apart is which file each carries and where it sits --
# a list is the shortest value with two "wheres" in it.
WIRE_SHAPE_SCHEMA = """
scalar JSON
scalar Upload

type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
    preview: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url(payload: JSON, files: [Upload]): String!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_wire_shape",
    schema=WIRE_SHAPE_SCHEMA,
    scalars=UPLOAD_SCALARS,
    queries='''
    from tests.generated.bindings_wire_shape.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
                preview @slot { __typename }
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url(payload: $payload)
        }
        """
    )

    image_files = api_gql(
        """
        fragment ImageFiles on ImageAttachment {
            url(files: $files)
        }
        """
    )
    ''',
)

from tests.generated.bindings_wire_shape import queries as wire_shape_queries


@pytest.mark.parametrize(
    ("left", "right"),
    [(1, True), (0, False), (1, 1.0), ({"a": 1}, {"a": True})],
    ids=["int-bool", "zero-false", "int-float", "nested"],
)
def test_applications_python_calls_equal_but_the_wire_does_not_are_rejected(
    left: object, right: object
):
    # `1 == True` and `1 == 1.0` are true in Python and false on the wire: a
    # JSON scalar receives `1`, `true` and `1.0` as three different values.
    # Judging agreement by `==` therefore kept whichever application came
    # first and sent its value for both spreads, which is the silent
    # mismatch the whole merge check exists to prevent -- so the comparison
    # is over what `serialize_variables` would send (`runtime._same_request`),
    # nesting included.
    with pytest.raises(ValueError, match=r"conflicting values.*\$payload"):
        wire_shape_queries.get_attachment.bind(
            attachment=wire_shape_queries.image_parts.with_args(payload=left),
            preview=wire_shape_queries.image_parts.with_args(payload=right),
        )


def test_applications_agreeing_on_the_wire_bind_whatever_python_shape_they_have():
    # The other side of judging by the request: a list and a tuple serialize
    # to the same JSON array, so two applications spelling one value either
    # way ask for the same request and bind.
    bound = wire_shape_queries.get_attachment.bind(
        attachment=wire_shape_queries.image_parts.with_args(payload=[1, 2]),
        preview=wire_shape_queries.image_parts.with_args(payload=(1, 2)),
    )
    assert bound.fragment_args == {"payload": [1, 2]}


async def test_mixed_mapping_keys_are_compared_after_wire_json_normalization(
    httpserver: HTTPServer,
):
    # Python не сортирует вместе int и str keys, а HTTP JSON encoder сначала
    # превращает оба в строки имён JSON object. Две applications с этим value
    # задают один request и обязаны bind-иться; execute доказывает, что сравнение
    # и transport используют одну normalized shape.
    seen: list[dict[str, object]] = []

    def handler(request: Request) -> Response:
        payload = GraphQLRequest.model_validate_json(request.get_data())
        seen.append(payload.variables or {})
        attachment = {"__typename": "ImageAttachment", "url": "ok"}
        body = {
            "data": {
                "post": {
                    "id": "1",
                    "attachment": attachment,
                    "preview": dict(attachment),
                }
            }
        }
        return Response(json.dumps(body), status=200, mimetype="application/json")

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(handler)
    async with use_package_client(
        "bindings_wire_shape", httpserver.url_for("/graphql/")
    ):
        value = {1: "a", "2": "b"}
        bound = wire_shape_queries.get_attachment.bind(
            attachment=wire_shape_queries.image_parts.with_args(payload=value),
            preview=wire_shape_queries.image_parts.with_args(payload=dict(value)),
        )
        await bound.execute(id="1")

    assert seen == [{"id": "1", "payload": {"1": "a", "2": "b"}}]


def test_one_file_offered_at_two_places_is_two_requests():
    # The same file is not the same request when the two applications put it
    # somewhere else: `[file, None]` and `[None, file]` have identical JSON
    # (`[null, null]`, the file rides in the multipart body) and identical
    # file identity, so a comparison carrying only "which file" called them
    # one request -- the merge kept the first, and the second slot's spread
    # asked for position 1 while the map that went out named position 0.
    # Nothing was raised and nothing was wrong on the wire to see.
    one = FileVar(b"one", filename="one.txt")
    with pytest.raises(ValueError, match=r"conflicting values.*\$files"):
        wire_shape_queries.get_attachment.bind(
            attachment=wire_shape_queries.image_files.with_args(files=[one, None]),
            preview=wire_shape_queries.image_files.with_args(files=[None, one]),
        )


def test_one_file_offered_at_the_same_place_twice_is_one_request():
    # The positive twin, and what keeps the path from being the whole answer:
    # two applications naming the same file at the same place ask for one
    # request and bind, with the file itself carried through to `execute`.
    one = FileVar(b"one", filename="one.txt")
    bound = wire_shape_queries.get_attachment.bind(
        attachment=wire_shape_queries.image_files.with_args(files=[one, None]),
        preview=wire_shape_queries.image_files.with_args(files=[one, None]),
    )
    assert bound.fragment_args == {"files": [one, None]}


def test_two_files_swapped_between_two_places_are_two_requests():
    # The path alone is no more the answer than the identity alone was: these
    # two applications fill the same two positions, so the paths agree, and
    # they differ only in which file each position carries.
    one, two = FileVar(b"one", filename="one.txt"), FileVar(b"two", filename="two.txt")
    with pytest.raises(ValueError, match=r"conflicting values.*\$files"):
        wire_shape_queries.get_attachment.bind(
            attachment=wire_shape_queries.image_files.with_args(files=[one, two]),
            preview=wire_shape_queries.image_files.with_args(files=[two, one]),
        )


# --- subscription template ------------------------------------------

SUBSCRIPTION_SCHEMA = f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
}}

{ATTACHMENT_UNION}

type Subscription {{
    attachmentChanged(id: ID!): Post!
}}
"""

generated_package(
    "bindings_subscription",
    schema=SUBSCRIPTION_SCHEMA,
    queries='''
    from tests.generated.bindings_subscription.gql.api import api_gql

    image_url = api_gql(
        """
        fragment ImageUrl on ImageAttachment {
            url
        }
        """
    )

    watch_attachment = api_gql(
        """
        subscription WatchAttachment($id: ID!) {
            attachmentChanged(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    bound = watch_attachment.bind(attachment=image_url)
    ''',
)

from tests.generated.bindings_subscription import queries as subscription_queries


async def test_bound_subscription_streams_and_reads_each_message():
    messages: list[dict[str, object]] = [
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": {"__typename": "ImageAttachment", "url": "u1"},
                    }
                }
            },
        },
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": {"__typename": "ImageAttachment", "url": "u2"},
                    }
                }
            },
        },
        {"type": "complete"},
    ]
    app = make_subscription_app(messages)
    async with use_package_client(
        "bindings_subscription", "http://testserver/graphql", target_app=app
    ):
        events: list[str] = []
        # execute() only ever takes the template's own variables -- the
        # fragment is already pinned by .bind(), not passed here.
        async with subscription_queries.bound.execute(id="p-1") as stream:
            async for event in stream:
                image = subscription_queries.image_url.read(
                    event.attachment_changed.attachment
                )
                assert image is not None
                events.append(image.url)
        assert events == ["u1", "u2"]


# --- undiscovered bind combination raises LookupError at import ----

DISCOVERY_SCHEMA = f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
}}

{ATTACHMENT_UNION}
"""


def _discovery_queries(*bind_lines: str) -> str:
    bind_block = "\n    ".join(bind_lines)
    return f'''
    from sample_app.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {{
            post(id: $id) {{
                id
                attachment @slot {{ __typename }}
            }}
        }}
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {{
            url
        }}
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {{
            href
        }}
        """
    )

    # A second ImageAttachment fragment, distinct from `image_parts`: the
    # tuple bind below must not reuse a fragment already bound alone to the
    # same slot elsewhere -- that combination is rejected.
    other_image_parts = api_gql(
        """
        fragment OtherImageParts on ImageAttachment {{
            url
        }}
        """
    )

    {bind_block}
    '''


def test_undiscovered_tuple_bind_raises_lookuperror_at_import(
    test_project: ProjectBuilder,
):
    # "Forgot to regenerate" now only reaches the multi-fragment
    # combinations: every single-fragment and empty combination comes from the
    # schema, so nothing a caller spells with one fragment per slot can be
    # missing. A tuple is still written by a call site alone, so a tuple
    # nobody wrote is still a combination the dispatch table has never been
    # given.
    test_project.prepare(
        schema=DISCOVERY_SCHEMA,
        queries=_discovery_queries(
            "both = get_attachment.bind(attachment=(other_image_parts, link_parts))",
        ),
    )
    _ = test_project.generate_and_import()

    # Same fragment/template literal text (still resolvable through the
    # already-generated dispatch dicts) but a *different* list. Rewriting
    # queries.py without regenerating and re-importing reproduces exactly the
    # "call site changed, forgot to regenerate" mistake this error exists to
    # catch.
    test_project.write_file(
        test_project.root / f"{test_project.package}/queries.py",
        _discovery_queries(
            "both = get_attachment.bind(attachment=(image_parts, link_parts))"
        ),
    )
    test_project.clear_import_state()
    with pytest.raises(LookupError, match="unknown bind combination"):
        importlib.import_module(f"{test_project.package}.queries")


def test_a_bind_the_scan_cannot_read_is_sent_to_the_ignored_binds_file(
    test_project: ProjectBuilder,
):
    # The other way into the same `LookupError`, and the reason its message
    # names two: here the combination *is* written, on a call no scan can
    # read -- the template is the value under a dict key, which is a runtime
    # question. Nothing is generated for it, so advising a regeneration alone
    # sends the reader after a fix that changes nothing; the call is recorded
    # in `ignored_binds.json` instead (`test_bind_discovery` pins that
    # artifact), and the message has to say so.
    test_project.prepare(
        schema=DISCOVERY_SCHEMA,
        queries=_discovery_queries(
            "templates = {'q': get_attachment}",
            "both = templates['q'].bind(attachment=(image_parts, link_parts))",
        ),
    )
    with pytest.raises(LookupError, match=r"ignored_binds\.json"):
        _ = test_project.generate_and_import()


# --- the same fragment bound into two different templates ----------

TWO_TEMPLATES_SCHEMA = f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
    highlight: Attachment
}}

{ATTACHMENT_UNION}
"""

generated_package(
    "bindings_two_templates",
    schema=TWO_TEMPLATES_SCHEMA,
    queries='''
    from tests.generated.bindings_two_templates.gql.api import api_gql

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    get_highlight = api_gql(
        """
        query GetHighlight($id: ID!) {
            post(id: $id) {
                id
                highlight @slot { __typename }
            }
        }
        """
    )

    bound_attachment = get_attachment.bind(attachment=image_parts)
    bound_highlight = get_highlight.bind(highlight=image_parts)
    ''',
)

from tests.generated.bindings_two_templates import queries as two_templates_queries


def _resolve_two_templates_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {
        "id": id,
        "attachment": {"__typename": "ImageAttachment", "url": "attachment-url"},
        "highlight": {"__typename": "ImageAttachment", "url": "highlight-url"},
    }


async def test_same_fragment_bound_into_two_templates_reads_each_result(
    httpserver: HTTPServer,
):
    async with gql_server(
        httpserver,
        "bindings_two_templates",
        {"Query": {"post": _resolve_two_templates_post}},
    ):
        attachment_result = await two_templates_queries.bound_attachment.execute(id="1")
        highlight_result = await two_templates_queries.bound_highlight.execute(id="1")
        assert attachment_result.post is not None
        assert highlight_result.post is not None

        attachment = two_templates_queries.image_parts.read(
            attachment_result.post.attachment
        )
        highlight = two_templates_queries.image_parts.read(
            highlight_result.post.highlight
        )
        assert attachment is not None
        assert highlight is not None
        assert attachment.url == "attachment-url"
        assert highlight.url == "highlight-url"


# --- a template with no binds ---------------------------------------


def test_a_template_no_call_site_binds_is_bindable_anyway(
    test_project: ProjectBuilder,
):
    # `get_highlight` has no `.bind()` anywhere in the package, and binding it
    # still works: the combinations come from the schema, so a helper handed a
    # fragment as a parameter finds a text for it. This is the whole point of
    # the redesign, and the case that used to raise `LookupError`. A list is
    # still the one shape a call site has to write, so that one still raises.
    test_project.prepare(
        schema=TWO_TEMPLATES_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                url
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    attachment @slot { __typename }
                }
            }
            '''
        )

        link_parts = api_gql(
            '''
            fragment LinkParts on LinkAttachment {
                href
            }
            '''
        )

        get_highlight = api_gql(
            '''
            query GetHighlight($id: ID!) {
                post(id: $id) {
                    id
                    highlight @slot { __typename }
                }
            }
            '''
        )

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    fragment = queries_module.image_parts  # pyright: ignore[reportAny]
    other = queries_module.link_parts  # pyright: ignore[reportAny]
    get_highlight = queries_module.get_highlight  # pyright: ignore[reportAny]
    bound = get_highlight.bind(highlight=fragment)  # pyright: ignore[reportAny]
    [reader] = bound.slot_readers["highlight"]  # pyright: ignore[reportAny]
    assert type(reader.definition) is type(fragment)  # pyright: ignore[reportAny]
    with pytest.raises(LookupError, match="unknown bind combination"):
        get_highlight.bind(highlight=(fragment, other))  # pyright: ignore[reportAny]


# --- statically excluded slot rule still enforced with a bind -----


def test_statically_excluded_slot_is_rejected_even_with_a_bind(
    test_project: ProjectBuilder,
):
    # Same scenario as test_statically_excluded_slot_is_rejected in
    # tests/test_slots.py (collection's own excluded-slot check), with a
    # .bind() call added: expand_binding itself has nothing to reject (the
    # field is still a syntactically ordinary slot from its own point of
    # view), so this pins that the check still runs -- and still rejects --
    # for a template that has bindings, not only bind-less ones.
    test_project.prepare(
        schema=DISCOVERY_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    ... @include(if: false) {
                        attachment @slot { __typename }
                    }
                }
            }
            '''
        )

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                url
            }
            '''
        )

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="statically excluded"):
        test_project.generate()


# --- Where a closure fragment's data actually lands -------------------------

CLOSURE_SHAPE_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

interface Node {
    id: ID!
}

type ImageAttachment implements Node {
    id: ID!
    url: String!
    thumb: Thumb!
}

type Thumb {
    alt: String!
}

type LinkAttachment {
    href: String!
}

union Attachment = ImageAttachment | LinkAttachment
"""

generated_package(
    "bindings_closure_shape",
    schema=CLOSURE_SHAPE_SCHEMA,
    queries='''
    from tests.generated.bindings_closure_shape.gql.api import api_gql

    node_id = api_gql(
        """
        fragment NodeId on Node {
            id
        }
        """
    )

    thumb_alt = api_gql(
        """
        fragment ThumbAlt on Thumb {
            alt
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url
            ...NodeId
            thumb { ...ThumbAlt }
        }
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {
            href
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    bound = get_attachment.bind(attachment=(image_parts, link_parts))
    ''',
)

from tests.generated.bindings_closure_shape import queries as shape_queries


def _resolve_shape_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    if id == "link":
        return {"id": id, "attachment": {"__typename": "LinkAttachment", "href": "h"}}
    return {
        "id": id,
        "attachment": {
            "__typename": "ImageAttachment",
            "id": "img-1",
            "url": "u",
            "thumb": {"alt": "a"},
        },
    }


async def test_a_narrowing_brick_does_not_demand_its_fields_from_a_sibling_type(
    httpserver: HTTPServer,
):
    # `NodeId` is spread inside `ImageParts`, so the server sends `id` only
    # for an ImageAttachment payload. Validating the brick at its own type
    # condition -- every Node -- made a correct LinkAttachment response fail
    # with "id Field required", breaking every binding that mixes a shared
    # interface brick with per-type fragments.
    async with gql_server(
        httpserver,
        "bindings_closure_shape",
        {"Query": {"post": _resolve_shape_post}},
    ):
        result = await shape_queries.bound.execute(id="link")
        assert result.post is not None
        node = result.post.attachment
        assert shape_queries.link_parts.read(node) is not None
        # Reachable at this slot only through the ImageAttachment branch, so a
        # LinkAttachment payload reads back as None rather than raising.
        assert shape_queries.node_id.read(node) is None

        image_result = await shape_queries.bound.execute(id="1")
        assert image_result.post is not None
        image_node = image_result.post.attachment
        brick = shape_queries.node_id.read(image_node)
        assert brick is not None
        assert brick.id == "img-1"


async def test_a_fragment_spread_under_a_field_stays_readable_through_its_owner(
    httpserver: HTTPServer,
):
    # `ThumbAlt`'s `alt` arrives under `thumb`, never on the slot's root
    # payload. Offering it as a readable definition validated it against the root and
    # failed every response; its data is reached through `ImageParts`'s own
    # model instead, and asking the slot for it is a wiring error.
    async with gql_server(
        httpserver,
        "bindings_closure_shape",
        {"Query": {"post": _resolve_shape_post}},
    ):
        result = await shape_queries.bound.execute(id="1")
        assert result.post is not None
        node = result.post.attachment
        image = shape_queries.image_parts.read(node)
        assert image is not None
        assert image.thumb.alt == "a"
        with pytest.raises(ValueError, match=r"'ThumbAlt' is not part of"):
            read_type_erased(shape_queries.thumb_alt, node)


# --- a brick reached through two tuple elements of different types ---

TWO_TYPENAME_BRICK_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

interface Node {
    id: ID!
}

type ImageAttachment implements Node {
    id: ID!
    url: String!
}

type LinkAttachment implements Node {
    id: ID!
    href: String!
}

union Attachment = ImageAttachment | LinkAttachment
"""

generated_package(
    "bindings_two_typename_brick",
    schema=TWO_TYPENAME_BRICK_SCHEMA,
    queries='''
    from tests.generated.bindings_two_typename_brick.gql.api import api_gql

    node_id = api_gql(
        """
        fragment NodeId on Node {
            id
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url
            ...NodeId
        }
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {
            href
            ...NodeId
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    bound = get_attachment.bind(attachment=(image_parts, link_parts))
    ''',
)

from tests.generated.bindings_two_typename_brick import queries as two_typename_queries


def test_a_brick_reached_through_two_tuple_elements_keeps_both_typenames():
    # `readable_fragments` unions the typenames a brick is reachable at over
    # every path inside one binding. The table has to carry that union, not
    # recompute it per tuple element -- otherwise the brick reads back `None`
    # on a payload that genuinely carries its fields, for whichever typename
    # the table forgot. `NodeId` is spread by both `ImageParts` and
    # `LinkParts`, which cover the slot's two disjoint runtime types, so a
    # bind naming both tuple elements is the only way to observe the union
    # rather than either single path's narrower set.
    bound = two_typename_queries.get_attachment.bind(
        attachment=(two_typename_queries.image_parts, two_typename_queries.link_parts)
    )
    brick = next(
        reader
        for reader in bound.slot_readers["attachment"]
        if type(reader.definition) is type(two_typename_queries.node_id)
    )
    assert brick.typenames == frozenset({"ImageAttachment", "LinkAttachment"})
