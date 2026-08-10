import ast
import re
import textwrap
from pathlib import Path

from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from tests.conftest import basedpyright_errors
from tests.conftest import generated_package
from tests.conftest import generated_queries_path
from tests.conftest import gql_server

# README code blocks have failed when a reviewer actually ran them through the
# generator -- a solo-bound fragment reused inside a list bind of the same slot
# produced an overlapping-overload basedpyright error, and a fragment used a
# variable it never declared. Mapping README *claims* to tests missed those,
# because no test ran the README's own code. This fixture closes that gap:
# `queries.py` below is copied verbatim from the module-level
# `api_gql`/`.bind()` fenced blocks of README.md's "## Fragment Slots"
# section, so a regression in either the generator or the docs shows up here
# first -- `test_queries_fixture_matches_readme_fragment_slots_section` below
# proves the "copied verbatim" claim itself, instead of just asserting it in
# this comment.

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
    thumbnail(width: Int!): String!
}

type LinkAttachment {
    href: String!
}
"""

# Verbatim from README.md's "## Fragment Slots" section: the template, both
# fragment-definitions blocks, both solo binds, the list-bind fragments and
# bind, and the fragment-variables bind. `with_args(...)` itself is called at
# use time (inside a test), not at module level, matching the README's own
# split between "### Binding fragments to a template" / "### Fragment
# variables" and "### Executing and reading".
QUERIES = '''
from tests.generated.readme_fragment_slots.gql.api import api_gql

get_post_attachment = api_gql("""
    query GetPostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")

image_url = api_gql("""
    fragment ImageUrl on ImageAttachment {
        url
    }
""")

link_url = api_gql("""
    fragment LinkUrl on LinkAttachment {
        href
    }
""")

get_post_attachment_image = get_post_attachment.bind(attachment=image_url)
get_post_attachment_link = get_post_attachment.bind(attachment=link_url)

image_caption = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")

link_summary = api_gql("""
    fragment LinkSummary on LinkAttachment {
        href
    }
""")

get_post_attachment_any = get_post_attachment.bind(
    attachment=[image_caption, link_summary]
)

image_thumbnail = api_gql("""
    fragment ImageThumbnail on ImageAttachment {
        thumbnail(width: $width)
    }
""")

