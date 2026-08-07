import importlib
import json
from collections.abc import Callable

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.slots import GQLFragment
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

# --- list binding with two disjoint fragments ----------------------

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

    both = get_attachment.bind(attachment=[image_parts, link_parts])
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


async def test_disjoint_list_binding_reads_each_slice_and_rejects_foreign_handle(
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

        # Each handle reads its own slice directly.
        image = disjoint_queries.image_parts.read(image_node)
        link = disjoint_queries.link_parts.read(link_node)
        assert isinstance(image, ImagePartsData)
        assert isinstance(link, LinkPartsData)
        assert image.url == "u1"
        assert link.href == "h1"
        # An offered handle answers None, not raise, on a typename it does
        # not cover -- only a handle never offered to this slot is a wiring
        # bug (below).
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


async def test_a_second_handle_of_a_bound_fragment_is_not_the_singleton(
    httpserver: HTTPServer,
):
    # `GQLSlotNode._slot_data` (src/iron_gql/slots.py) is keyed by
    # `id(handle)`, not by fragment name or class -- precisely so a subclass
    # overriding `__eq__`/`__hash__` cannot alias one fragment's data to
    # another. Nothing else in this suite exercises that: every other "is not
    # part of the binding" test uses a *differently named* fragment, which
    # would pass identically under name-keyed storage. Here `twin` shares the
    # fragment name ('ImageParts') and the model with the singleton that was
    # actually bound (`image_parts`) -- only identity-keying, not name-keying,
    # tells them apart.
    #
    # Built from the runtime base, the shortest spelling of a second handle;
    # the generated class takes the same metadata arguments and accepts them
    # just as well (what its zero-argument spelling does and does not stop is
    # pinned in tests/test_slots_typing.py). Either way the mistake
    # type-checks everywhere the singleton does and surfaces only here, at
    # the read.
    async with gql_server(
        httpserver, "bindings_disjoint", {"Query": {"post": _resolve_disjoint_post}}
    ):
        result = await disjoint_queries.both.execute(id="img")
        assert result.post is not None
        node = result.post.attachment

        # The bound singleton reads its slice with real field data -- proves
        # the distinction below is about identity, not that reading is broken.
        image = disjoint_queries.image_parts.read(node)
        assert image is not None
        assert image.url == "u1"

        twin = GQLFragment(
            fragment_name="ImageParts",
            adapter=pydantic.TypeAdapter(ImagePartsData),
        )
        with pytest.raises(
            ValueError,
            match=(
                "fragment 'ImageParts' is not part of the binding that "
                "produced slot 'attachment'"
            ),
        ):
            read_type_erased(twin, node)


# --- list binding with two overlapping fragments ---------------------

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
    both = get_attachment.bind(attachment=[image_caption, image_size])
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
    # ImageCaption and ImageSize both cover ImageAttachment: each handle reads
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

        both = get_attachment.bind(attachment=[thumb_small, thumb_large])
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
    # A second bind of the same template, so `foreign_parts` is genuinely
    # bind-reachable (gets a real typed handle) while still being foreign to
    # `bound`'s own closure -- an orphan fragment (bound nowhere) never
    # becomes a handle at all (`parser.bind_closures` drops it), so
    # this is the only way to test "outside this binding's closure" rather
    # than "outside every binding".
    elsewhere = get_attachment.bind(attachment=foreign_parts)
    ''',
)

from tests.generated.bindings_composition import queries as composition_queries
from tests.generated.bindings_composition.gql import api as composition_api


def _resolve_composition_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {"id": id, "attachment": {"__typename": "ImageAttachment", "url": "u1"}}


async def test_composed_fragment_inner_handle_reads_its_own_slice(
    httpserver: HTTPServer,
):
    # `ImageParts` spreads `BaseParts` at its own root level, so `BaseParts`
    # is reachable only through that spread -- and its fields still land on
    # the slot's root payload, which is what makes it independently readable
    # through its own handle. That is the whole point of keeping a read layer
    # instead of exact per-binding models: the outer, bound fragment and the
    # inner, merged-in one both read their own model from the same node,
    # through their own handles.
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
        # handle, with real field data -- not merely merged into the outer
        # fragment's model.
        inner = composition_queries.base_parts.read(node)
        assert inner is not None
        assert inner.url == "u1"

        # Erasing a handle's type erases the static check and nothing else:
        # the object is the same one the binding offered, and `_slot_data` is
        # keyed by its identity, so it still reads its own slice with real
        # field data (the README's type-erased-path paragraph, at runtime --
        # its static half is pinned in tests/test_slots_typing.py).
        erased: GQLFragment[pydantic.BaseModel] = composition_queries.image_parts
        erased_read = read_type_erased(erased, node)
        assert isinstance(erased_read, composition_api.ImagePartsData)
        assert erased_read.url == "u1"

        # `foreign_parts` is bind-reachable (bound to `elsewhere`, a
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
    exec_source = composition_api.GetAttachmentWithAttachmentImageParts.exec_source__
    assert "...BaseParts" in exec_source
    assert "fragment BaseParts on ImageAttachment" in exec_source


# --- two-level spread chain -- the deepest handle still reads -----

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


async def test_two_level_spread_chain_deepest_handle_reads(httpserver: HTTPServer):
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


async def test_bound_operation_validation_error_keeps_bare_result_class_name(
    httpserver: HTTPServer,
):
    # `execute` validates against `GetAttachmentResult[ImageParts | NodeId]`,
    # a real subclass pydantic builds and caches per parametrization -- not
    # `GetAttachmentResult` itself. Without the `GQLModel` scaffold's
    # `model_parametrized_name` override, that subclass's mangled `__name__`
    # would leak into the ValidationError title instead of the bare result
    # name a plain (non-bound) operation already shows.
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


# --- fragment variables ---------------------------------------------

FRAGMENT_VARS_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url(width: Int!): String!
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
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url(width: $width)
        }
        """
    )

    bound = get_attachment.bind(attachment=image_parts)
    ''',
)

