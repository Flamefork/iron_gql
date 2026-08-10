"""The scan, held against the interpreter it models.

`discovery` decides what a `.bind(...)` call site reads by walking the AST;
CPython decides it by running. Where the two disagree the generator writes a
binding for a template the call never touches, and the user gets a
`LookupError` at a line that looks correct.

The relation is refinement, not equality. The scan is allowed to be *stricter*
than Python -- a name bound twice in the scope it resolves in is a hard error
rather than a flow question -- so three outcomes are acceptable for any case:
the same answer the interpreter gives, a loud refusal (a raised diagnosis or a
recorded `IgnoredBind`), or silence where the interpreter never reaches the
call at all. The one forbidden outcome is a *different* answer.

The cases here are a crossing of axes, written to be read. `fuzz_scoping`
composes the same statements into trees at random and judges them with the
same oracle; what it finds is promoted into this corpus, where the snapshot
can see it.
"""

from pathlib import Path

import pytest

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


@pytest.mark.parametrize("case", CORPUS, ids=[case.id for case in CORPUS])
def test_the_scan_refines_the_interpreter(case: ScopeCase, tmp_path: Path):
    write_case(case, tmp_path)
    interpreter = interpreter_answer(tmp_path, case.module, case.invoke)
    scan = scan_answer(tmp_path)
    problems = divergences(scan, interpreter)
    if case.must_bind and not scan.binds:
        problem = "the scan found no binding for a case written as ordinary "
        problem += f"valid code (refusal: {scan.refusal})"
        problems.append(problem)
    if problems:
        source = (tmp_path / "app/mod.py").read_text(encoding="utf-8")
        pytest.fail("\n".join([*problems, "", "app/mod.py:", source]), pytrace=False)


def _outcome(scan: Observed) -> str:
    # One line per case: what the scan *did*, not whether it was allowed to.
    # Refinement is a correctness bound and permits a loud refusal for
    # anything, so a case sliding from "binds" to "refuses" is invisible to it
    # -- which is exactly how a fix for one family silently dropped bindings in
    # another. The snapshot turns every such slide into a diff to explain.
    if not scan.binds:
        return "refused" if scan.refusal else "silent"
    templates = sorted({found.template for found in scan.binds.values()})
    return "bound:" + ",".join(templates)


SNAPSHOT = Path(__file__).parent / "corpus" / "scoping_outcomes.txt"


def test_outcomes_match_the_snapshot(tmp_path: Path):
    lines: list[str] = []
    for index, case in enumerate(CORPUS):
        root = tmp_path / str(index)
        root.mkdir()
        write_case(case, root)
        lines.append(f"{case.id} {_outcome(scan_answer(root))}")
    current = "\n".join(lines) + "\n"
    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(current, encoding="utf-8")
        pytest.fail(f"snapshot created at {SNAPSHOT}; review it and commit")
    assert current == SNAPSHOT.read_text(encoding="utf-8")