get_post_attachment_thumbnail = get_post_attachment.bind(attachment=image_thumbnail)
'''

generated_package("readme_fragment_slots", schema=SCHEMA, queries=QUERIES)

from tests.generated.readme_fragment_slots import queries as readme_queries


def _resolve_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    if id == "img":
        return {
            "id": id,
            "attachment": {
                "__typename": "ImageAttachment",
                "url": "https://cdn.example/pic.png",
                "caption": "a picture",
            },
        }
    return {
        "id": id,
        "attachment": {
            "__typename": "LinkAttachment",
            "href": "https://example.com/post",
        },
    }


def _resolve_thumbnail(
    _root: dict[str, object], _info: GraphQLResolveInfo, *, width: int
) -> str:
    return f"https://cdn.example/pic-{width}.png"


_RESOLVERS = {
    "Query": {"post": _resolve_post},
    "ImageAttachment": {"thumbnail": _resolve_thumbnail},
}


async def test_readme_solo_binds_execute_and_read(httpserver: HTTPServer):
    async with gql_server(httpserver, "readme_fragment_slots", _RESOLVERS):
        image_result = await readme_queries.get_post_attachment_image.execute(id="img")
        assert image_result.post is not None
        image = readme_queries.image_url.read(image_result.post.attachment)
        assert image is not None
        assert image.url == "https://cdn.example/pic.png"

        link_result = await readme_queries.get_post_attachment_link.execute(id="link")
        assert link_result.post is not None
        link = readme_queries.link_url.read(link_result.post.attachment)
        assert link is not None
        assert link.href == "https://example.com/post"


async def test_readme_list_bind_reads_each_fragment_directly(
    httpserver: HTTPServer,
):
    # The list bind (`get_post_attachment_any`) uses `image_caption`/
    # `link_summary`, not the already-solo-bound `image_url`/`link_url` —
    # pins that the corrected README example actually reads through each
    # fragment's own handle, not only that it type-checks.
    async with gql_server(httpserver, "readme_fragment_slots", _RESOLVERS):
        image_result = await readme_queries.get_post_attachment_any.execute(id="img")
        link_result = await readme_queries.get_post_attachment_any.execute(id="link")
        assert image_result.post is not None
        assert link_result.post is not None
        image_node = image_result.post.attachment
        link_node = link_result.post.attachment

        image = readme_queries.image_caption.read(image_node)
        link = readme_queries.link_summary.read(link_node)
        assert image is not None
        assert link is not None
        assert image.caption == "a picture"
        assert link.href == "https://example.com/post"


async def test_readme_with_args_supplies_the_fragment_variable(httpserver: HTTPServer):
    # Pins the corrected two-statement form: bind
    # first (module level, in `queries.py`), then `.with_args(...)` where the
    # value is known -- here, at call time inside the test.
    async with gql_server(httpserver, "readme_fragment_slots", _RESOLVERS):
        bound = readme_queries.get_post_attachment_thumbnail.with_args(width=800)
        result = await bound.with_headers({"Authorization": "..."}).execute(id="img")
        assert result.post is not None
        thumbnail = readme_queries.image_thumbnail.read(result.post.attachment)
        assert thumbnail is not None
        assert thumbnail.thumbnail == "https://cdn.example/pic-800.png"


def test_readme_queries_module_type_checks():
    # The regression this whole file exists to catch: the list bind
    # (`get_post_attachment_any`) must not overlap the solo binds'
    # `Sequence[...]` overloads, and `with_args` chained after `bind()` in a
    # separate statement must type-check. Runs basedpyright against the
    # committed fixture, so `just lint`'s whole-project run also covers it.
    errors = basedpyright_errors(generated_queries_path("readme_fragment_slots"))
    assert errors == [], f"expected no type errors, got: {errors}"


def _readme_fenced_blocks() -> list[str]:
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    _, _, after = readme.partition("\n## Fragment Slots\n")
    section, _, _ = after.partition("\n## Customization Hooks\n")
    return [
        match.group(1).rstrip("\n")
        for match in re.finditer(r"```python\n(.*?)```", section, re.DOTALL)
    ]


def test_readme_generic_bound_helper_type_checks(tmp_path: Path):
    # README's "Executing and reading" block writes a helper generic over the
    # result -- `GetPostAttachmentBound[TResult]` in, `TResult` out, then a
    # `read` off the returned node, which only type-checks once the caller's
    # binding has put its own result model in. It is the one README snippet
    # that depends on the whole phantom chain holding together, and
    # (unlike the module-level gql/bind blocks) it cannot live in `queries.py`,
    # so it gets checked here instead. Taken from the README itself, so the
    # prose and the pin cannot drift.
    (block,) = [b for b in _readme_fenced_blocks() if "GetPostAttachmentBound[" in b]
    check_file = tmp_path / "check_readme_generic.py"
    check_file.write_text(
        "\n".join([
            "from tests.generated.readme_fragment_slots.gql.api import (",
            "    GetPostAttachmentBound,",
            ")",
            "from tests.generated.readme_fragment_slots.queries import (",
            "    get_post_attachment_image,",
            "    image_url,",
            ")",
            "",
            "",
            # The README's usage lines `await` at module level, which only
            # reads as Python inside a coroutine; nesting the whole block in
            # one changes nothing else about it.
            "async def readme_usage() -> None:",
            textwrap.indent(block, "    "),
            "    _ = (result, image)",
            "",
        ]),
        encoding="utf-8",
    )
    errors = basedpyright_errors(check_file)
    assert errors == [], f"expected no type errors, got: {errors}"


def _is_gql_or_bind_call(stmt: ast.stmt) -> bool:
    # A module-level assignment whose value is a call to `api_gql` or a
    # `.bind(...)` call. Narrower than what `discover_package` accepts --
    # which is any scope and any expression -- because this fixture is a
    # `queries.py` built by concatenating README blocks, and only blocks that
    # stand alone at module level can be concatenated that way. README's
    # usage-example blocks (an `async def`, a bare `await` expression) never
    # match, which is how the extraction below tells "belongs in queries.py"
    # apart from "application code demonstrating how to call the result".
    match stmt:
        case (
            ast.Assign(value=ast.Call() as call)
            | ast.AnnAssign(value=ast.Call() as call)
        ):
            match call.func:
                case ast.Name(id="api_gql"):
                    return True
                case ast.Attribute(attr="bind"):
                    return True
                case _:
                    return False
        case _:
            return False


def _is_source_block(block: str) -> bool:
    body = ast.parse(block).body
    return bool(body) and all(_is_gql_or_bind_call(stmt) for stmt in body)


def _readme_fragment_slots_source_blocks() -> list[str]:
    return [block for block in _readme_fenced_blocks() if _is_source_block(block)]


def test_queries_fixture_matches_readme_fragment_slots_section():
    # Makes the module comment's "copied verbatim" claim true instead of just
    # asserting it: extracts every module-level gql/bind fenced block from
    # README's "## Fragment Slots" section (skipping the usage-example blocks
    # interleaved with them, which don't belong in queries.py) and checks the
    # QUERIES fixture is exactly their concatenation. A regression in either
    # the README prose or this fixture fails here first.
    expected = "\n\n".join(_readme_fragment_slots_source_blocks())
    _import_line, _blank, rest = QUERIES.strip("\n").partition("\n\n")
    assert rest == expected
