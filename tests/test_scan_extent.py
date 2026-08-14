import json
from pathlib import Path

import pytest

from iron_gql.codegen import generate_gql_package
from iron_gql.codegen.discovery import DiscoveredPackage
from iron_gql.codegen.discovery import discover_package
from tests.conftest import ProjectBuilder
from tests.conftest import write_text


def _write(root: Path, rel: str, text: str) -> None:
    write_text(root / rel, text)


def _discover(root: Path) -> DiscoveredPackage:
    return discover_package(root, "api_gql", skip_path=root / "gql.py")


def _texts(package: DiscoveredPackage) -> list[str]:
    return [statement.raw_text for statement in package.statements]


# A directory the walk refuses to enter is one that holds another tree: an
# environment's installed packages, a tool's cache, a second checkout. What
# follows pins both halves of that sentence -- what is refused, and what is
# not, since a rule that refused a directory of the project's own source would
# take its statements out of the generated package.


def test_a_statement_inside_a_hidden_directory_is_not_collected(tmp_path: Path):
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(
        tmp_path,
        ".venv/lib/python3.13/site-packages/other/api.py",
        'ghost = api_gql("query Ghost { f }")\n',
    )
    assert _texts(_discover(tmp_path)) == ["query Q { f }"]


def test_an_unparsable_file_inside_a_hidden_directory_does_not_abort_the_scan(
    tmp_path: Path,
):
    # Installed code is written for other interpreters and other Python
    # versions, and the abort on an unparsable file that names `bind` cannot
    # tell one of those from a broken file of ours. Refusing the directory is
    # what keeps a dependency from deciding whether this package generates.
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, ".venv/lib/site-packages/legacy.py", "# bind\ndef broken(\n")
    assert _texts(_discover(tmp_path)) == ["query Q { f }"]


def test_a_third_party_bind_inside_a_hidden_directory_is_not_diagnosed(
    tmp_path: Path,
):
    # `.bind(` is an ordinary method name, and the tree's statement names are
    # pooled to diagnose a call that spells one of them where it cannot read
    # it. Over installed code that pool made a dependency's `tmpl.bind(...)`
    # collide with a template of ours and stop generation.
    _write(
        tmp_path, "app/mod.py", 'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
    )
    _write(tmp_path, ".venv/lib/site-packages/widgets.py", "tmpl.bind(f=(frag,))\n")
    package = _discover(tmp_path)
    assert package.binds == []
    assert _texts(package) == ["query Q { f @slot { __typename } }"]


def test_the_same_third_party_bind_outside_a_hidden_directory_is_diagnosed(
    tmp_path: Path,
):
    # The contrast that gives the test above its meaning: the refusal is what
    # answers the collision, not a change to how a name is resolved.
    _write(
        tmp_path, "app/mod.py", 'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
    )
    _write(tmp_path, "vendor/widgets.py", "tmpl.bind(f=(frag,))\n")
    with pytest.raises(TypeError, match="assigns a gql statement"):
        _discover(tmp_path)


def test_a_statement_inside_a_virtual_environment_is_not_collected(tmp_path: Path):
    # `venv` is a legal package name, so only `pyvenv.cfg` says that this
    # directory is an installation rather than source.
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "venv/pyvenv.cfg", "home = /usr/bin\n")
    _write(tmp_path, "venv/lib/probe.py", 'ghost = api_gql("query Ghost { f }")\n')
    assert _texts(_discover(tmp_path)) == ["query Q { f }"]


def test_a_scan_root_is_never_judged_by_the_rule_that_prunes_its_subdirectories(
    tmp_path: Path,
):
    # A root carries whatever name its caller gave it -- pytest's own
    # `tmp_path` sits under `pytest-of-<user>`, and a scan of installed code is
    # rooted inside `site-packages`. Only directories the walk descends *into*
    # are asked about.
    root = tmp_path / ".workspace" / "not-a-package.d"
    _write(root, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    assert _texts(_discover(root)) == ["query Q { f }"]


def test_a_statement_in_a_directory_no_import_could_name_is_still_collected(
    tmp_path: Path,
):
    # `ops-scripts` is not a module path component, so nothing under it can be
    # imported -- but a script is run, not imported, and the statement it holds
    # belongs in the package all the same.
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "ops-scripts/report.py", 'r = api_gql("query R { f }")\n')
    assert _texts(_discover(tmp_path)) == ["query Q { f }", "query R { f }"]


def test_a_statement_in_a_file_no_import_could_name_is_still_collected(
    tmp_path: Path,
):
    _write(tmp_path, "app/my-script.py", 'q = api_gql("query Q { f }")\n')
    assert _texts(_discover(tmp_path)) == ["query Q { f }"]


def test_a_directory_without_an_init_file_is_walked(tmp_path: Path):
    # Namespace packages have no `__init__.py`, and neither does a tree that is
    # about to get one written into it. Presence of the file is not what the
    # walk descends on.
    _write(tmp_path, "app/nested/mod.py", 'q = api_gql("query Q { f }")\n')
    assert _texts(_discover(tmp_path)) == ["query Q { f }"]


def test_statements_are_returned_in_whole_tree_order(tmp_path: Path):
    # The order every consumer inherits, down to the generated file's diff
    # between two machines. A walk hands a directory's own files over before
    # descending, so `b.py` would come first without a sort over the whole
    # tree -- the file names here are chosen so that difference shows.
    _write(tmp_path, "b.py", 'b = api_gql("query B { f }")\n')
    _write(tmp_path, "app/a.py", 'a = api_gql("query A { f }")\n')
    _write(tmp_path, "app/sub/a.py", 'sub = api_gql("query Sub { f }")\n')
    package = _discover(tmp_path)
    assert [str(statement.file) for statement in package.statements] == [
        "app/a.py",
        "app/sub/a.py",
        "b.py",
    ]


def test_a_symlinked_directory_is_not_followed(tmp_path: Path):
    # Unchanged from the glob this walk replaced: a link can leave the tree
    # entirely, and what it points at is not the caller's source to scan.
    outside = tmp_path / "outside"
    _write(outside, "ghost.py", 'ghost = api_gql("query Ghost { f }")\n')
    root = tmp_path / "root"
    _write(root, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert _texts(_discover(root)) == ["query Q { f }"]


def test_skipped_directories_are_written_to_the_debug_directory(
    test_project: ProjectBuilder,
):
    # A tree left alone on purpose and a tree the scan lost are both absent
    # from `statements`. This artifact is where they stop looking the same,
    # exactly as `ignored_binds.json` is for a `.bind(` call.
    test_project.prepare(
        schema="""
        type Query {
            ping: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql("query Ping { ping }")
        """,
    )
    write_text(test_project.root / ".venv/lib/probe.py", "")
    write_text(test_project.root / "venv/pyvenv.cfg", "home = /usr/bin\n")
    debug_dir = test_project.root / "debug_out"
    generate_gql_package(
        mode="async",
        schema_path=test_project.root / "schema.graphql",
        src_path=test_project.root,
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        debug_path=debug_dir,
    )
    assert json.loads((debug_dir / "skipped_dirs.json").read_text("utf-8")) == [
        {"location": ".venv", "reason": "hidden directory"},
        {"location": "venv", "reason": "virtual environment"},
    ]
