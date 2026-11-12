"""Checking the static scan against the Python calls that actually ran.

`discovery` determines the value in `.bind(...)` by walking the AST, and CPython
by executing. The oracle first runs each corpus case and checks the scan only
where the call actually happened. Reachability is not part of the static walk's
contract.

The scan may be stricter than Python: if the single value cannot be proven
without flow analysis, a loud refusal is allowed. A different answer and a silent
loss of an executed binding are not. The snapshot records where the scan binds a
value and where it refuses, so that a weakening does not go unnoticed.
"""

import sys
from pathlib import Path

from tests.corpus.oracle import Observed
from tests.corpus.oracle import divergences
from tests.corpus.oracle import interpreter_answer
from tests.corpus.oracle import scan_answer
from tests.corpus.scoping import ScopeCase
from tests.corpus.scoping import build_corpus
from tests.corpus.scoping import build_declaration_corpus
from tests.corpus.scoping import build_placement_corpus
from tests.corpus.scoping import handwritten_cases
from tests.corpus.scoping import write_case

CORPUS = [
    *build_corpus(),
    *build_declaration_corpus(),
    *build_placement_corpus(),
    *handwritten_cases(),
]


def _outcome(scan: Observed) -> str:
    # The snapshot records the scan's actual result. Refinement allows a loud
    # refusal, so without the snapshot a move from `binds` to `refuses` would go
    # unnoticed.
    if not scan.binds:
        return "refused" if scan.refusal else "silent"
    templates = sorted({found.template for found in scan.binds.values()})
    return "bound:" + ",".join(templates)


SNAPSHOT = Path(__file__).parent / "corpus" / "scoping_outcomes.txt"


def test_scan_refines_the_interpreter_and_matches_snapshot(tmp_path: Path):
    case_ids = [case.id for case in CORPUS]
    assert len(case_ids) == len(set(case_ids))
    case_inputs = [(case.files, case.module, case.invoke) for case in CORPUS]
    assert len(case_inputs) == len(set(case_inputs))

    expected = [
        line.split(" ", maxsplit=1)
        for line in SNAPSHOT.read_text(encoding="utf-8").splitlines()
    ]

    failures: list[str] = []
    executed: list[tuple[ScopeCase, Path, Observed]] = []
    for index, case in enumerate(CORPUS):
        root = tmp_path / str(index)
        root.mkdir()
        write_case(case, root)
        interpreter = interpreter_answer(root, case.module, case.invoke)
        if case.must_bind and not interpreter.binds:
            source = (root / "app/mod.py").read_text(encoding="utf-8")
            failures.append(
                "\n".join([
                    case.id,
                    f"valid case did not execute the binding ({interpreter.refusal})",
                    "",
                    "app/mod.py:",
                    source,
                ])
            )
        if interpreter.binds:
            executed.append((case, root, interpreter))

    assert failures == [], "\n\n".join(failures)
    cached_case_paths = [
        path for path in sys.path_importer_cache if Path(path).is_relative_to(tmp_path)
    ]
    assert cached_case_paths == []
    assert [case.id for case, _, _ in executed] == [case_id for case_id, _ in expected]

    for (case, root, interpreter), (_, expected_outcome) in zip(
        executed, expected, strict=True
    ):
        scan = scan_answer(root)
        problems = divergences(scan, interpreter)
        if case.must_bind and not scan.binds:
            problems.append(
                f"scan found no binding for valid code (refusal: {scan.refusal})"
            )
        actual_outcome = _outcome(scan)
        if actual_outcome != expected_outcome:
            problems.append(
                f"snapshot expected {expected_outcome!r}, got {actual_outcome!r}"
            )
        if problems:
            source = (root / "app/mod.py").read_text(encoding="utf-8")
            failures.append("\n".join([case.id, *problems, "", "app/mod.py:", source]))

    assert failures == [], "\n\n".join(failures)
