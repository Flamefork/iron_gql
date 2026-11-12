import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Lines as `mutmut results` (mutmut 3.7.0) printed them for this project: a
# survivor in one module and mutants a scoped run never reached in another.
RESULTS = """\
    iron_gql.codegen.ir.x_fragment_class_name__mutmut_1: not checked
    iron_gql.codegen.ir.x_applied_fragment_class_name__mutmut_2: not checked
    iron_gql.subscription.x_next_result__mutmut_12: survived
"""

NOT_CHECKED = """\
not checked: 2
    iron_gql.codegen.ir.x_fragment_class_name__mutmut_1
    iron_gql.codegen.ir.x_applied_fragment_class_name__mutmut_2
"""

SURVIVED = """\
survived: 1
    iron_gql.subscription.x_next_result__mutmut_12
"""

INCOMPLETE = "incomplete run: some mutants in scope were not checked\n"
ALL_KILLED = "every mutant in scope was killed\n"


def _report(results: str, *globs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.mutation_report", *globs],
        input=results,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent.parent,
    )


def test_an_empty_listing_reports_every_mutant_killed() -> None:
    completed = _report("")
    assert (completed.returncode, completed.stdout) == (0, ALL_KILLED)


def test_unkilled_mutants_are_reported_by_status_without_failing() -> None:
    completed = _report(RESULTS)
    assert (completed.returncode, completed.stdout) == (
        0,
        INCOMPLETE + NOT_CHECKED + SURVIVED,
    )


@pytest.mark.parametrize(
    ("globs", "expected"),
    [
        (("iron_gql.subscription.*",), SURVIVED),
        (("iron_gql.codegen.ir.*",), INCOMPLETE + NOT_CHECKED),
        (
            ("iron_gql.codegen.ir.*", "iron_gql.subscription.*"),
            INCOMPLETE + NOT_CHECKED + SURVIVED,
        ),
    ],
)
def test_a_scoped_run_reports_the_unkilled_mutants_of_its_modules(
    globs: tuple[str, ...], expected: str
) -> None:
    completed = _report(RESULTS, *globs)
    assert (completed.returncode, completed.stdout) == (0, expected)


def test_a_scoped_run_ignores_unkilled_mutants_outside_its_modules() -> None:
    # A glob names a whole module: `iron_gql.codegen.i` is not a parent of
    # `iron_gql.codegen.ir`, even though its name starts the other one's.
    completed = _report(RESULTS, "iron_gql.codegen.util.*", "iron_gql.codegen.i.*")
    assert (completed.returncode, completed.stdout) == (0, ALL_KILLED)


ROOT = Path(__file__).parent.parent

# A stand-in for `uv` that logs each call and, as `mutmut run`, stops on
# Ctrl-C and exits 0 the way mutmut 3.7 does, over a run it did not finish.
FAKE_UV = """\
#!/usr/bin/env bash
echo "$*" >> calls.log
if [ "$*" = "run mutmut run" ]; then
    sleep 30 >/dev/null 2>&1 &
    trap 'kill $!; exit 0' INT
    touch started
    wait
fi
"""


def _just(cwd: Path, recipe: str, *, path: str | None = None) -> subprocess.Popen[str]:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
    return subprocess.Popen(
        [
            "just",
            "--justfile",
            str(ROOT / "justfile"),
            "--working-directory",
            str(cwd),
            recipe,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def test_a_report_without_mutation_results_fails(tmp_path: Path) -> None:
    # `mutmut results` with no `mutants/` lists nothing and exits 0, which the
    # report would read as every mutant killed.
    _, stderr = (process := _just(tmp_path, "_report-unkilled-mutants")).communicate()
    assert (process.returncode, stderr.splitlines()[0]) == (
        1,
        "no mutation results: mutants/ is missing",
    )


def test_an_interrupted_run_stops_the_recipe_before_the_report(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    process = _just(tmp_path, "mutants", path=str(bin_dir))
    deadline = time.monotonic() + 10
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline, "the run never started"
        time.sleep(0.01)
    # Ctrl-C reaches the whole foreground process group.
    os.killpg(process.pid, signal.SIGINT)
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == 130
    assert "mutation run interrupted: incomplete, not reported" in stderr.splitlines()
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls == ["run mutmut run"]
