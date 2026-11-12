import re

import graphql
import pytest

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.corpus.rejected_inputs import REJECTED_SCHEMAS
from tests.corpus.rejected_inputs import REJECTED_STATEMENTS
from tests.corpus.rejected_inputs import RejectedInput

PING_QUERY = """
from sample_app.gql.api import api_gql
ping = api_gql("query Ping { ping }")
"""


def _prepare_with_schema(test_project: ProjectBuilder, schema: str) -> str:
    test_project.prepare(schema=schema, queries=PING_QUERY)
    return re.escape(str(test_project.root / "schema.graphql"))


# The oracle: graphql-core's own refusal to build and validate the schema,
# which it raises as one TypeError with the diagnoses a blank line apart.
def _graphql_core_diagnoses(schema: str) -> list[str]:
    try:
        graphql.assert_valid_schema(graphql.build_schema(schema))
    except graphql.GraphQLSyntaxError as exc:
        return [exc.message]
    except TypeError as exc:
        return str(exc).split("\n\n")
    msg = f"graphql-core accepts the schema: {schema!r}"
    raise AssertionError(msg)


def test_missing_schema_file_names_the_path(test_project: ProjectBuilder):
    path = _prepare_with_schema(test_project, "type Query { ping: String }")
    (test_project.root / "schema.graphql").unlink()

    with pytest.raises(
        GraphQLGenerationError, match=rf"Schema file {path} does not exist"
    ) as exc:
        test_project.render()
    assert isinstance(exc.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize(
    "case", REJECTED_SCHEMAS, ids=[case.id for case in REJECTED_SCHEMAS]
)
def test_rejected_schema_carries_the_graphql_core_diagnoses(
    test_project: ProjectBuilder, case: RejectedInput
):
    path = _prepare_with_schema(test_project, case.text)

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    diagnoses = [
        re.fullmatch(
            rf"Invalid schema at {path}(?::\d+:\d+(?:, {path}:\d+:\d+)*)?: (.*)",
            error,
        )
        for error in exc.value.errors
    ]
    assert [
        diagnosis.group(1) if diagnosis else None for diagnosis in diagnoses
    ] == _graphql_core_diagnoses(case.text)


def test_schema_syntax_error_names_the_schema_file_and_position(
    test_project: ProjectBuilder,
):
    path = _prepare_with_schema(test_project, "type Query {\n  ping: String\n")

    with pytest.raises(
        GraphQLGenerationError,
        match=rf"Invalid schema at {path}:2:15: Syntax Error: Expected Name",
    ) as exc:
        test_project.render()
    assert isinstance(exc.value.__cause__, graphql.GraphQLSyntaxError)


@pytest.mark.parametrize(
    ("schema", "places"),
    [
        pytest.param("type Query { ping: Missing }", ["1:20"], id="unknown-type"),
        pytest.param(
            "type Query { a: Int }\ntype Query { b: Int }",
            ["1:6", "2:6"],
            id="repeated-type",
        ),
        pytest.param("type Post { title: String }", [], id="no-query-root"),
    ],
)
def test_invalid_schema_names_the_schema_file_and_positions(
    test_project: ProjectBuilder, schema: str, places: list[str]
):
    path = str(test_project.root / "schema.graphql")
    _prepare_with_schema(test_project, schema)

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    [error] = exc.value.errors
    expected = ", ".join(f"{path}:{place}" for place in places) or path
    assert error.startswith(f"Invalid schema at {expected}: ")


@pytest.mark.parametrize(
    "case", REJECTED_STATEMENTS, ids=[case.id for case in REJECTED_STATEMENTS]
)
def test_statement_syntax_error_names_the_call_site_and_the_graphql_core_diagnosis(
    test_project: ProjectBuilder, case: RejectedInput
):
    test_project.prepare(
        schema="type Query { ping: String }",
        queries=f"""
        from sample_app.gql.api import api_gql
        broken = api_gql({case.text!r})
        """,
    )
    with pytest.raises(graphql.GraphQLSyntaxError) as oracle:
        graphql.parse(case.text.strip())
    [location] = oracle.value.locations or []

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    assert exc.value.errors == [
        (
            f"sample_app/queries.py:2: {oracle.value.message} (line "
            f"{location.line}, column {location.column} of the dedented statement)"
        )
    ]
    assert isinstance(exc.value.__cause__, graphql.GraphQLSyntaxError)


def test_statement_syntax_errors_of_several_files_come_in_one_error(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="type Query { ping: String }",
        queries="""
        from sample_app.gql.api import api_gql
        ok = api_gql("query Ok { ping }")
        broken = api_gql("query Broken { ping ")
        """,
    )
    test_project.write_file(
        test_project.root / "sample_app/other.py",
        """
        from sample_app.gql.api import api_gql

        other = api_gql(
            '''
            query Other {
                ping(
            }
            '''
        )
        """,
    )

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    assert len(exc.value.errors) == 2
    assert re.match(
        (
            r"sample_app/other\.py:3: Syntax Error: Expected Name, found '}'\. "
            r"\(line 2, column 10 of the dedented statement\)"
        ),
        exc.value.errors[0],
    )
    assert re.match(
        (
            r"sample_app/queries\.py:3: Syntax Error: Expected Name, found <EOF>\. "
            r"\(line 1, column 20 of the dedented statement\)"
        ),
        exc.value.errors[1],
    )
    cause = exc.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert [type(error) for error in cause.exceptions] == [
        graphql.GraphQLSyntaxError,
        graphql.GraphQLSyntaxError,
    ]


def test_statement_syntax_errors_come_without_the_diagnoses_they_cause(
    test_project: ProjectBuilder,
):
    # Every spread of the fragment that failed to parse would be an unknown
    # fragment, and the invalid field a diagnosis of its own: neither is
    # reported until the syntax is fixed.
    test_project.prepare(
        schema="type Query { ping: String }",
        queries="""
        from sample_app.gql.api import api_gql
        fields = api_gql("fragment Fields on Query { ping ")
        ping = api_gql("query Ping { ...Fields }")
        missing = api_gql("query Missing { absent }")
        """,
    )

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    [error] = exc.value.errors
    assert error.startswith("sample_app/queries.py:2: Syntax Error:")


def test_a_rejected_input_is_still_a_value_error(test_project: ProjectBuilder):
    test_project.prepare(
        schema="type Query { ping: String }",
        queries="""
        from sample_app.gql.api import api_gql
        ping = api_gql(PING)
        """,
    )

    with pytest.raises(ValueError, match="expected a single string literal"):
        test_project.render()


@pytest.mark.parametrize("file", ["schema.graphql", "sample_app/queries.py"])
def test_a_file_that_is_not_utf8_keeps_its_decode_error(
    test_project: ProjectBuilder, file: str
):
    test_project.prepare(schema="type Query { ping: String }", queries=PING_QUERY)
    (test_project.root / file).write_bytes(b"# \xff\n")

    with pytest.raises(UnicodeDecodeError):
        test_project.render()


def test_an_unreadable_schema_file_keeps_its_permission_error(
    test_project: ProjectBuilder,
):
    test_project.prepare(schema="type Query { ping: String }", queries=PING_QUERY)
    schema = test_project.root / "schema.graphql"
    schema.chmod(0)

    try:
        with pytest.raises(PermissionError):
            test_project.render()
    finally:
        schema.chmod(0o644)


def test_writing_the_generated_module_keeps_its_own_error(
    test_project: ProjectBuilder,
):
    test_project.prepare(schema="type Query { ping: String }", queries=PING_QUERY)
    rendered = test_project.render()
    rendered.path.mkdir(parents=True)

    with pytest.raises(IsADirectoryError):
        rendered.write()
