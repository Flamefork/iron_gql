import re
from typing import TYPE_CHECKING
from typing import cast

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.codegen.ir import applied_fragment_class_name
from iron_gql.codegen.render import bind_body_fixed_names
from iron_gql.codegen.render import template_execute_fixed_names
from iron_gql.runtime import GQLBoundOperation
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import generated_source
from tests.conftest import gql_server
from tests.conftest import read_type_erased
from tests.conftest import use_package_client

if TYPE_CHECKING:
    from collections.abc import Callable

# Post/Attachment union schema. The same shape tests/test_slots.py uses, kept
# as its own copy: the two files generate separate committed packages and each
# one's schema is free to grow a field the other has no use for.
SCHEMA = """
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
}

type LinkAttachment {
    href: String!
}
"""

QUERIES = '''
from sample_app.gql.api import api_gql

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

get_image_attachment = get_attachment.bind(attachment=image_parts)
'''


def _dispatch_block(generated: str) -> str:
    # Every combination's exec_source now lives as data inside
    # `_API_GQL_BIND_DISPATCH`, not a per-combination `exec_source__ = ...`
    # line -- scoped text search over just this block keeps a raw-statement
    # `@slot`/fragment-text occurrence elsewhere in the module (the literal
    # `api_gql(...)` dispatch keys, which legitimately keep it) from producing
    # a false positive or a false count.
    start = generated.index("_API_GQL_BIND_DISPATCH: dict")
    end = generated.index("\n}", start)
    return generated[start:end]


def _dispatch_entry(generated: str, dispatch_key: str) -> str:
    # One combination's row of that table. Every template is enumerated in
    # full now, so a question about *one* document ("is this definition
    # written once") has to be asked of one row: the same definition
    # legitimately appears in every row whose combination reaches it.
    [line] = [
        line
        for line in _dispatch_block(generated).splitlines()
        if line.strip().startswith(dispatch_key)
    ]
    return line


def _resolve_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {
        "id": id,
        "attachment": {"__typename": "ImageAttachment", "url": "u", "caption": "c"},
    }