from tests.generated.bindings_fragment_vars import queries as fragvars_queries


async def test_missing_required_fragment_variable_raises_at_execute_naming_it():
    # $width has no location default in the schema, so it is required --
    # required_arg_names__ pulls it in and fragment_args__() (called from
    # inside the generated execute()) must name it, before any request is
    # made. No server is set up: this must fail before reaching the network.
    with pytest.raises(ValueError, match=r"\$width"):
        _ = await fragvars_queries.bound.execute(id="1")


def _fragment_vars_handler(
    seen: list[tuple[dict[str, object], dict[str, str]]],
) -> Callable[[Request], Response]:
    def handler(request: Request) -> Response:
        payload = GraphQLRequest.model_validate_json(request.get_data())
        variables = payload.variables or {}
        seen.append((variables, dict(request.headers)))
        width = variables["width"]
        body = {
            "data": {
                "post": {
                    "id": variables["id"],
                    "attachment": {
                        "__typename": "ImageAttachment",
                        "url": f"img-{width}",
                    },
                }
            }
        }
        return Response(json.dumps(body), status=200, mimetype="application/json")

    return handler


async def test_with_args_roundtrip_and_with_headers_after_with_args_preserves_args(
    httpserver: HTTPServer,
):
    # The underlying runtime primitives (with_args__/fragment_args__ merging,
    # with_headers carrying fragment args forward) are already pinned at the
    # unit level by test_bound_runtime.py::test_with_headers_preserves_fragment_args
    # and ::test_missing_required_args_raise_with_names. This is the
    # integration proof: the *generated* with_args() wiring actually reaches
    # the server with the right variable name and value, and with_headers
    # called after with_args does not drop them.
    seen: list[tuple[dict[str, object], dict[str, str]]] = []
    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        _fragment_vars_handler(seen)
    )
    async with use_package_client(
        "bindings_fragment_vars", httpserver.url_for("/graphql/")
    ):
        bound = fragvars_queries.bound.with_args(width=800).with_headers({
            "X-Test": "y"
        })
        result = await bound.execute(id="1")
        assert result.post is not None
        image = fragvars_queries.image_parts.read(result.post.attachment)
        assert image is not None
        assert image.url == "img-800"

    variables, headers = seen[0]
    assert variables["width"] == 800
    assert headers["X-Test"] == "y"


