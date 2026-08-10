"""Every committed generated package, type-checked in one pass.

The generator's contract is that its output type-checks under the settings
this repository itself runs (`typeCheckingMode = "all"`), and that contract is
about *every* package, not the handful a test remembered to name. Asking for
the whole directory at once is also what makes it affordable: one process for
all of them instead of one per package.
"""

from pathlib import Path

from tests.conftest import basedpyright_report


def test_every_committed_package_typechecks():
    generated = Path(__file__).parent / "generated"
    packages = list(generated.glob("*/gql/api.py"))
    report = basedpyright_report(generated)
    errors = [
        diagnostic
        for diagnostic in report.general_diagnostics
        if diagnostic.severity == "error"
    ]
    assert errors == [], "\n".join(
        f"{diagnostic.range.start.line + 1}: {diagnostic.message}"
        for diagnostic in errors
    )
    # Silence is only evidence once something was read: every generated module
    # plus the `queries.py` call site beside it.
    assert report.summary.files_analyzed >= 2 * len(packages)
