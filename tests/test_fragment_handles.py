import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pydantic
import pytest

from iron_gql import runtime
from iron_gql import slots
from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package

SCHEMA = """
type Query {
    node(id: ID!): Node
    viewer: User!
}

interface Node {
    id: ID!
}

type User implements Node {
    id: ID!
    name: String!
}

type Admin implements Node {
    id: ID!
    permissions: [String!]!
}
"""

generated_package(
    "fragment_handles",
    schema=SCHEMA,
    queries='''
    from tests.generated.fragment_handles.gql.api import api_gql

    user_fields = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }
        """
    )

    node_fields = api_gql(
        """
        fragment NodeFields on Node {
            __typename
            id
            ... on Admin { permissions }
        }
        """
    )

    combined = api_gql(
        """
        fragment ViewerFields on User {
            name
        }

        query GetViewer {
            viewer {
                id
                ...ViewerFields
            }
        }
        """
    )

    with_slot = api_gql(
        """
        query WithSlot($id: ID!) {
            node(id: $id) @slot { __typename }
        }
        """
    )
    ''',
)

from tests.generated.fragment_handles import queries as handle_queries
from tests.generated.fragment_handles.gql import api


def test_fragment_statement_returns_a_handle_singleton():
    assert isinstance(handle_queries.user_fields, api.UserFields)
    assert handle_queries.user_fields is api.USER_FIELDS


def test_handle_carries_fragment_metadata():
    handle = handle_queries.user_fields
    assert handle.fragment_name__ == "UserFields"
    assert handle.covered_typenames__ == frozenset({"User"})
    assert handle.fragment_def__ == "fragment UserFields on User {\n  id\n  name\n}"


def test_handle_validates_its_own_selection():
    data = api.USER_FIELDS.validate__({"id": "u-1", "name": "Alice"})
    assert isinstance(data, api.UserFieldsData)
    assert data.name == "Alice"


def test_handle_model_ignores_other_readers_fields():
    # The payload a handle validates carries the other passed fragments'
    # fields next to its own; the model keeps exactly its own selection.
    data = api.USER_FIELDS.validate__({
        "id": "u-1",
        "name": "Alice",
        "email": "alice@example.com",
    })
    assert data.model_dump() == {"id": "u-1", "name": "Alice"}


def test_interface_fragment_covers_every_possible_type():
    # Every possible type is covered, and each variant model sees only its own
    # selection: `permissions` belongs to the Admin inline fragment and is not
    # a field of the User variant.
    handle = handle_queries.node_fields
    assert handle.covered_typenames__ == frozenset({"Admin", "User"})
    user = api.NODE_FIELDS.validate__({
        "__typename": "User",
        "id": "u-1",
        "permissions": ["root"],
    })
    assert "permissions" not in user.model_dump()


def test_interface_fragment_model_is_a_discriminated_union():
    admin = api.NODE_FIELDS.validate__({
        "__typename": "Admin",
        "id": "a-1",
        "permissions": ["root"],
    })
    assert isinstance(admin, pydantic.BaseModel)
    assert admin.model_dump(by_alias=True) == {
        "__typename": "Admin",
        "id": "a-1",
        "permissions": ["root"],
    }

    user = api.NODE_FIELDS.validate__({
        "__typename": "User",
        "id": "u-1",
    })
    assert isinstance(user, pydantic.BaseModel)
    assert user.model_dump(by_alias=True) == {"__typename": "User", "id": "u-1"}


def test_statement_with_an_operation_is_not_a_handle():
    assert isinstance(handle_queries.combined, api.GetViewer)
    assert not isinstance(handle_queries.combined, slots.GQLFragment)
    assert not hasattr(api, "ViewerFields")


def test_package_without_operations_passes_fragments_through(
    test_project: ProjectBuilder,
):
    # No operations means no slots, so nothing can accept a handle — the
    # statement keeps its pre-slot meaning: spread by name, untyped catch-all.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        only_user = api_gql(
            '''
            fragment OnlyUser on User {
                id
            }
            '''
        )
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.only_user, runtime.GQLOperation)  # pyright: ignore[reportAny]


