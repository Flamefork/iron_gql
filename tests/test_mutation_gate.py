import subprocess
import sys
from pathlib import Path

import pytest

# Lines as `mutmut results` (mutmut 3.7.0) printed them for this project: a
# survivor in one module and mutants a scoped run never reached in two others.
RESULTS = """\
    iron_gql.codegen.ir.x_fragment_class_name__mutmut_1: not checked
    iron_gql.codegen.ir.x_applied_fragment_class_name__mutmut_2: not checked
    iron_gql.subscription.x_next_result__mutmut_12: survived
"""


def _gate(results: str, *globs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.mutation_gate", *globs],
        input=results,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent.parent,
    )


def test_an_empty_listing_passes() -> None:
    completed = _gate("")
    assert (completed.returncode, completed.stdout) == (0, "")


def test_any_unkilled_mutant_fails_an_unscoped_run_and_is_listed() -> None:
    completed = _gate(RESULTS)
    assert completed.returncode == 1
    assert completed.stdout == "mutants the tests did not kill:\n" + RESULTS


@pytest.mark.parametrize(
    ("globs", "listed"),
    [
        (("iron_gql.subscription.*",), [2]),
        (("iron_gql.codegen.ir.*",), [0, 1]),
        (("iron_gql.codegen.ir.*", "iron_gql.subscription.*"), [0, 1, 2]),
    ],
)
def test_a_scoped_run_fails_on_the_unkilled_mutants_of_its_modules(
    globs: tuple[str, ...], listed: list[int]
) -> None:
    completed = _gate(RESULTS, *globs)
    lines = RESULTS.splitlines()
    assert completed.returncode == 1
    assert completed.stdout.splitlines()[1:] == [lines[i] for i in listed]


def test_a_scoped_run_ignores_unkilled_mutants_outside_its_modules() -> None:
    # A glob names a whole module: `iron_gql.codegen.i` is not a parent of
    # `iron_gql.codegen.ir`, even though its name starts the other one's.
    completed = _gate(RESULTS, "iron_gql.codegen.util.*", "iron_gql.codegen.i.*")
    assert (completed.returncode, completed.stdout) == (0, "")