async def test_single_fragment_binding_end_to_end(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    async with test_project.server(
        httpserver,
        schema=SCHEMA,
        queries=QUERIES,
        resolvers={"Query": {"post": _resolve_post}},
    ) as (_api, queries):
        result = await queries.get_image_attachment.execute(id="1")  # pyright: ignore[reportAny]
        assert result.post is not None  # pyright: ignore[reportAny]
        image = queries.image_parts.read(result.post.attachment)  # pyright: ignore[reportAny]
        assert image is not None
        assert image.url == "u"  # pyright: ignore[reportAny]


LOCAL_BIND_QUERIES = '''
from sample_app.gql.api import api_gql


async def fetch_attachment(post_id: str, bound):
    return await bound.execute(id=post_id)


async def read_image(post_id: str):
    """A template, a fragment and a bind that never leave the function that
    uses them: nothing here is a module-level name, and the whole thing still
    generates, imports and runs."""
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
    bound = get_attachment.bind(attachment=image_parts)
    result = await fetch_attachment(post_id, bound)
    if result.post is None:
        return None
    return image_parts.read(result.post.attachment)
'''


async def test_a_bind_written_entirely_inside_a_function_runs(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    async with test_project.server(
        httpserver,
        schema=SCHEMA,
        queries=LOCAL_BIND_QUERIES,
        resolvers={"Query": {"post": _resolve_post}},
    ) as (_api, queries):
        image = await queries.read_image("1")  # pyright: ignore[reportAny]
        assert image is not None
        assert image.url == "u"  # pyright: ignore[reportAny]


def test_generated_source_strips_slot_and_renders_binding(test_project: ProjectBuilder):
    test_project.prepare(schema=SCHEMA, queries=QUERIES)
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    # `@slot` legitimately survives elsewhere in the file: the raw template
    # text is still the literal key `api_gql(...)`/`.bind(...)` dispatch on
    # (a bare `get_attachment = api_gql(...)` call must keep resolving to the
    # template object, not raise), and the discovered `.bind()` name resolves
    # through that same text. Only the combination's own exec source -- what
    # `execute()` actually sends the server, carried as data in the bind
    # dispatch table now rather than a per-combination `exec_source__ =` line
    # -- must be free of the directive; mirrors
    # `test_slot_directive_is_stripped_and_split_in_exec_source` in
    # tests/test_slots.py.
    dispatch_key = "('GetAttachment', (('attachment', (ImageParts,)),))"
    [dispatch_line] = [
        line for line in generated.splitlines() if line.strip().startswith(dispatch_key)
    ]
    assert "@slot" not in dispatch_line
    assert "...ImageParts" in dispatch_line
    # The combination is a `bind()` overload now, not a class of its own --
    # and the overload names the *base*, not the fragment class, so a helper
    # that only knows `OnImageAttachment[TModel]` can call it. The phantom it
    # fills the shared bound base's result with carries the base back out
    # together with whatever the concrete definition reads (`TReads`).
    assert (
        "def bind[TModelAttachment: pydantic.BaseModel, TReadsAttachment]"
        "(self, *, attachment: OnImageAttachment[TModelAttachment, "
        "TReadsAttachment]) -> GetAttachmentBound[GetAttachmentResult["
        "OnImageAttachment[TModelAttachment, TReadsAttachment] "
        "| TReadsAttachment]]: ..."
    ) in generated


# A two-slot template with a partial fill: the shapes no other committed
# fixture exercises yet. `partial` leaves `preview` unfilled (pins `Never`
# and the multi-slot overload — only `attachment` is a kwarg); `bare` leaves
# every slot unfilled (pins the all-defaulted overload reachable as a bare
# `bind()`); `both` fills every slot in one call (pins the overload where two
# slots are solved from two independent on-type bases).
SHAPES_SCHEMA = """
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
    url: String!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bindings_shapes",
    schema=SHAPES_SCHEMA,
    queries='''
    from tests.generated.bindings_shapes.gql.api import api_gql

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
            url
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

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {
            href
        }
        """
    )

    # `preview=[]` spelled explicitly rather than omitted: pins the rule at
    # the source level (it resolves to the attachment-only combination,
    # exactly what omitting `preview` also produces -- see
    # test_zero_kwarg_and_partial_bindings_resolve_at_runtime and
    # test_bind_with_explicit_empty_list_matches_omitted_slot below), not
    # just at a runtime call site outside the discovered source.
    partial = get_attachment.bind(attachment=image_parts, preview=[])
    bare = get_attachment.bind()
    # Both slots filled in one call, each through its own on-type base: the
    # slots pick their forms independently, so the product of the per-slot
    # forms already covers this and no overload of its own is needed.
    both = get_attachment.bind(attachment=image_parts, preview=image_parts)
    # "slot given several": a tuple of two disjoint fragments -- the one shape
    # the enumeration does not produce, so a literal bind still has to.
    several = get_attachment.bind(preview=(other_parts, link_parts))
    # `image_parts` is already bound alone to `attachment` above (`partial`,
    # `both`); here it also sits inside a tuple bind of that same slot,
    # alongside a disjoint fragment -- the "registry plus one reader" shape.
    image_and_link = get_attachment.bind(attachment=(image_parts, link_parts))
    # A second tuple bind of the *same* slot, sharing a fragment with the
    # first: two combinations of one arity, overlapping. Written here because
    # a form per combination then admits `(image_parts, image_parts)` twice
    # over, each returning its own phantom, which is `reportOverlappingOverload`
    # on the generated module -- see
    # test_two_tuple_binds_of_one_slot_share_a_form.
    image_and_other = get_attachment.bind(attachment=(image_parts, other_parts))
    # Другая arity означает другой overload namespace. Первые два positional
    # parameters намеренно повторяют TFillAttachment1/2; generated module и этот
    # call site проходят BasedPyright, закрепляя разделение на public typing
    # boundary.
    all_three = get_attachment.bind(attachment=(image_parts, other_parts, link_parts))
    ''',
)

from tests.generated.bindings_shapes import queries as shapes_queries
from tests.generated.bindings_shapes.gql import api as shapes_api

type ObservedReaders = tuple[
    tuple[str, tuple[tuple[type[object], frozenset[str]], ...]], ...
]


def _combination(bound: GQLBoundOperation) -> tuple[str, ObservedReaders]:
    # A bound instance's observable identity now that every combination of
    # one template shares a class (`GetAttachmentBound`): what a
    # per-combination class used to spell in its own name and body is data on
    # the instance instead. Two binds of the same combination -- however each
    # spelled it -- produce equal values here; two different combinations
    # never do, since `exec_source` alone is already unique per combination.
    readers = tuple(
        (
            slot,
            tuple(
                (reader.definition.definition_type, reader.typenames)
                for reader in slot_readers
            ),
        )
        for slot, slot_readers in bound.slot_readers.items()
    )
    return bound.exec_source, readers


def test_unfilled_slot_renders_never_and_the_partial_overload():
    generated = generated_source("bindings_shapes")
    # The unfilled slot's phantom is `Never`, so its node is statically
    # unreadable by any fragment -- the static half of the runtime rule that a
    # definition no bind offered raises instead of returning None. Each slot's
    # node is generic in that slot's own phantom, and the binding fills both
    # in when it names its result -- `Never` for the one it left unfilled.
    node = "GetAttachmentResultPost"
    slots = (("Preview", "TSlotPreview"), ("Attachment", "TSlotAttachment"))
    for slot, param in slots:
        header = f"class {node}{slot}SlotImageAttachment[{param} = Never]"
        assert f"{header}(GQLSlotModel[{param}]):" in generated
    # The combination is a `bind()` overload now, not a class of its own; the
    # overload spans every template slot, not just the one it fills --
    # `preview`, unfilled here, still gets a parameter, typed
    # `Sequence[Never]` with an empty-tuple default so both omitting it and
    # passing `preview=[]` land on this same overload. `attachment` is typed
    # by the on-type base its fragments share, so one overload covers every
    # fragment on `ImageAttachment` and a helper generic over the model can
    # call it.
    assert (
        "def bind[TModelAttachment: pydantic.BaseModel, TReadsAttachment]"
        "(self, *, attachment: OnImageAttachment[TModelAttachment, "
        "TReadsAttachment], preview: Sequence[Never] = ()) "
        "-> GetAttachmentBound[GetAttachmentResult["
        "OnImageAttachment[TModelAttachment, TReadsAttachment] "
        "| TReadsAttachment, Never]]: ..."
    ) in generated
    # The reader table for that same combination, in the bind dispatch dict:
    # entry's own data now, keyed by the same `DispatchKey` the overload's
    # signature type-checks against.
    dispatch_key = "('GetAttachment', (('attachment', (ImageParts,)),))"
    [dispatch_line] = [
        line for line in generated.splitlines() if line.strip().startswith(dispatch_key)
    ]
    assert (
        "{\"attachment\": ((ImageParts, frozenset({'ImageAttachment'})),), "
        '"preview": ()}'
    ) in dispatch_line


def test_all_unfilled_binding_renders_an_all_defaulted_overload():
    # Without an overload every parameter of which is reachable
    # without an argument, an all-unfilled binding is a combination bind()'s
    # typed surface could never return -- once any @overload exists, the
    # untyped `**fragments` implementation signature stops being visible to
    # callers. The union-parameter overload covers this without a dedicated
    # zero-kwarg form: every slot is unfilled, so every parameter defaults to
    # `()`, and `bind()` matches directly.
    generated = generated_source("bindings_shapes")
    assert (
        "def bind(self, *, attachment: Sequence[Never] = (), "
        "preview: Sequence[Never] = ()) "
        "-> GetAttachmentBound[GetAttachmentResult[Never, Never]]: ..."
    ) in generated


def test_zero_kwarg_and_partial_bindings_resolve_at_runtime():
    bare = shapes_queries.get_attachment.bind()
    assert isinstance(bare, shapes_api.GetAttachmentBound)
    assert bare.slot_readers == {"attachment": (), "preview": ()}

    bound = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    assert isinstance(bound, shapes_api.GetAttachmentBound)
    assert [
        reader.definition.fragment_name__ for reader in bound.slot_readers["attachment"]
    ] == ["ImageParts"]
    assert bound.slot_readers["preview"] == ()


def test_bind_with_explicit_empty_list_matches_omitted_slot():
    # `preview=[]` and omitting `preview` entirely are
    # both the documented way to say "no fragments for this slot" and must
    # resolve to the same combination -- and, since the overload now spans
    # every slot (see the test above), both spellings type-check without a
    # `# pyright: ignore`. `queries.py`'s own `partial` bind already writes
    # the `preview=[]` spelling in discovered source (see its comment there);
    # `test_bindings_shapes_queries_module_type_checks` below runs
    # basedpyright over that fixture.
    omitted = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    explicit_empty = shapes_queries.get_attachment.bind(
        attachment=shapes_queries.image_parts,
        preview=[],
    )
    assert _combination(omitted) == _combination(explicit_empty)


def test_bind_with_several_fragments_in_a_tuple_resolves_to_its_own_binding():
    # The fourth spelling of the same rule ("slot given several"): a tuple of
    # two fragments resolves to its own combination, with `attachment` left
    # unfilled.
    several = shapes_queries.get_attachment.bind(
        preview=(shapes_queries.other_parts, shapes_queries.link_parts)
    )
    assert several.slot_readers["attachment"] == ()
    assert {
        reader.definition.fragment_name__ for reader in several.slot_readers["preview"]
    } == {
        "OtherParts",
        "LinkParts",
    }


def test_bind_reusing_a_solo_fragment_inside_a_tuple_resolves_independently():
    # Consequence of lifting the solo/tuple overlap rejection: `image_parts`
    # is bound alone to `attachment` (`shapes_queries.partial`) and also
    # sits inside a tuple bind of that same slot (`image_and_link`) -- the
    # "registry plus one reader" shape. Each resolves to its own combination;
    # one does not affect the other.
    solo = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    registry = shapes_queries.get_attachment.bind(
        attachment=(shapes_queries.image_parts, shapes_queries.link_parts)
    )
    assert _combination(solo) != _combination(registry)
    assert {
        reader.definition.fragment_name__ for reader in solo.slot_readers["attachment"]
    } == {"ImageParts"}
    assert {
        reader.definition.fragment_name__
        for reader in registry.slot_readers["attachment"]
    } == {
        "ImageParts",
        "LinkParts",
    }


def test_two_tuple_binds_of_one_slot_share_a_form():
    # Две combinations одного slot и одной arity — две строки dispatch table,
    # но одна форма signature. Отдельные формы пересеклись бы на
    # `(image_parts, image_parts)`, возвращая разные phantoms. Один constrained
    # form на arity не пересекается с другим form той же arity и сохраняет
    # precision: phantom собирается из constraint каждой фактической позиции.
    source = generated_source("bindings_shapes")
    body = source.split("class GetAttachment(runtime.GQLTemplate):")[1]
    arity_two_signatures = [
        line
        for line in body.splitlines()
        if "attachment: tuple[TFillAttachment1, TFillAttachment2], preview:" in line
    ]
    assert len(arity_two_signatures) == 4
    assert all(
        "TFillAttachment1: (ImageParts, LinkParts, OtherParts)" in signature
        and "TFillAttachment2: (ImageParts, LinkParts, OtherParts)" in signature
        for signature in arity_two_signatures
    )


def test_bind_spellings_resolve_to_distinct_runtime_combinations():
    runtime_bounds = {
        "omitted": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts
        ),
        "one_fragment": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts, preview=shapes_queries.image_parts
        ),
        "several": shapes_queries.get_attachment.bind(
            preview=(shapes_queries.other_parts, shapes_queries.link_parts)
        ),
        "explicit_empty": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts, preview=[]
        ),
        "bare_call": shapes_queries.get_attachment.bind(),
        "pair_tuple": shapes_queries.get_attachment.bind(
            attachment=(shapes_queries.image_parts, shapes_queries.link_parts)
        ),
    }
    # Every spelling produces the shared `GetAttachmentBound` class, so the
    # runtime half of the agreement is about the combination each bound
    # instance carries, not its class -- the two spellings of one combination
    # (`omitted`/`explicit_empty`) must agree, and every genuinely distinct
    # combination among the rest must disagree with every other.
    assert _combination(runtime_bounds["omitted"]) == _combination(
        runtime_bounds["explicit_empty"]
    )
    distinct = ["omitted", "one_fragment", "several", "bare_call", "pair_tuple"]
    combinations = [_combination(runtime_bounds[name]) for name in distinct]
    for i, left in enumerate(combinations):
        for right in combinations[i + 1 :]:
            assert left != right


# One slot over a union, three fragments: two spread-compatible with it and
# one on a type the slot can never hold. `Album` is reachable from
# `ImageAttachment` so the third fragment is legal GraphQL and still outside
# every slot of the package.
ENUMERATION_SCHEMA = """
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
    album: Album!
}

type LinkAttachment {
    href: String!
}

type Album {
    id: ID!
}
"""

generated_package(
    "enumeration",
    schema=ENUMERATION_SCHEMA,
    queries='''
    from tests.generated.enumeration.gql.api import api_gql

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

    album_summary = api_gql(
        """
        fragment AlbumSummary on Album {
            id
        }
        """
    )
    ''',
)


from tests.generated.enumeration import queries as enumeration_queries


def test_every_compatible_pair_is_generated_without_a_call_site():
    # No queries.py in this package calls .bind() at all: the combinations
    # come from the schema, so a helper may bind a fragment it was handed as a
    # parameter and still find a text.
    source = generated_source("enumeration")
    assert "'ImageParts'" in source
    assert "'LinkParts'" in source
    # the fragment on a type no slot can hold gets a class but no combination
    assert "class AlbumSummary(" in source
    assert "'AlbumSummary'" not in _dispatch_block(source)
    # ...and no overload accepts it either: `OnAlbum` is not among the bases
    # the slot's signature names, which is what rejects an incompatible
    # fragment now that every fragment of the package is typed (its class
    # still derives from `OnAlbum`, which is why this asks the signatures
    # rather than the whole module).
    signatures = [line for line in source.splitlines() if "def bind" in line]
    assert signatures
    assert not any("OnAlbum" in line for line in signatures)


def _many_slot_schema(count: int) -> str:
    aliases = "\n".join(f"    slot{index}: Attachment" for index in range(count))
    return f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
{aliases}
}}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {{
    url: String!
}}

type LinkAttachment {{
    href: String!
}}
"""


def _many_slot_queries(count: int) -> str:
    slots = "\n".join(
        f"                slot{index} @slot {{ __typename }}" for index in range(count)
    )
    return f'''
    from sample_app.gql.api import api_gql

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {{ url }}
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {{ href }}
        """
    )

    many = api_gql(
        """
        query Many($id: ID!) {{
            post(id: $id) {{
                id
{slots}
            }}
        }}
        """
    )
    '''


def test_a_one_element_tuple_reaches_the_bare_runtime_combination():
    # Slot с одним fragment принимается через on-type base этого фрагмента;
    # тот же base нельзя ещё раз представить tuple. One-element tuple называет
    # combination, уже представленный bare fragment, и создал бы второй overload
    # для одного call. Поэтому `slot=(fragment,)` нигде не type-checks:
    # `enumeration` не записывает literal tuple bind,
    # and where one is written the tuple form names its own arity, never a
    # shorter one (`test_bindings_tuple_scope`).
    #
    # Runtime `dispatch_key` normalises the two spellings to one combination,
    # and the second half here is what says the rejection is a
    # narrowing of the *static* surface and not a behaviour change.
    # Reached through an erased reference, the same way test_bind_contract.py
    # reaches `bind`: the question here is what the runtime answers to a call
    # the signatures reject, and writing that call directly would make this
    # very file fail the whole-project type check.
    bind = cast(
        "Callable[..., GQLBoundOperation]", enumeration_queries.get_attachment.bind
    )
    assert _combination(bind(attachment=(enumeration_queries.image_parts,))) == (
        _combination(bind(attachment=enumeration_queries.image_parts))
    )


def test_a_template_whose_product_exceeds_the_limit_is_rejected(
    test_project: ProjectBuilder,
):
    # Six slots over a type two fragments are compatible with: 3**6 = 729,
    # over the 256 the generator will write. The message has to carry the
    # numbers -- "too many" without them leaves no way to see which slot to
    # drop.
    test_project.prepare(schema=_many_slot_schema(6), queries=_many_slot_queries(6))
    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.generate()
    assert "729 combinations" in str(exc.value)
    assert "6 slots" in str(exc.value)


def test_literal_tuple_combinations_are_part_of_the_same_limit(
    test_project: ProjectBuilder,
):
    fragment_count = 23
    fragments = "\n".join(
        "\n".join([
            "",
            f'fragment_{index} = api_gql("""',
            f"fragment Fragment{index} on Image {{",
            "    value",
            "}",
            '""")',
        ])
        for index in range(fragment_count)
    )
    pair_binds = "\n".join(
        f"pair_{left}_{right} = template.bind(item=(fragment_{left}, fragment_{right}))"
        for left in range(fragment_count)
        for right in range(left + 1, fragment_count)
    )
    test_project.prepare(
        schema="""
            type Query { image: Image }
            type Image { value: String! }
        """,
        queries=(
            "from sample_app.gql.api import api_gql\n\n"
            'template = api_gql("""\n'
            "query Many {\n"
            "    image @slot { __typename }\n"
            "}\n"
            '""")\n'
            f"{fragments}\n\n"
            f"{pair_binds}\n"
        ),
    )

    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "277 combinations" in message
    assert "253 literal-only" in message


