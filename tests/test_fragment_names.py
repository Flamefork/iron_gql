import graphql
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server
from tests.corpus.fragment_names import FRAGMENT_NAME_CONFLICTS
from tests.corpus.fragment_names import FragmentNameConflict

# ADR 0006: a fragment name means one definition across the package. The
# schema is the one `tests/corpus/fragment_names.py` is written against.
SCHEMA = """
type Query {
    user: User
}

type User {
    id: ID!
    name: String
}
"""


def _queries(*statements: str, tail: str = "") -> str:
    # One statement per line, the first on line 2 -- see `_place`.
    calls = "\n".join(
        f"s{index} = api_gql({text!r})" for index, text in enumerate(statements)
    )
    return f"from sample_app.gql.api import api_gql\n{calls}\n{tail}"


def _place(index: int) -> str:
    return f"sample_app/queries.py:{index + 2}"


def _conflict(name: str, *places: str) -> str:
    return (
        f"Fragment '{name}' is defined differently at {', '.join(places)}; a "
        "fragment name means one definition across the package -- rename one of "
        "the fragments or make their definitions agree"
    )


# The oracle for a statement the case marks invalid: graphql-core validating it
# on its own, without the unused-fragment rule, since nothing in a statement of
# fragments uses them.
def _graphql_core_diagnoses(text: str) -> str:
    errors = graphql.validate(
        graphql.build_schema(SCHEMA),
        graphql.parse(text),
        rules=[
            rule
            for rule in graphql.specified_rules
            if rule is not graphql.NoUnusedFragmentsRule
        ],
    )
    return "\n".join(str(error) for error in errors)


@pytest.mark.parametrize(
    "case", FRAGMENT_NAME_CONFLICTS, ids=[case.id for case in FRAGMENT_NAME_CONFLICTS]
)
def test_different_definitions_of_one_fragment_name_are_one_diagnosis(
    test_project: ProjectBuilder, case: FragmentNameConflict
):
    test_project.prepare(schema=SCHEMA, queries=_queries(*case.statements))

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    assert exc.value.errors == [
        *(
            f"Invalid GraphQL fragment in {_place(index)}:\n"
            + _graphql_core_diagnoses(case.statements[index])
            for index in case.invalid
        ),
        *(_conflict(name, *map(_place, indices)) for name, indices in case.conflicts),
    ]


NODE_SCHEMA = """
type Query {
    node: Node
}

type Node {
    id: ID!
    name: String
}
"""


@pytest.mark.parametrize(
    ("child", "bundle"),
    [
        pytest.param(1, 2, id="single-first"),
        pytest.param(2, 1, id="several-first"),
    ],
)
def test_a_bound_fragment_cannot_send_another_definition_than_it_reads(
    test_project: ProjectBuilder, child: int, bundle: int
):
    # The fragment's model was collected against one `Child` while the
    # document the binding sent carried the other, so a field read back
    # silently empty.
    statements = [
        "query Find { node @slot { __typename } }",
        "",
        "",
        "fragment Parent on Node { ...Child }",
    ]
    statements[child] = "fragment Child on Node { name }"
    statements[bundle] = (
        "fragment Child on Node { id }\nfragment Sibling on Node { id }"
    )
    test_project.prepare(
        schema=NODE_SCHEMA,
        queries=_queries(*statements, tail="bound = s0.bind(node=s3)\n"),
    )

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    assert exc.value.errors == [_conflict("Child", _place(1), _place(2))]


generated_package(
    "fragment_spellings",
    schema=NODE_SCHEMA,
    queries='''
    from tests.generated.fragment_spellings.gql.api import api_gql

    find = api_gql("query Find { node @slot { __typename } }")

    compact = api_gql("fragment Fields on Node { id name }")

    spelled_out = api_gql(
        """
        # The same fragment, laid out and commented.
        fragment Fields on Node {
            id
            name
        }
        """
    )

    by_compact = find.bind(node=compact)
    by_spelled_out = find.bind(node=spelled_out)
    ''',
)

from tests.generated.fragment_spellings import queries as spellings


async def test_definitions_differing_only_in_layout_and_comments_are_one(
    httpserver: HTTPServer,
):
    def resolve_node(_root: None, _info: GraphQLResolveInfo[object]) -> dict[str, str]:
        return {"id": "n-1", "name": "Ann"}

    async with gql_server(
        httpserver, "fragment_spellings", {"Query": {"node": resolve_node}}
    ):
        result = await spellings.by_compact.execute()
        fields = spellings.compact.read(result.node)
        assert fields is not None
        assert (fields.id, fields.name) == ("n-1", "Ann")
        result = await spellings.by_spelled_out.execute()
        fields = spellings.spelled_out.read(result.node)
        assert fields is not None
        assert (fields.id, fields.name) == ("n-1", "Ann")


def test_a_template_and_a_fragment_it_binds_cannot_define_one_name_differently(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema=NODE_SCHEMA,
        queries=_queries(
            (
                "query Find { node @slot { __typename } again: node { ...Fields } }\n"
                "fragment Fields on Node { name }"
            ),
            "fragment Fields on Node { id }",
            tail="bound = s0.bind(node=s1)\n",
        ),
    )

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    assert exc.value.errors == [_conflict("Fields", _place(0), _place(1))]