# Three positions, three behaviours. `$width` is nullable with no default, so
# it behaves exactly like an operation variable of `execute`: required keyword,
# `None` sent as an explicit null. `$height` fills a non-null position that
# declares a default, and `$pad` a nullable one that declares a default --
# whether the declared type needed relaxing is not the question a caller asks,
# so both may be left out, and leaving either out has to reach the server as an
# absent variable so the schema's default applies.
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
    url(width: Int, height: Int! = 10, pad: Int = 7): String!
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
            url(width: $width, height: $height, pad: $pad)
        }
        """
    )

    bound = get_attachment.bind(attachment=image_parts)
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
) -> str:
    return f"{width}-{height}-{pad}"


async def test_only_the_defaulted_fragment_variable_may_be_omitted(
    httpserver: HTTPServer,
):
    # `$height` and `$pad` carry a Python default and drop out of the payload
    # when left at None, so the schema's own `= 10` and `= 7` apply; `$width`
    # has no Python default and its None crosses the wire as a real null.
    # Answering "may this be left out?" with "was the declared type relaxed?"
    # made `$pad` -- nullable already, so never relaxed -- a required keyword
    # whose None was sent as an explicit null, putting the schema's `= 7`
    # out of reach.
    async with gql_server(
        httpserver,
        "bindings_fragment_var_nullability",
        {
            "Query": {"post": _resolve_nullability_post},
            "ImageAttachment": {"url": _resolve_sized_url},
        },
    ):
        bound = nullability_queries.bound.with_args(width=None)
        result = await bound.execute(id="1")
        assert result.post is not None
        image = nullability_queries.image_parts.read(result.post.attachment)
        assert image is not None
        assert image.url == "None-10-7"

        sized = nullability_queries.bound.with_args(width=5, height=20, pad=1)
        result = await sized.execute(id="1")
        assert result.post is not None
        image = nullability_queries.image_parts.read(result.post.attachment)
        assert image is not None
        assert image.url == "5-20-1"

        # Each call states the whole set: `height`/`pad` left out of this one
        # go back to the schema's defaults instead of carrying over from the
        # call above, which is the only way "leave it out" stays sayable.
        again = sized.with_args(width=5)
        result = await again.execute(id="1")
        assert result.post is not None
        image = nullability_queries.image_parts.read(result.post.attachment)
        assert image is not None
        assert image.url == "5-10-7"


async def test_a_defaulted_fragment_variable_is_not_required_but_a_nullable_one_is():
    # `required_arg_names__` follows the same split: only `$width` is missing
    # when nothing was passed, and no request is made.
    with pytest.raises(
        ValueError, match=r"^missing fragment variable values \(\$width\)"
    ):
        _ = await nullability_queries.bound.execute(id="1")


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
    # list bind below must not reuse a fragment already bound alone to the
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


def test_undiscovered_bind_combination_raises_lookuperror_at_import(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema=DISCOVERY_SCHEMA,
        queries=_discovery_queries(
            "bound = get_attachment.bind(attachment=image_parts)",
            "both = get_attachment.bind(attachment=[other_image_parts, link_parts])",
        ),
    )
    _ = test_project.generate_and_import()

    # Same fragment/template literal text (still resolvable through the
    # already-generated dispatch dicts) but a *different* bind combination --
    # one the generated package's `_..._GQL_BIND_DISPATCH` was never given.
    # Rewriting queries.py without regenerating and re-importing reproduces
    # exactly the "call site changed, forgot to regenerate" mistake this
    # error exists to catch.
    test_project.write_file(
        test_project.root / f"{test_project.package}/queries.py",
        _discovery_queries("bound = get_attachment.bind(attachment=link_parts)"),
    )
    importlib.invalidate_caches()
    test_project.clear_modules()
    with pytest.raises(LookupError, match="unknown bind combination"):
        importlib.import_module(f"{test_project.package}.queries")


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


def test_bindless_template_imports_cleanly_and_bind_raises_lookuperror(
    test_project: ProjectBuilder,
):
    # `get_highlight` has no `.bind()` anywhere in the package -- generation
    # and import must still succeed (a template is a real, standalone class
    # even with an empty dispatch table). `image_parts` is bind-reachable
    # (bound to the *other* template), so it is a real GQLFragment handle;
    # calling get_highlight.bind() with it still misses the empty dispatch
    # table and must raise, the same LookupError shape as any other unknown
    # combination.
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
    get_highlight = queries_module.get_highlight  # pyright: ignore[reportAny]
    with pytest.raises(LookupError, match="unknown bind combination"):
        get_highlight.bind(highlight=fragment)  # pyright: ignore[reportAny]


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

    bound = get_attachment.bind(attachment=[image_parts, link_parts])
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
    # payload. Offering it as a slot handle validated it against the root and
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