def test_a_template_one_slot_under_the_limit_still_generates(
    test_project: ProjectBuilder,
):
    # The other side of the same boundary, which the rejection alone cannot
    # pin: five slots are 3**5 = 243 texts, and those the generator does
    # write. Without this a limit of 1 would pass the test above.
    test_project.prepare(schema=_many_slot_schema(5), queries=_many_slot_queries(5))
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert _dispatch_block(generated).count("('Many', ") == 243


CONDITIONAL_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

interface Attachment {
    id: ID!
}

type ImageAttachment implements Attachment {
    id: ID!
    url: String!
    related: Attachment
}

type LinkAttachment implements Attachment {
    id: ID!
    href: String!
}
"""

_CONDITIONAL_QUERIES = """
from sample_app.gql.api import api_gql

brick = api_gql(
    '''
    fragment Brick on Attachment { id }
    '''
)

outer = api_gql(
    '''
    fragment Outer on ImageAttachment { OUTER_BODY }
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

bound = get_attachment.bind(attachment=(outer, brick))
"""


def _conditional_queries(outer_body: str) -> str:
    return _CONDITIONAL_QUERIES.replace("OUTER_BODY", outer_body)


# What the rejection's own message tells the developer to do, as source they
# could paste. Read by both tests below, so the advice and the proof that it
# can be carried out cannot drift apart.
CONDITIONAL_REMEDIES = {
    "drop-the-directive": "url ...Brick",
    "spread-under-a-field": "url related { ...Brick @include(if: $withId) }",
    "inline-the-fields": "url ... @include(if: $withId) { id }",
}


def test_a_conditional_brick_path_valid_in_a_tuple_is_rejected_alone(
    test_project: ProjectBuilder,
):
    # `Outer` spreads `Brick` under @include; that is legal as long as some
    # other fragment of the same bind reaches `Brick` unconditionally, which
    # is exactly what the literal tuple bind at the bottom of the queries does.
    # Enumeration also produces the combination where `Outer` stands alone --
    # and there the conditional path is the only one, which the rule rejects.
    # The diagnosis has to name the pair, not just the fragment: the developer
    # never wrote this combination.
    test_project.prepare(
        schema=CONDITIONAL_SCHEMA,
        queries=_conditional_queries("url ...Brick @include(if: $withId)"),
    )
    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.generate()
    assert "Outer" in str(exc.value)
    assert "Brick" in str(exc.value)
    # And it has to name a remedy that can still be carried out: "move it into
    # a fragment no binding reaches" was one until combinations came from the
    # schema, and now every fragment on a slot-compatible type is reached.
    assert "no binding reaches" not in str(exc.value)
    assert "drop the directive" in str(exc.value)
    assert "nested under a field" in str(exc.value)
    assert "inline fragment" in str(exc.value)


@pytest.mark.parametrize(
    "outer_body",
    list(CONDITIONAL_REMEDIES.values()),
    ids=list(CONDITIONAL_REMEDIES),
)
def test_every_remedy_the_conditional_diagnosis_names_generates(
    test_project: ProjectBuilder, outer_body: str
):
    # Advice a developer cannot follow is worse than none, and the only way to
    # know it is followable is to follow it. Each of the three shapes the
    # message names, applied to the very package the test above rejects.
    test_project.prepare(
        schema=CONDITIONAL_SCHEMA, queries=_conditional_queries(outer_body)
    )
    assert test_project.generate() is True


def test_two_fragment_variables_mapping_to_one_python_name_are_rejected(
    test_project: ProjectBuilder,
):
    # A factory's `with_args` parameters are its own keyword namespace, which
    # collision naming has to cover the same way `bind()`'s does: `$fooBar`
    # and `$foo_bar` both snake to `foo_bar`, and the generated
    # `def with_args(self, *, foo_bar: ..., foo_bar: ...)` never compiles.
    test_project.prepare(
        schema="""
        type Query {
            post(id: ID!): Post
        }

        type Post {
            id: ID!
            attachment: Attachment
        }

        union Attachment = ImageAttachment | LinkAttachment

        type ImageAttachment {
            url(size: Int, w: Int): String!
        }

        type LinkAttachment {
            href: String!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                url(size: $fooBar, w: $foo_bar)
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
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"Parameter 'foo_bar' of with_args\(\) of fragment 'ImageParts'"
            r".*claimed by"
        ),
    ):
        test_project.generate()


def test_a_fragment_variable_named_args_is_accepted(test_project: ProjectBuilder):
    # `with_args()`'s body binds no local name, so `args` is an ordinary
    # parameter name there. Reserving it anyway rejected a legal GraphQL
    # variable for a shadowing that never happens, and no rename of the
    # *schema's* argument was open to the fragment's author.
    test_project.prepare(
        schema="""
            type Query {
                post(id: ID!): Post
            }

            type Post {
                id: ID!
                attachment: Attachment
            }

            union Attachment = ImageAttachment | LinkAttachment

            type ImageAttachment {
                variants(args: String!): [String!]!
            }

            type LinkAttachment {
                href: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

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

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                variants(args: $args)
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_a_fragments_own_variable_at_two_conflicting_types_is_rejected(
    test_project: ProjectBuilder,
):
    # `$size` disagreeing with itself inside one fragment's own closure --
    # no bind, no second fragment involved -- is exactly the same defect
    # `bindings._variable_type_conflict_error` already catches for a
    # combination's closure; `fragment_own_vars` runs the same check over a
    # single fragment's own closure, ahead of any combination that might
    # reach it.
    test_project.prepare(
        schema="""
        type Query {
            post(id: ID!): Post
        }

        type Post {
            id: ID!
            attachment: Attachment
        }

        union Attachment = ImageAttachment | LinkAttachment

        type ImageAttachment {
            thumbnail(width: Int!): String!
            photos(limit: String!): [String!]!
        }

        type LinkAttachment {
            href: String!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                thumbnail(width: $size)
                photos(limit: $size)
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
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"\$size.*no GraphQL variable declaration type is allowed at every usage"
        ),
    ):
        test_project.generate()


WIDE_TUPLE_ARITY = 8


def _wide_tuple_project(test_project: ProjectBuilder) -> None:
    fields = "\n".join(f"    f{i}: String!" for i in range(WIDE_TUPLE_ARITY))
    fragments = "\n".join(
        f"""
    frag{i} = api_gql(
        '''
        fragment Frag{i} on ImageAttachment {{ f{i} }}
        '''
    )"""
        for i in range(WIDE_TUPLE_ARITY)
    )
    names = ", ".join(f"frag{i}" for i in range(WIDE_TUPLE_ARITY))
    test_project.prepare(
        schema=f"""
            type Query {{
                post(id: ID!): Post
            }}

            type Post {{
                id: ID!
                attachment: Attachment
            }}

            union Attachment = ImageAttachment | LinkAttachment

            type ImageAttachment {{
{fields}
            }}

            type LinkAttachment {{
                href: String!
            }}
        """,
        queries=f"""
    from sample_app.gql.api import api_gql
{fragments}

    get_attachment = api_gql(
        '''
        query GetAttachment($id: ID!) {{
            post(id: $id) {{
                id
                attachment @slot {{ __typename }}
            }}
        }}
        '''
    )

    bound = get_attachment.bind(attachment=({names}))
    """,
    )


def test_a_wide_tuple_bind_stays_importable(test_project: ProjectBuilder):
    # The tuple form is linear in the arity, and has to be. Spelling every
    # ordering out instead was exact -- it refused a repeated fragment -- and
    # factorial: these eight fragments wrote 40 320 tuples into one
    # annotation, a 2.5 MB module CPython then refused to compile at all
    # (`RecursionError` at import), while generation reported success.
    # `MAX_COMBINATIONS_PER_TEMPLATE` never saw it: this slot's own dispatch
    # table holds nine rows.
    _wide_tuple_project(test_project)
    api_module, _queries = test_project.generate_and_import()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    # One form for the arity, one parameter per position -- what a factorial
    # spelling would blow up is the count, so the count is what is pinned.
    assert generated.count("tuple[") == 1
    positions = ", ".join(
        f"TFillAttachment{position}" for position in range(1, WIDE_TUPLE_ARITY + 1)
    )
    assert f"tuple[{positions}]" in generated
    # Each position carries the same constraints -- every fragment written in
    # a tuple of this arity, once each -- so the annotation grows with the
    # square of the arity at worst, never with its factorial.
    constraints = ", ".join(f"Frag{i}" for i in range(WIDE_TUPLE_ARITY))
    [tuple_overload] = [
        line for line in generated.splitlines() if f"tuple[{positions}]" in line
    ]
    assert tuple_overload.count(f"({constraints})") == WIDE_TUPLE_ARITY
    assert api_module.GetAttachment is not None  # pyright: ignore[reportAny]


def test_a_literal_tuple_bind_types_a_factory_by_its_applied_class(
    test_project: ProjectBuilder,
):
    # A factory has no on-type base of its own, so the class a caller of a
    # literal tuple bind actually passes is what `with_args` returns -- the
    # constraints of the tuple overload's positions have to name that class
    # (`applied_class_name`), not the bare factory, or a real call site
    # passing `image_thumbnail.with_args(width=...)` would fail to type-check
    # against the very overload discovering that call site produced.
    test_project.prepare(
        schema="""
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
                thumbnail(width: Int!): String!
            }

            type LinkAttachment {
                href: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        image_url = api_gql(
            '''
            fragment ImageUrl on ImageAttachment {
                url
            }
            '''
        )

        image_thumbnail = api_gql(
            '''
            fragment ImageThumbnail on ImageAttachment {
                thumbnail(width: $width)
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

        bound = get_attachment.bind(attachment=(image_url, image_thumbnail))
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    constraints = "(_ImageThumbnailApplied, ImageUrl)"
    assert f"TFillAttachment1: {constraints}" in generated
    assert f"TFillAttachment2: {constraints}" in generated
    dispatch_key = (
        "('GetAttachment', (('attachment', (ImageUrl, _ImageThumbnailApplied)),))"
    )
    [dispatch_entry] = [
        line for line in generated.splitlines() if line.strip().startswith(dispatch_key)
    ]
    assert "((ImageThumbnail, frozenset({'ImageAttachment'}))," in dispatch_entry
    # The bare factory nowhere among them: it is not a bindable application.
    [tuple_overload] = [
        line
        for line in generated.splitlines()
        if "tuple[TFillAttachment1, TFillAttachment2]" in line
    ]
    assert "ImageThumbnail," not in tuple_overload
    assert "ImageThumbnail)" not in tuple_overload


def test_a_fragment_named_like_another_ones_applied_class_is_rejected(
    test_project: ProjectBuilder,
):
    # `SizedImage`'s own factory generates a private `_SizedImageApplied` class
    # alongside it (`ir.applied_fragment_class_name`) -- a second, unrelated
    # fragment that happens to be named that exact same thing would silently
    # rebind it, the same collision class `naming._fixed_name_claims` already
    # catches for every generated class name.
    test_project.prepare(
        schema="""
            type Query {
                post(id: ID!): Post
            }

            type Post {
                id: ID!
                attachment: Attachment
            }

            union Attachment = ImageAttachment | LinkAttachment

            type ImageAttachment {
                thumbnail(width: Int!): String!
                other: String!
            }

            type LinkAttachment {
                href: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        sized_image = api_gql(
            '''
            fragment SizedImage on ImageAttachment {
                thumbnail(width: $width)
            }
            '''
        )

        colliding = api_gql(
            '''
            fragment _SizedImageApplied on ImageAttachment {
                other
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
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match="Name '_SizedImageApplied' is claimed by"
    ):
        test_project.generate()


def test_a_fragment_may_use_the_old_private_concrete_class_name(
    test_project: ProjectBuilder,
):
    # Public definitions are concrete now, so `ImageParts` no longer reserves
    # a private `_ImageParts` companion name.
    test_project.prepare(
        schema="""
            type Query { image: ImageAttachment }
            type ImageAttachment { url: String!, other: String! }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''fragment ImageParts on ImageAttachment { url }'''
        )
        colliding = api_gql(
            '''fragment _ImageParts on ImageAttachment { other }'''
        )
        get_image = api_gql(
            '''query GetImage { image @slot { __typename } }'''
        )
        """,
    )
    api_module, _queries_module = test_project.generate_and_import()
    module_globals = vars(api_module)
    image_parts_class = cast("type[object]", module_globals["ImageParts"])
    colliding_class = cast("type[object]", module_globals["_ImageParts"])
    assert image_parts_class is not colliding_class


def test_bind_call_matching_no_discovered_binding_raises():
    # The residual the overload surface cannot close: the signatures are the
    # *product* of each slot's own forms, while the texts are the product of
    # the single-fragment forms plus whatever literal binds wrote -- so a call
    # that mixes a base-filled slot with another slot's literal tuple picks a
    # signature for a combination the dispatch table does not hold.
    # `shapes_queries.several` writes `preview=(other_parts, link_parts)` with
    # `attachment` empty; combining that tuple with a filled `attachment`
    # type-checks and raises where it runs.
    with pytest.raises(LookupError, match="regenerate"):
        shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts,
            preview=(shapes_queries.other_parts, shapes_queries.link_parts),
        )


def _resolve_shapes_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {
        "id": id,
        "attachment": {"__typename": "ImageAttachment", "url": "attachment-url"},
        "preview": {"__typename": "ImageAttachment", "url": "preview-url"},
    }


async def test_bare_binding_executes_and_reading_it_via_an_unbound_fragment_raises(
    httpserver: HTTPServer,
):
    # `bare` leaves both slots unfilled; a real round trip must still succeed
    # (no fragment data is requested from the server). Reading a slot back
    # through a fragment that is not part of this binding is not "no value" --
    # `image_parts` was never offered to either slot's validation, so
    # `slot_data__` has no entry for it at all, and that backstop invariant
    # must surface as a loud error, not a silent None.
    async with gql_server(
        httpserver, "bindings_shapes", {"Query": {"post": _resolve_shapes_post}}
    ):
        result = await shapes_queries.bare.execute(id="1")
        assert result.post is not None
        with pytest.raises(ValueError, match="is not part of the binding"):
            read_type_erased(shapes_queries.image_parts, result.post.attachment)
        with pytest.raises(ValueError, match="is not part of the binding"):
            read_type_erased(shapes_queries.image_parts, result.post.preview)


async def test_both_slots_bound_in_one_call_read_independently(
    httpserver: HTTPServer,
):
    # A single `.bind(attachment=..., preview=...)` call fills two different
    # slots of the same template with the same fragment class in one shot --
    # each slot's own reader must come back with that slot's own value, not
    # the other's.
    async with gql_server(
        httpserver, "bindings_shapes", {"Query": {"post": _resolve_shapes_post}}
    ):
        result = await shapes_queries.both.execute(id="1")
        assert result.post is not None
        attachment = shapes_queries.image_parts.read(result.post.attachment)
        preview = shapes_queries.image_parts.read(result.post.preview)
        assert attachment is not None
        assert preview is not None
        assert attachment.url == "attachment-url"
        assert preview.url == "preview-url"


# A real server never omits a non-null field; the broken payload is served as
# a canned response, same technique as test_broken_slot_data_fails_execute in
# tests/test_slots.py.
SHAPES_BROKEN_PREVIEW_BODY = {
    "data": {
        "post": {
            "id": "1",
            "attachment": {
                "__typename": "ImageAttachment",
                "url": "attachment-url",
            },
            "preview": {"__typename": "ImageAttachment"},
        }
    }
}


async def test_unread_slot_of_a_binding_still_validates_eagerly(
    httpserver: HTTPServer,
):
    # `preview`'s payload is missing `url` (required by ImagePartsData), and
    # this test never reads the preview slot. Boundary validation covers every
    # fragment of the binding, including slices nobody reads -- stricter than
    # the old runtime, which only validated a slot's own offered definitions when
    # that slot's own payload was present, and a promise worth pinning: execute()
    # must still raise, not hand back a result whose unread half is broken.
    httpserver.expect_request("/graphql/", method="POST").respond_with_json(
        SHAPES_BROKEN_PREVIEW_BODY
    )
    async with use_package_client("bindings_shapes", httpserver.url_for("/graphql/")):
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _ = await shapes_queries.both.execute(id="1")
    assert exc_info.value.errors()[0]["loc"] == (
        "post",
        "preview",
        "ImageAttachment",
        "url",
    )


# --- Согласованность logical combination и runtime dispatch -----------------


def test_omitted_slot_and_explicit_empty_list_are_one_combination(
    test_project: ProjectBuilder,
):
    # README: "Omitting a slot and passing it an explicit empty list mean the
    # same thing." Two binds spelling it both ways are therefore the *same*
    # combination, and one combination is one dispatch entry -- the two
    # spellings meet in the `DispatchKey` both produce. Runtime key и rendered
    # literal должны одинаково исключать empty slots; discovery сводит эти
    # spellings к одной logical combination раньше.
    test_project.prepare(
        schema=SHAPES_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment { url }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    attachment @slot { __typename }
                    preview @slot { __typename }
                }
            }
            '''
        )

        omitted = get_attachment.bind(attachment=image_parts)
        empty = get_attachment.bind(attachment=image_parts, preview=[])
        """,
    )
    test_project.generate()

    generated = (test_project.root / "sample_app/gql/api.py").read_text(
        encoding="utf-8"
    )
    key = "('GetAttachment', (('attachment', (ImageParts,)),))"
    assert _dispatch_block(generated).count(key) == 1
    # Both spellings also meet the *enumerated* combination of the same shape:
    # the product produces `attachment=ImageParts, preview=nothing` whether or
    # not anybody writes it, so the two call sites add no entry of their own.
    assert (
        _dispatch_block(generated).count("('GetAttachment', ") == 4  # 2 slots x (∅, IP)
    )
    # The entry points at the statements the combination is made of -- the
    # template and the fragment -- because that is what a reader has to edit
    # to change it; nobody wrote the combination itself.
    assert re.search(r"# See: \S+queries\.py:\d+, \S+queries\.py:\d+", generated)


# The names a rendered `bind()` body needs from outside its own parameters,
# checked from both sides. `naming._signature_claims` reserves them by hand,
# and the only thing tying that list to `render`'s emitters is a comment --
# so an over-claim (a legal GraphQL name refused for nothing) and an
# under-claim (a shadowed name reaching the generated module) each get a test.
SLOT_NAME_SCHEMA = """
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
}

type LinkAttachment {
    href: String!
}
"""


def _slot_name_queries(slot: str, *, binds: str) -> str:
    return f"""
    from sample_app.gql.api import api_gql

    image_parts = api_gql(
        '''
        fragment ImageParts on ImageAttachment {{ url }}
        '''
    )

    link_parts = api_gql(
        '''
        fragment LinkParts on LinkAttachment {{ href }}
        '''
    )

    get_attachment = api_gql(
        '''
        query GetAttachment($id: ID!) {{
            post(id: $id) {{
                id
                {slot}: attachment @slot {{ __typename }}
            }}
        }}
        '''
    )

    {binds}
    """


# Every name `bind()`'s namespace already holds, with what holds it. The
# receiver is written by the renderer for every method; the package-spelled
# names are read from where they are declared rather than copied -- a copy
# would keep passing for names the claim list no longer holds, and stay silent
# about the ones it grows. The two classes are this corpus's own, since the
# renderer names them after the template rather than after the package.
CLAIMED_BIND_NAMES = (
    ("self", "the method receiver"),
    *bind_body_fixed_names("api"),
    ("GetAttachmentBound", "the bound base of template 'GetAttachment'"),
    ("GetAttachmentResult", "the result class of template 'GetAttachment'"),
)

# Both shapes `bind()` is rendered in, as the binds a call site writes. The
# answer must not depend on which one a tree happens to produce: it used to,
# because the overloaded form's implementation took `**fragments` and nothing
# could shadow anything through it, so a slot name was legal or not depending
# on how many combinations the tree held.
BIND_FORMS = {
    "inline": "bound = get_attachment.bind({name}=image_parts)",
    "overloaded": (
        "with_image = get_attachment.bind({name}=image_parts)\n"
        "    with_link = get_attachment.bind({name}=link_parts)"
    ),
}


@pytest.mark.parametrize("form", BIND_FORMS.values(), ids=list(BIND_FORMS))
@pytest.mark.parametrize(
    ("name", "origin"), CLAIMED_BIND_NAMES, ids=[name for name, _ in CLAIMED_BIND_NAMES]
)
def test_a_slot_named_after_a_claimed_name_is_rejected(
    test_project: ProjectBuilder, name: str, origin: str, form: str
):
    # A slot whose Python spelling is a name the generated `bind()` already
    # holds has nowhere to go: `slots` would shadow the module the body calls
    # into, the dispatch dict the table the body looks the combination up in,
    # `self` the receiver. Rejection is the only honest answer, and it is the
    # same answer in both forms.
    #
    # Generated with `to_snake_fn` left as the identity, because that is what
    # it takes for a slot to reach the upper-case half of the list: the hook is
    # documented and unconstrained, and assuming its output lower-case is what
    # left `_API_GQL_BIND_DISPATCH` unclaimed while a body called into it.
    #
    # The names a body *binds* are not here, and must not be: they are none.
    # `assert_method_namespaces_are_closed` holds the renderer to that, so a
    # legal GraphQL name never has to be spent on a local.
    test_project.prepare(
        schema=SLOT_NAME_SCHEMA,
        queries=_slot_name_queries(name, binds=form.format(name=name)),
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            rf"Parameter '{name}' of bind\(\) of template 'GetAttachment'"
            rf".*{re.escape(origin)}"
        ),
    ):
        test_project.generate(to_snake_fn=lambda name: name)


# `SLOT_NAME_SCHEMA` with a field argument, so a fragment can declare a
# variable of its own and be rendered as a factory with a `with_args`.
WIDTH_ARG_SCHEMA = SLOT_NAME_SCHEMA.replace("url: String!", "url(width: Int!): String!")


# Ещё два namespace, покрытые тем же правилом: template `execute` читает client,
# cast alias и result class, а factory `with_args` — только свой Applied class.
CLAIMED_TEMPLATE_EXECUTE_NAMES = (
    *template_execute_fixed_names("api"),
    ("GetAttachmentResult", "the result class this execute() validates against"),
)

CLAIMED_WITH_ARGS_NAMES = (
    (applied_fragment_class_name("ImageParts"), "the applied fragment class"),
)


def _variable_name_queries(variable: str) -> str:
    return f"""
    from sample_app.gql.api import api_gql

    image_parts = api_gql(
        '''
        fragment ImageParts on ImageAttachment {{ url }}
        '''
    )

    get_attachment = api_gql(
        '''
        query GetAttachment(${variable}: ID!) {{
            post(id: ${variable}) {{
                id
                attachment @slot {{ __typename }}
            }}
        }}
        '''
    )
    """


def _fragment_variable_queries(variable: str) -> str:
    return f"""
    from sample_app.gql.api import api_gql

    image_parts = api_gql(
        '''
        fragment ImageParts on ImageAttachment {{ url(width: ${variable}) }}
        '''
    )

    get_attachment = api_gql(
        '''
        query GetAttachment($id: ID!) {{
            post(id: $id) {{
                id
                attachment @slot {{ __typename }}
            }}
        }}
        '''
    )
    """


@pytest.mark.parametrize(
    ("name", "origin"),
    CLAIMED_TEMPLATE_EXECUTE_NAMES,
    ids=[name for name, _ in CLAIMED_TEMPLATE_EXECUTE_NAMES],
)
def test_a_template_variable_named_after_a_claimed_name_is_rejected(
    test_project: ProjectBuilder, name: str, origin: str
):
    test_project.prepare(schema=SLOT_NAME_SCHEMA, queries=_variable_name_queries(name))
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            rf"Parameter '{name}' of execute\(\) of template 'GetAttachment'"
            rf".*{re.escape(origin)}"
        ),
    ):
        test_project.generate(to_snake_fn=lambda name: name)


@pytest.mark.parametrize(
    ("name", "origin"),
    CLAIMED_WITH_ARGS_NAMES,
    ids=[name for name, _ in CLAIMED_WITH_ARGS_NAMES],
)
def test_a_fragment_variable_named_after_a_claimed_name_is_rejected(
    test_project: ProjectBuilder, name: str, origin: str
):
    test_project.prepare(
        schema=WIDTH_ARG_SCHEMA, queries=_fragment_variable_queries(name)
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            rf"Parameter '{name}' of with_args\(\) of fragment 'ImageParts'"
            rf".*{re.escape(origin)}"
        ),
    ):
        test_project.generate(to_snake_fn=lambda name: name)


def test_a_bound_fragment_spreading_a_bundled_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # A bind can only use a typed definition from a single-fragment statement, so a
    # fragment that exists only inside a multi-definition bundle is
    # unreachable through a bind's closure -- even though spreading it by name
    # anywhere else is perfectly legal, which is what makes this worth its own
    # diagnosis instead of graphql-core's generic "Unknown fragment". The
    # closure is walked once, in the parser, where that visibility predicate
    # lives; matched on the wording only that check produces.
    test_project.prepare(
        schema=SHAPES_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        bundle = api_gql(
            '''
            fragment Brick on ImageAttachment { url }
            fragment Unused on LinkAttachment { href }
            '''
        )

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment { ...Brick }
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

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"spreads fragment\(s\) Brick in its closure, but they are not",
    ):
        test_project.generate()


def test_fragment_reached_by_both_the_template_and_the_binding_composes(
    test_project: ProjectBuilder,
):
    # The brick shape the README recommends: a template spreads `Common` by
    # name, and a bound fragment's own closure reaches the same `Common`.
    # Both sides pull the definition from one package-wide namespace, so the
    # shared name is the same definition -- appending the closure blindly put
    # two copies into the expanded document and graphql-core rejected it as
    # "there can be only one fragment named 'Common'", pointing at the
    # fragment instead of at the composition.
    test_project.prepare(
        schema=SHAPES_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        common = api_gql(
            '''
            fragment Common on ImageAttachment { url }
            '''
        )

        outer = api_gql(
            '''
            fragment Outer on ImageAttachment { ...Common }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    preview { __typename ... on ImageAttachment { ...Common } }
                    attachment @slot { __typename }
                }
            }
            '''
        )

        bound = get_attachment.bind(attachment=outer)
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    exec_source = _dispatch_entry(
        generated, "('GetAttachment', (('attachment', (Outer,)),))"
    )
    assert exec_source.count("fragment Common on ImageAttachment") == 1
    assert "fragment Outer on ImageAttachment" in exec_source


def test_two_templates_whose_class_names_collide_say_so(test_project: ProjectBuilder):
    # `getPost` and `GetPost` are two operations but one `class_name`. Looking
    # a bind's template up by that derived name answered one template's bind
    # with the other's slots, and the diagnosis named a slot list from a
    # template the developer never bound -- hiding the collision that is the
    # actual problem.
    test_project.prepare(
        schema="""
            type Query {
                post(id: ID!): Post
                other(id: ID!): Post
            }

            type Post {
                id: ID!
                attachment: Attachment
                cover: Attachment
            }

            union Attachment = ImageAttachment | LinkAttachment

            type ImageAttachment {
                url: String!
            }

            type LinkAttachment {
                href: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        lower = api_gql(
            '''
            query getPost($id: ID!) {
                post(id: $id) { id attachment @slot { __typename } }
            }
            '''
        )

        upper = api_gql(
            '''
            query GetPost($id: ID!) {
                other(id: $id) { id cover @slot { __typename } }
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

        first = lower.bind(attachment=image_parts)
        second = upper.bind(cover=image_parts)
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "GetPostResult" in message
    assert "unknown slot" not in message


def test_a_factory_the_template_also_spreads_by_name_is_rejected(
    test_project: ProjectBuilder,
):
    # The composition merge-by-name still lets a variable-free brick serve a
    # static spread and a bind at once (`test_fragment_reached_by_both_the_
    # template_and_the_binding_composes`, unaffected). A fragment carrying its
    # own variable is a different matter now that `with_args` lives on the
    # fragment, not the combination: `$size` here is the template's own
    # operation variable (spread by name under `hero`, so `GetAttachment`
    # could not validate without declaring it) -- but `ImageParts` is a
    # factory regardless of which template happens to name-spread it, and its
    # generated `with_args(size=...)` would let a caller supply a *second*,
    # independent value for the same GraphQL variable, silently overriding
    # whatever `execute`'s own `size` argument set (`bound__` merges every
    # applied fragment's `fragment_args__` into the request's `variables`
    # after the template's own arguments). Before `with_args` moved to the
    # fragment, this combination was fine: `expand_binding` recognized `$size`
    # as already declared by the template and synthesized nothing of its own
    # for it. Now the fragment's own variable-ness is decided independently of
    # any one combination, so this has to be a generation error instead.
    test_project.prepare(
        schema="""
            type Query {
                post(id: ID!): Post
            }

            type Post {
                id: ID!
                hero: ImageAttachment
                attachment: Attachment
            }

            union Attachment = ImageAttachment | LinkAttachment

            type ImageAttachment {
                thumb(size: Int!): String!
            }

            type LinkAttachment {
                href: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                thumb(size: $size)
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!, $size: Int!) {
                post(id: $id) {
                    id
                    hero { ...ImageParts }
                    attachment @slot { __typename }
                }
            }
            '''
        )

        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(r"Fragment 'ImageParts'.*variable \$size.*template 'GetAttachment'"),
    ):
        test_project.generate()


def test_static_factory_spread_without_a_compatible_slot_is_allowed(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="""
            type Query { page: Page }
            type Page {
                hero: ImageAttachment!
                owner: User!
            }
            type ImageAttachment { thumb(size: Int!): String! }
            type User { id: ID! }
        """,
        queries="""
            from sample_app.gql.api import api_gql

            image_parts = api_gql(
                '''
                fragment ImageParts on ImageAttachment {
                    thumb(size: $size)
                }
                '''
            )

            get_page = api_gql(
                '''
                query GetPage($size: Int!) {
                    page {
                        hero { ...ImageParts }
                        owner @slot { __typename }
                    }
                }
                '''
            )
        """,
    )

    assert test_project.generate() is True


def test_static_factory_spread_conflicts_with_a_compatible_transitive_owner(
    test_project: ProjectBuilder,
):
    # InnerParts несовместим со slot User, но OuterParts совместим и владеет
    # всеми variables своего transitive closure. Static spread отдаёт тот же
    # $size в execute, поэтому принятие пакета разрешило бы OuterParts.with_args
    # молча перекрыть execute(size=...).
    test_project.prepare(
        schema="""
            type Query { page: Page }
            type Page {
                hero: ImageAttachment!
                owner: User!
            }
            type User {
                id: ID!
                avatar: ImageAttachment!
            }
            type ImageAttachment { thumb(size: Int!): String! }
        """,
        queries="""
            from sample_app.gql.api import api_gql

            inner_parts = api_gql(
                '''
                fragment InnerParts on ImageAttachment {
                    thumb(size: $size)
                }
                '''
            )

            outer_parts = api_gql(
                '''
                fragment OuterParts on User {
                    avatar { ...InnerParts }
                }
                '''
            )

            get_page = api_gql(
                '''
                query GetPage($size: Int!) {
                    page {
                        hero { ...InnerParts }
                        owner @slot { __typename }
                    }
                }
                '''
            )
        """,
    )

    with pytest.raises(
        GraphQLGenerationError,
        match=(r"Fragment 'OuterParts'.*variable \$size.*template 'GetPage'"),
    ):
        test_project.generate()


def test_factory_with_a_leading_underscore_name_imports(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="""
            type Query { image: ImageAttachment }
            type ImageAttachment { thumb(size: Int!): String! }
        """,
        queries="""
            from sample_app.gql.api import api_gql

            get_image = api_gql(
                '''
                query GetImage {
                    image @slot { __typename }
                }
                '''
            )

            parts_factory = api_gql(
                '''
                fragment _Parts on ImageAttachment {
                    thumb(size: $size)
                }
                '''
            )

            applied = parts_factory.with_args(size=10)
        """,
    )

    test_project.generate_and_import()


def test_factory_variable_named_dunder_class_can_be_applied(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="""
            type Query { image: ImageAttachment }
            type ImageAttachment { thumb(size: Int!): String! }
        """,
        queries="""
            from sample_app.gql.api import api_gql

            get_image = api_gql(
                '''
                query GetImage {
                    image @slot { __typename }
                }
                '''
            )

            parts = api_gql(
                '''
                fragment Parts on ImageAttachment {
                    thumb(size: $__class__)
                }
                '''
            )

            applied = parts.with_args(__class__=10)
        """,
    )

    test_project.generate_and_import()


def test_on_type_bases_come_only_from_actual_fragment_conditions(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="""
            interface Node { id: ID! }
            type Image implements Node { id: ID!, url: String! }
            type User implements Node { id: ID!, name: String! }
            type Query { node: Node }
        """,
        queries="""
            from sample_app.gql.api import api_gql

            on_image = api_gql(
                '''
                query OnImage {
                    node @slot { __typename }
                }
                '''
            )

            user_parts = api_gql(
                '''
                fragment UserParts on User { name }
                '''
            )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class OnUser(slots.GQLBindableFragment" in generated
    assert "class OnImage(slots.GQLBindableFragment" not in generated
