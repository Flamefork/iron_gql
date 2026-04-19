from tests.conftest import ProjectBuilder


def test_source_location_comment_includes_file_and_line(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
    )
    test_project.generate_and_import()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "# See: sample_app/queries.py:3" in generated


def test_source_locations_are_deduplicated_per_query(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
    )
    test_project.write_file(
        test_project.root / "sample_app/other.py",
        """
        from sample_app.gql.api import api_gql

        ping2 = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
    )
    test_project.generate_and_import()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    see_lines = [line for line in generated.splitlines() if "# See:" in line]
    assert len(see_lines) == 1
    assert "sample_app/queries.py:3" in see_lines[0]
    assert "sample_app/other.py:3" in see_lines[0]


def test_operations_sorted_by_source_location(test_project: ProjectBuilder):
    # Operations emitted by file+line to keep diffs stable when unrelated
    # queries move.
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
                pong: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        pong = api_gql('query Pong { pong }')
        """,
    )
    test_project.write_file(
        test_project.root / "sample_app/zzz.py",
        """
        from sample_app.gql.api import api_gql

        ping = api_gql('query Ping { ping }')
        """,
    )
    test_project.generate_and_import()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    pong_pos = generated.index("class Pong(")
    ping_pos = generated.index("class Ping(")
    assert pong_pos < ping_pos
