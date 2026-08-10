"""The scan, pointed at Python nobody wrote for it.

Every other corpus in this suite is written by us, so every shape in it is a
shape someone imagined. The dev environment holds a few hundred thousand lines
that were not: pydantic, graphql-core, httpx, pytest and their dependencies
use `match`, walrus, PEP 695 and every import form in the wild.

The scan makes three promises about such a tree, and none of them needs a gql
statement to be present: it does not crash, it claims nothing as ours, and
every `.bind(` it walked past is accounted for -- as an ignored call with a
reason, never dropped in silence.
"""

import sysconfig
from pathlib import Path

import pytest

from iron_gql.codegen.discovery import discover_package

SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"])

# One package per case, so a failure names the tree it came from. Ours is
# excluded: it is scanned by every other test in this file's neighbourhood,
# and its `testing` module mentions the very names this scan looks for.
PACKAGES = sorted(
    path
    for path in SITE_PACKAGES.glob("*/")
    if (path / "__init__.py").exists() and path.name != "iron_gql"
)


@pytest.mark.parametrize("package", PACKAGES, ids=[p.name for p in PACKAGES])
def test_real_code_is_walked_without_claiming_anything(package: Path):
    found = discover_package(package, "api_gql", skip_path=package / "unused.py")
    assert found.binds == []
    # `.bind(` is an ordinary method name -- sockets, tkinter widgets and LDAP
    # connections all have one -- so third-party trees are full of them. Each
    # has to come back as a recorded reason rather than vanish, which is the
    # same accounting the generated packages get.
    written = sum(
        source.read_text(encoding="utf-8", errors="ignore").count(".bind(")
        for source in package.rglob("*.py")
    )
    if written:
        assert found.ignored, f"{package.name} writes .bind( but nothing was recorded"
