import re
from pathlib import Path

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import basedpyright_errors
from tests.conftest import basedpyright_report
from tests.conftest import generated_package
from tests.conftest import generated_queries_path
from tests.conftest import generated_source
from tests.conftest import gql_server
from tests.conftest import read_type_erased
from tests.conftest import use_package_client
from tests.conftest import write_text

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
    # through that same text. Only the binding's own exec source — what
    # `execute()` actually sends the server — must be free of the directive;
    # mirrors `test_slot_directive_is_stripped_and_split_in_exec_source` in
    # tests/test_slots.py.
    exec_source_lines = [
        line for line in generated.splitlines() if "exec_source__ =" in line
    ]
    assert exec_source_lines, "exec_source__ assignment not found in generated api.py"
    assert all("@slot" not in line for line in exec_source_lines)
    assert "...ImageParts" in generated
    bound_base = "GetAttachmentBound[ImageParts]"
    assert f"class GetAttachmentWithAttachmentImageParts({bound_base}):" in generated


# A two-slot template with a partial fill: the shapes no other committed
# fixture exercises yet. `partial` leaves `preview` unfilled (pins `Never`
# and the multi-slot overload — only `attachment` is a kwarg); `bare` leaves
# every slot unfilled (pins the all-defaulted overload reachable as a bare
# `bind()`); `both` fills every slot in one call, mixing calling conventions
# per slot (pins the union-parameter overload against a mixed call).
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
    # the source level (it resolves to the attachment-only binding, exactly what
    # omitting `preview` also produces -- see
    # test_zero_kwarg_and_partial_bindings_resolve_at_runtime and
    # test_bind_with_explicit_empty_list_matches_omitted_slot below), not
    # just at a runtime call site outside the discovered source.
    partial = get_attachment.bind(attachment=image_parts, preview=[])
    bare = get_attachment.bind()
    # Mixed spelling: `attachment` bare, `preview` as a one-element list --
    # each slot picks its own calling convention independently. Pins the bug
    # the union-parameter overload fixes: the old two-corner (all-bare,
    # all-list) overload pair had no overload this call could match.
    both = get_attachment.bind(attachment=image_parts, preview=[image_parts])
    # "slot given several": a list of two disjoint fragments.
    several = get_attachment.bind(preview=[other_parts, link_parts])
    # `image_parts` is already bound alone to `attachment` above (`partial`,
    # `both`); here it also sits inside a list bind of that same slot,
    # alongside a disjoint fragment -- the "registry plus one reader" shape
    # . The old solo/list overlap rejection forbade this
    # combination only because the two-corner overload encoding made the
    # solo binding's `Sequence[ImageParts]` a subtype of the wider
    # `Sequence[ImageParts | LinkParts]`; the union-parameter overload
    # doesn't have that shape, so the combination is legal.
    image_and_link = get_attachment.bind(attachment=[image_parts, link_parts])
    ''',
)

from tests.generated.bindings_shapes import queries as shapes_queries
from tests.generated.bindings_shapes.gql import api as shapes_api


def test_unfilled_slot_renders_never_and_the_partial_overload():
    generated = generated_source("bindings_shapes")
    # The unfilled slot's phantom is `Never`, so its node is statically
    # unreadable by any fragment -- the static half of the runtime rule that a
    # handle no bind offered raises instead of returning None.
    bound_base = "GetAttachmentBound[ImageParts, Never]"
    partial_class = f"class GetAttachmentWithAttachmentImageParts({bound_base}):"
    assert partial_class in generated
    assert (
        'slot_handles__ = {"attachment": '
        "(slots.SlotHandle(IMAGE_PARTS, frozenset({'ImageAttachment'})),), "
        '"preview": ()}'
    ) in generated
    # The overload spans every template slot, not just
    # the ones this binding fills -- `preview`, unfilled here, still gets a
    # parameter, typed `Sequence[Never]` with an empty-tuple default so both
    # omitting it and passing `preview=[]` land on this same overload.
    # `attachment`, filled by exactly one fragment, accepts both calling
    # conventions as a union rather than a dedicated bare-only overload.
    assert (
        "def bind(self, *, attachment: ImageParts | Sequence[ImageParts], "
        "preview: Sequence[Never] = ()) "
        "-> GetAttachmentWithAttachmentImageParts: ..."
    ) in generated


def test_all_unfilled_binding_renders_an_all_defaulted_overload():
    # Without an overload every parameter of which is reachable
    # without an argument, an all-unfilled binding is a real, importable
    # class that bind()'s typed surface can never return — once any
    # @overload exists, the untyped `**fragments` implementation signature
    # stops being visible to callers. The union-parameter overload covers
    # this without a dedicated zero-kwarg form: every slot is unfilled, so
    # every parameter defaults to `()`, and `bind()` matches directly.
    generated = generated_source("bindings_shapes")
    assert (
        "class GetAttachmentWithNothing(GetAttachmentBound[Never, Never]):" in generated
    )
    assert (
        "def bind(self, *, attachment: Sequence[Never] = (), "
        "preview: Sequence[Never] = ()) -> GetAttachmentWithNothing: ..."
    ) in generated


def test_zero_kwarg_and_partial_bindings_resolve_at_runtime():
    assert isinstance(
        shapes_queries.get_attachment.bind(), shapes_api.GetAttachmentWithNothing
    )
    bound = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    assert isinstance(bound, shapes_api.GetAttachmentWithAttachmentImageParts)


def test_bind_with_explicit_empty_list_matches_omitted_slot():
    # `preview=[]` and omitting `preview` entirely are
    # both the documented way to say "no fragments for this slot" and must
    # resolve to the same binding -- and, since the overload now spans every
    # slot (see the test above), both spellings type-check to `Partial`
    # without a `# pyright: ignore`. `queries.py`'s own `partial` bind
    # already writes the `preview=[]` spelling in discovered source (see its
    # comment there); `test_bindings_shapes_queries_module_type_checks`
    # below runs basedpyright over that fixture.
    omitted = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    explicit_empty = shapes_queries.get_attachment.bind(
        attachment=shapes_queries.image_parts,
        preview=[],
    )
    assert isinstance(omitted, shapes_api.GetAttachmentWithAttachmentImageParts)
    assert isinstance(explicit_empty, shapes_api.GetAttachmentWithAttachmentImageParts)


def test_bind_with_several_fragments_in_a_list_resolves_to_its_own_binding():
    # The fourth spelling of the same rule ("slot given several"): a list of
    # two fragments resolves to its own binding, with `attachment` left
    # unfilled.
    several = shapes_queries.get_attachment.bind(
        preview=[shapes_queries.other_parts, shapes_queries.link_parts]
    )
    assert isinstance(several, shapes_api.GetAttachmentWithPreviewLinkPartsOtherParts)


def test_bind_reusing_a_solo_fragment_inside_a_list_resolves_independently():
    # Consequence of lifting the solo/list overlap rejection: `image_parts`
    # is bound alone to `attachment` (`shapes_queries.partial`) and also
    # sits inside a list bind of that same slot (`image_and_link`) -- the
    # "registry plus one reader" shape . Each resolves to
    # its own binding class; one does not affect the other.
    solo = shapes_queries.get_attachment.bind(attachment=shapes_queries.image_parts)
    registry = shapes_queries.get_attachment.bind(
        attachment=[shapes_queries.image_parts, shapes_queries.link_parts]
    )
    assert isinstance(solo, shapes_api.GetAttachmentWithAttachmentImageParts)
    assert isinstance(
        registry, shapes_api.GetAttachmentWithAttachmentImagePartsLinkParts
    )


def test_bindings_shapes_queries_module_type_checks():
    # Finding 1's own reproduction: basedpyright rejected the developer's own
    # queries.py once a template had both a partial and an all-unfilled
    # binding, because the generated `bind()` had no overload an all-unfilled
    # call could match. Runs against the committed fixture file directly, so
    # `just lint`'s whole-project basedpyright run also covers it. The fixture
    # also carries a mixed-spelling bind (`both`, see its comment) -- this is
    # this bug's own reproduction: the old two-corner (all-bare, all-list)
    # overload pair had no overload a mixed call could match.
    errors = basedpyright_errors(generated_queries_path("bindings_shapes"))
    assert errors == [], f"expected no type errors, got: {errors}"


def test_bind_spellings_agree_between_static_and_runtime_types(tmp_path: Path):
    # The union-parameter overload's core claim: every spelling bind()
    # accepts must resolve to the same binding class basedpyright infers
    # statically as the one bind() actually returns at runtime. Six calling
    # conventions on the same two-slot template, matching the spellings named
    # in the design's overload-surface fix: a slot omitted; a slot filled by
    # one handle (both slots here, spelled bare); several handles; an
    # explicit `[]`; a bare `bind()`; and a call that mixes conventions
    # across its two slots. `one_handle` and `mixed` are the same
    # combination spelled two different ways -- both must land on `Both`.
    check_file = tmp_path / "check_bind_spellings.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_shapes import queries

            omitted = queries.get_attachment.bind(attachment=queries.image_parts)
            reveal_type(omitted)

            one_handle = queries.get_attachment.bind(
                attachment=queries.image_parts, preview=queries.image_parts
            )
            reveal_type(one_handle)

            several = queries.get_attachment.bind(
                preview=[queries.other_parts, queries.link_parts]
            )
            reveal_type(several)

            explicit_empty = queries.get_attachment.bind(
                attachment=queries.image_parts, preview=[]
            )
            reveal_type(explicit_empty)

            bare_call = queries.get_attachment.bind()
            reveal_type(bare_call)

            mixed = queries.get_attachment.bind(
                attachment=queries.image_parts, preview=[queries.image_parts]
            )
            reveal_type(mixed)
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == [], f"expected no type errors, got: {errors}"
    infos = [d for d in diagnostics if d.severity == "information"]
    expected_static = {
        "omitted": "GetAttachmentWithAttachmentImageParts",
        "one_handle": "GetAttachmentWithAttachmentImagePartsWithPreviewImageParts",
        "several": "GetAttachmentWithPreviewLinkPartsOtherParts",
        "explicit_empty": "GetAttachmentWithAttachmentImageParts",
        "bare_call": "GetAttachmentWithNothing",
        "mixed": "GetAttachmentWithAttachmentImagePartsWithPreviewImageParts",
    }
    assert [info.message for info in infos] == [
        f'Type of "{name}" is "{cls}"' for name, cls in expected_static.items()
    ]

    runtime_classes = {
        "omitted": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts
        ),
        "one_handle": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts, preview=shapes_queries.image_parts
        ),
        "several": shapes_queries.get_attachment.bind(
            preview=[shapes_queries.other_parts, shapes_queries.link_parts]
        ),
        "explicit_empty": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts, preview=[]
        ),
        "bare_call": shapes_queries.get_attachment.bind(),
        "mixed": shapes_queries.get_attachment.bind(
            attachment=shapes_queries.image_parts, preview=[shapes_queries.image_parts]
        ),
    }
    assert {
        name: type(bound).__name__ for name, bound in runtime_classes.items()
    } == expected_static


def test_catch_all_overload_keeps_its_union_in_a_package_with_templates(
    tmp_path: Path,
):
    # A statement whose text is not a literal falls to `api_gql`'s catch-all
    # overload. That overload widens by exactly the kinds the package can
    # return -- operation, fragment handle, template -- and never collapses to
    # `object`: a template elsewhere in the package must not strip the typing
    # off every dynamic call in it. `bindings_shapes` has all three kinds.
    check_file = tmp_path / "check_catch_all.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_shapes.gql.api import api_gql


            def dynamic(text: str) -> None:
                reveal_type(api_gql(text))
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == [], f"expected no type errors, got: {errors}"
    [info] = [d for d in diagnostics if d.severity == "information"]
    assert info.message == (
        'Type of "api_gql(text)" is "GQLOperation | GQLFragment[BaseModel] '
        '| GQLTemplate"'
    )


def test_bind_call_matching_no_discovered_binding_type_checks_but_raises(
    tmp_path: Path,
):
    # The residual the union-parameter overload cannot close: types cannot
    # count list elements, so a list bind using a strict subset of another
    # list bind's fragments type-checks against that wider binding's
    # overload, even though no discovered bind produced this exact
    # combination. `shapes_queries.several` binds `preview` to
    # [other_parts, link_parts]; a bind of `preview` to `[other_parts]`
    # alone statically resolves to `Several` (the only overload its type is
    # assignable to) but has no entry in the runtime dispatch table.
    check_file = tmp_path / "check_subset_bind.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_shapes import queries

            subset = queries.get_attachment.bind(preview=[queries.other_parts])
            reveal_type(subset)
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == [], f"expected no type errors, got: {errors}"
    [info] = [d for d in diagnostics if d.severity == "information"]
    assert info.message == (
        'Type of "subset" is "GetAttachmentWithPreviewLinkPartsOtherParts"'
    )

    with pytest.raises(LookupError, match="regenerate"):
        shapes_queries.get_attachment.bind(preview=[shapes_queries.other_parts])


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
    # the old runtime, which only validated a slot's own offered handles when
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


# --- Rules the three copies of the bind key used to disagree about ----------


def test_omitted_slot_and_explicit_empty_list_are_one_combination(
    test_project: ProjectBuilder,
):
    # README: "Omitting a slot and passing it an explicit empty list mean the
    # same thing." Two binds spelling it both ways are therefore the *same*
    # combination, and one combination is one class -- the two spellings meet
    # in the name, which is derived from the canonical key both produce. The
    # three copies of that key (runtime, rendered dispatch literal, discovery)
    # used to disagree about empty slots, which left a second class importable
    # but unreachable through `.bind()`, behind a duplicate overload.
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
    bound_base = "GetAttachmentBound[ImageParts, Never]"
    partial_class = f"class GetAttachmentWithAttachmentImageParts({bound_base}):"
    assert generated.count(partial_class) == 1
    # Both call sites are recorded on the one class, so a reader of the
    # generated module can still find every place the combination is written.
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


def test_a_slot_named_after_the_slots_module_is_rejected(
    test_project: ProjectBuilder,
):
    # One binding, so `bind()` is rendered as a real body that reads
    # `slots.bind_key(...)` -- a `slots` parameter would shadow the module and
    # the generated call would fail at runtime with an AttributeError.
    test_project.prepare(
        schema=SLOT_NAME_SCHEMA,
        queries=_slot_name_queries(
            "slots", binds="bound = get_attachment.bind(slots=image_parts)"
        ),
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"Parameter 'slots' of bind\(\) of template 'GetAttachment'"
            r".*the iron_gql slots module"
        ),
    ):
        test_project.generate()


def test_a_slot_named_after_the_slots_module_is_accepted_with_two_bindings(
    test_project: ProjectBuilder,
):
    # The other side of the same claim: with two bindings the slots are
    # parameters of `@overload` stubs whose body is `...`, over an
    # implementation taking only `**fragments`, so nothing reads `slots` from
    # a scope a parameter could shadow -- and refusing the name here would be
    # a legal GraphQL name rejected for nothing.
    test_project.prepare(
        schema=SLOT_NAME_SCHEMA,
        queries=_slot_name_queries(
            "slots",
            binds=(
                "with_image = get_attachment.bind(slots=image_parts)\n"
                "    with_link = get_attachment.bind(slots=link_parts)"
            ),
        ),
    )
    _api, queries = test_project.generate_and_import()

    with_image: object = queries.with_image  # pyright: ignore[reportAny]
    with_link: object = queries.with_link  # pyright: ignore[reportAny]
    assert type(with_image).__name__ == "GetAttachmentWithSlotsImageParts"
    assert type(with_link).__name__ == "GetAttachmentWithSlotsLinkParts"


def test_a_slot_named_msg_is_accepted(test_project: ProjectBuilder):
    # `bind()`'s body used to build its LookupError message in a local named
    # `msg`, and the claim list reserved that name -- refusing a slot called
    # `msg` even though a parameter shadowing a local the body assigns before
    # reading breaks nothing. The message is one expression now, and the claim
    # is gone with it.
    test_project.prepare(
        schema=SLOT_NAME_SCHEMA,
        queries=_slot_name_queries(
            "msg", binds="bound = get_attachment.bind(msg=image_parts)"
        ),
    )
    _api, queries = test_project.generate_and_import()

    bound: object = queries.bound  # pyright: ignore[reportAny]
    assert type(bound).__name__ == "GetAttachmentWithMsgImageParts"


def test_two_fragment_variables_mapping_to_one_python_name_are_rejected(
    test_project: ProjectBuilder,
):
    # A binding's synthesized fragment variables become `with_args`'
    # parameters -- their own keyword namespace, which no rule covered:
    # `$fooBar` and `$foo_bar` both snake to `foo_bar` and the generated
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

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"Parameter 'foo_bar' of with_args\(\) of binding "
            r"'GetAttachmentWithAttachmentImageParts'.*claimed by"
        ),
    ):
        test_project.generate()


def test_a_bound_fragment_spreading_a_bundled_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # A bind can only make a handle of a single-fragment statement, so a
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
    [exec_source] = [
        line
        for line in generated.splitlines()
        if line.strip().startswith("exec_source__ = ")
    ]
    assert exec_source.count("fragment Common on ImageAttachment") == 1
    assert "fragment Outer on ImageAttachment" in exec_source


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

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "def with_args(self, *, args: str) -> Self:" in generated


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


def test_two_binds_whose_slot_and_fragment_names_split_the_same_letters_collide(
    test_project: ProjectBuilder,
):
    # A binding's class name concatenates its slot's Pascal name with its
    # fragments' class names, with no separator between them -- so a list
    # bind naming fragments "Ab" and "Cd" together produces the same string as
    # a solo bind naming one fragment "AbCd". Two different combinations, one
    # class name: the README lists this as a generation error, fixed by
    # renaming a fragment or aliasing the slot.
    test_project.prepare(
        schema=SCHEMA,
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

        ab = api_gql(
            '''
            fragment Ab on ImageAttachment {
                url
            }
            '''
        )

        cd = api_gql(
            '''
            fragment Cd on ImageAttachment {
                caption
            }
            '''
        )

        abcd = api_gql(
            '''
            fragment AbCd on ImageAttachment {
                url
                caption
            }
            '''
        )

        split = get_attachment.bind(attachment=[ab, cd])
        joined = get_attachment.bind(attachment=abcd)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="alias the slot field"):
        test_project.generate()


def test_a_fragment_the_template_also_spreads_can_be_bound(
    test_project: ProjectBuilder,
):
    # The composition merge-by-name exists so one brick can serve a static
    # spread and a bind at once. With a variable in that brick the two halves
    # disagreed: the variable was synthesized as the binding's own and then
    # reported as colliding with the template's declaration of the same name,
    # which is the declaration it actually refers to.
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

        bound = get_attachment.bind(attachment=image_parts)
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    # `$size` stays the template's own variable: supplied through `execute`,
    # and the binding grows no `with_args` of its own.
    assert "async def execute(self, *, id: builtins.str, size: int)" in generated
    assert "def with_args(" not in generated
    exec_source = next(
        line for line in generated.splitlines() if "exec_source__ =" in line
    )
    assert exec_source.count("fragment ImageParts on ImageAttachment") == 1
