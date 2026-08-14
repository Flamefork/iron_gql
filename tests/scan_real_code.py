import sysconfig
from pathlib import Path

from iron_gql.codegen.discovery import discover_package

SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"])


def assert_real_code_package(package: Path) -> None:
    found = discover_package(package, "api_gql", skip_path=package / "unused.py")
    if found.binds:
        msg = f"{package.name}: discovery ошибочно нашёл GraphQL binding"
        raise AssertionError(msg)
    written = sum(
        source.read_text(encoding="utf-8", errors="ignore").count(".bind(")
        for source in package.rglob("*.py")
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
