import sysconfig
from pathlib import Path

from iron_gql.codegen.discovery import discover_package
from iron_gql.codegen.discovery import walk_scanned_tree

SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"])


def assert_real_code_package(package: Path) -> None:
    found = discover_package(package, "api_gql", skip_path=package / "unused.py")
    if found.binds:
        msg = f"{package.name}: discovery ошибочно нашёл GraphQL binding"
        raise AssertionError(msg)
    # Counted over the files the scan itself read, not over `rglob`: the walk
    # refuses to enter some directories, and a count taken over the whole tree
    # would compare one tree's `.bind(` calls with another tree's scan.
    written = sum(
        source.read_text(encoding="utf-8", errors="ignore").count(".bind(")
        for source in walk_scanned_tree(package).files
    )
    if written and not found.ignored:
        msg = f"{package.name}: .bind( не учтён как ignored call"
        raise AssertionError(msg)


def main() -> None:
    packages = sorted(
        path
        for path in SITE_PACKAGES.glob("*/")
        if (path / "__init__.py").exists() and path.name != "iron_gql"
    )
    for package in packages:
        assert_real_code_package(package)


if __name__ == "__main__":
    main()