def test_duplicate_fragment_names_across_statements_are_rejected(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql(
            '''
            fragment UserFields on User {
                id
            }
            '''
        )

        second = api_gql(
            '''
            fragment UserFields on User {
                name
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"^Cannot compile different GraphQL fragments with same name UserFields",
    ):
        test_project.generate()


NESTED_HANDLE_SCHEMA = """
type Query {
    node(id: ID!): Node
    profile(id: ID!): Profile
}

interface Node {
    id: ID!
}

type User implements Node {
    id: ID!
    profile: Profile!
}

type Profile {
    handle: String!
    bio: String!
}
"""

NESTED_HANDLE_STATEMENTS = """
        card = api_gql(
            '''
            fragment Card on User {
                __typename
                id
                profile { handle }
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )
"""


def _nested_model_name(api_module: ModuleType) -> str:
    profile_field = api_module.CardData.model_fields["profile"]  # pyright: ignore[reportAny]
    annotation = profile_field.annotation  # pyright: ignore[reportAny]
    assert isinstance(annotation, type)
    return annotation.__name__


def test_handle_model_names_ignore_unrelated_operations(
    test_project: ProjectBuilder,
):
    # A handle's model names — the root and everything reachable from it —
    # are public API: callers annotate and narrow against them. Adding an
    # unrelated operation that selects the same GraphQL type differently
    # must not move any of them.
    test_project.prepare(
        schema=NESTED_HANDLE_SCHEMA,
        queries=f"""
        from sample_app.gql.api import api_gql
        {NESTED_HANDLE_STATEMENTS}
        """,
    )
    api_module, _queries_module = test_project.generate_and_import()
    baseline = _nested_model_name(api_module)

    test_project.prepare(
        schema=NESTED_HANDLE_SCHEMA,
        queries=f"""
        from sample_app.gql.api import api_gql
        {NESTED_HANDLE_STATEMENTS}
        unrelated = api_gql(
            '''
            query Unrelated {{
                profile(id: "1") {{ bio }}
            }}
            '''
        )
        """,
    )
    api_module, _queries_module = test_project.generate_and_import()
    assert _nested_model_name(api_module) == baseline


def test_local_shadowing_resolves_spreads_local_first(
    test_project: ProjectBuilder,
):
    # The operation defines and spreads its own `Bits`; a different bundle
    # defines a same-named `Bits` that spreads `Extra`, which is ambiguous
    # across statements. The operation never reaches that `Extra`: its spread
    # resolves local-first, the same way `make_validation_doc` builds the
    # document that is validated and sent.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query Q($id: ID!) {
                node(id: $id) { ...Bits }
            }
            fragment Bits on Node { id }
            '''
        )

        other_bundle = api_gql(
            '''
            fragment Bits on Node { ...Extra }
            fragment Extra on Node { id }
            '''
        )

        another_extra = api_gql(
            '''
            fragment Extra on Node { __typename }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_duplicate_fragment_with_different_spelling_returns_the_handle(
    test_project: ProjectBuilder,
):
    # Deduplication compares dedented text, so the same fragment indented
    # differently at two call sites is one handle — but the dispatch dict is
    # keyed by the exact literal, so every spelling must resolve to the
    # singleton rather than fall through to a bare GQLOperation.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql("fragment UserBits on User { id }")
        second = api_gql('''
            fragment UserBits on User { id }
        ''')

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )
        """,
    )
    api_module, queries_module = test_project.generate_and_import()
    assert queries_module.first is api_module.USER_BITS  # pyright: ignore[reportAny]
    assert queries_module.second is api_module.USER_BITS  # pyright: ignore[reportAny]


def test_fragment_pinning_a_generated_model_name_is_rejected(
    test_project: ProjectBuilder,
):
    # Fragment `qResultUser` pins `QResultUserData` — the same raw name the
    # path `q.user.data` generates for its model. The rename map is keyed by
    # name, so it cannot move one while keeping the other; the collision has
    # to be resolved by the developer.
    test_project.prepare(
        schema="""
        type Query {
            user: User
        }

        type User {
            name: String
            data: Data
        }

        type Data {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query q { user { data { id } } }
            '''
        )

        f = api_gql(
            '''
            fragment qResultUser on User { name }
            '''
        )

        s = api_gql(
            '''
            query S { user @slot { __typename } }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="pins model name"):
        test_project.generate()


def test_fragment_named_python_keyword_is_rejected(test_project: ProjectBuilder):
    # `fragment none` is valid GraphQL, but capitalize_first turns it into
    # `class None(...)` — the module must be rejected before it is written,
    # not discovered broken at import.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        f = api_gql(
            '''
            fragment none on User { id }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"'None'.*not a usable Python identifier"
    ):
        test_project.generate()


def test_statically_empty_fragment_is_rejected(test_project: ProjectBuilder):
    # A literal `@skip(if: true)` on every field leaves the fragment model
    # without fields, and a fieldless class renders with an empty body that
    # the generated module cannot even import.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        empty_bits = api_gql(
            '''
            fragment EmptyBits on User {
                id @skip(if: true)
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match="statically empty"):
        test_project.generate()


def test_invalid_standalone_fragment_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        broken = api_gql(
            '''
            fragment UserFields on User {
                id
                nickname
            }
            '''
        )
        """,
    )

    with pytest.raises(GraphQLGenerationError, match="Cannot query field 'nickname'"):
        test_project.generate()


# Minimal typed slice of basedpyright's --outputjson schema: just enough to
# reach severity/rule/message/line without json.loads's `Any` leaking into
# every assertion under this repo's strict basedpyright config.
class _DiagnosticPosition(pydantic.BaseModel):
    line: int


class _DiagnosticRange(pydantic.BaseModel):
    start: _DiagnosticPosition


class _Diagnostic(pydantic.BaseModel):
    severity: str
    message: str
    range: _DiagnosticRange
    rule: str | None = None


class _BasedPyrightReport(pydantic.BaseModel):
    general_diagnostics: list[_Diagnostic] = pydantic.Field(alias="generalDiagnostics")


def test_type_checker_rejects_incompatible_fragment_and_infers_read_type(
    tmp_path: Path,
):
    # The per-type compatibility bases exist only for basedpyright: they are
    # empty marker classes, so no runtime assertion can tell a correct base
    # from a dropped one. This is the only test that can catch that
    # regression class. `slots_multi` has everything needed: AlbumSummary is
    # a fragment on Album, a type outside every slot's possible types in that
    # package, so it never becomes a handle at all — its statement resolves
    # to the untyped catch-all, which no slot kwarg accepts — and AlbumTitle
    # is compatible with both `preview` and `attachment`. The scratch file
    # lives under tmp_path, outside the repo tree, so `just lint`'s
    # whole-project basedpyright run never picks it up.
    check_file = tmp_path / "check_slots.py"
    check_file.write_text(
        textwrap.dedent("""
            from tests.generated.slots_multi import queries


            async def main() -> None:
                await queries.list_posts.execute(
                    preview=queries.album_title,
                    attachment=queries.album_cover,
                    owner=queries.album_summary,
                )
                result = await queries.list_posts.execute(
                    preview=queries.album_title,
                    attachment=queries.album_cover,
                    owner=queries.owner_identity,
                )
                post = result.posts[0]
                title = queries.album_title.read(post.attachment)
                reveal_type(title)
                passthrough = queries.album_summary.with_headers({"x-trace": "1"})
                reveal_type(passthrough)
        """).lstrip("\n"),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "basedpyright", "--outputjson", str(check_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent.parent,
    )
    try:
        report = pydantic.TypeAdapter(_BasedPyrightReport).validate_json(
            completed.stdout
        )
    except pydantic.ValidationError as exc:
        msg = (
            "basedpyright's JSON output no longer matches the expected shape "
            f"(exit code {completed.returncode}): {exc}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
        pytest.fail(msg)
    diagnostics = report.general_diagnostics

    # Half 1: AlbumSummary must be statically rejected as `owner`, pinned
    # to the exact kwarg's line so an unrelated type error can't satisfy it.
    errors = [d for d in diagnostics if d.severity == "error"]
    assert len(errors) == 1, f"expected exactly one type error, got: {diagnostics}"
    rejection = errors[0]
    assert rejection.rule == "reportArgumentType", rejection
    assert rejection.range.start.line == 7, rejection
    assert "GQLOperation" in rejection.message, rejection.message
    assert "OwnerFragment" in rejection.message, rejection.message

    # Half 2: read() must recover AlbumTitle's own model (AlbumTitleData),
    # not the `attachment` slot's discriminated-union model.
    infos = [d for d in diagnostics if d.severity == "information"]
    assert len(infos) == 2, (
        f"expected exactly two reveal_type diagnostics, got: {diagnostics}"
    )
    inference = infos[0]
    assert inference.message == 'Type of "title" is "AlbumTitleData | None"', inference

    # Half 3: a known-but-untyped statement (a fragment no slot accepts) keeps
    # its own Literal overload returning the plain GQLOperation — the handles
    # elsewhere in the package widen only the catch-all, so operation methods
    # like `with_headers` stay available at this call site.
    passthrough = infos[1]
    assert passthrough.message == 'Type of "passthrough" is "GQLOperation"', passthrough


def test_unknown_statement_is_rejected_instead_of_a_bare_operation():
    # A stale generated module fed a statement it does not know used to hand
    # back a bare GQLOperation whose first use failed far from the cause; the
    # dispatch now names the actual problem.
    with pytest.raises(LookupError, match="regenerate the package"):
        api.api_gql("query Unknown { viewer { id } }")


def test_operation_spreading_an_ambiguous_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # The operation defines no local `Common`, so it resolves through the
    # global index where scan order picks the winner — same ambiguity rule as
    # a handle's dependencies.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        one = api_gql(
            '''
            fragment Common on User { name }

            query UseOne {
                viewer { id ...Common }
            }
            '''
        )

        other = api_gql("fragment Common on User { id }")

        use = api_gql(
            '''
            query UseOther {
                viewer { ...Common }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"spreads fragment 'Common', which is defined"
    ):
        test_project.generate()


def test_bundle_statement_returns_the_untyped_catch_all(
    test_project: ProjectBuilder,
):
    # README: a statement bundling several fragments without an operation is
    # the one case that keeps returning the untyped catch-all — its fragments
    # are spread statically by name, and the call sits at module level, so a
    # raise here would break the import of user code.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        bundle = api_gql(
            '''
            fragment A on User { id }

            fragment B on User { name }

            query UseBoth {
                viewer { ...A ...B }
            }
            '''
        )

        fragments_only = api_gql(
            '''
            fragment C on User { id }

            fragment D on User { name }
            '''
        )
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.fragments_only, runtime.GQLOperation)  # pyright: ignore[reportAny]
