"""A campaign of generated scope trees, judged by the committed oracle.

Not a `test_` module on purpose: a run worth anything is thousands of examples
and tens of seconds, and its verdict is a *survey* rather than a pass/fail --
what the scan answered, what it refused, and which shapes it got wrong. It
runs from `just fuzz-scoping`, beside the mutation recipes and on the same
schedule.

What it finds does not stay here. A divergence is minimised, written into
`corpus/scoping.py` as a case with axes of its own, and from then on it is the
committed corpus and its snapshot that hold the line -- in six seconds, on
every run, which is a promise a random search cannot make.

Two verdicts, one contract:

  divergence   the scan answered differently from CPython, or answered where
               CPython never ran. Refinement forbids exactly this.
  lost-plain   nothing in the module binds `tmpl` a second time and the
               interpreter did reach the call, so a refusal is not the scan
               being careful -- it is a real bind dropped. Refinement alone
               cannot see this, which is why the generator reports what it
               placed rather than only what came out.
"""

import sys
import tempfile
from collections import Counter
from pathlib import Path

from hypothesis import HealthCheck
from hypothesis import Verbosity
from hypothesis import given
from hypothesis import settings

from tests.corpus.oracle import divergences
from tests.corpus.oracle import interpreter_answer
from tests.corpus.oracle import scan_answer
from tests.corpus.scope_tree import Module
from tests.corpus.scope_tree import modules
from tests.corpus.scoping import SUPPORT_FILES

STATS: Counter[str] = Counter()
# One example per class of divergence: a survey reports the shape of the
# problem, not forty copies of whichever one the search hit first.
FOUND: dict[tuple[str, str], tuple[str, str, list[str]]] = {}


def _classify(problem: str) -> str:
    if "never executes this call" in problem:
        return "answered-an-unexecuted-call"
    if "the interpreter reads" in problem:
        return "different-answer"
    if "neither found it nor said why" in problem:
        return "silently-dropped"
    return "lost-plain-bind"


def judge(module: Module) -> list[str]:
    try:
        compile(module.source, "<generated>", "exec")
    except SyntaxError:
        # `compile` is the authority on which compositions are Python, the
        # same way it is in `scoping.build_corpus`.
        STATS["not-python"] += 1
        return []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative, source in SUPPORT_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        (root / "app/mod.py").write_text(module.source, encoding="utf-8")
        interpreter = interpreter_answer(root, "app.mod")
        scan = scan_answer(root)
    problems = divergences(scan, interpreter)
    if problems:
        STATS["divergence"] += 1
        return problems
    if module.plain and interpreter.binds and not scan.binds:
        STATS["lost-plain-bind"] += 1
        return [f"the scan refused ordinary code (refusal: {scan.refusal})"]
    if not interpreter.binds:
        STATS["never-executed"] += 1
    else:
        STATS["bound" if scan.binds else "refused"] += 1
    return []


def run(max_examples: int) -> int:
    @given(modules())
    @settings(
        max_examples=max_examples,
        deadline=None,
        # Each example writes a tree, imports it and scans it, so it is slow
        # and large by Hypothesis's standards on purpose; those are the two
        # health checks that would stop a campaign for doing its job.
        suppress_health_check=(
            HealthCheck.too_slow,
            HealthCheck.data_too_large,
            HealthCheck.large_base_example,
        ),
        verbosity=Verbosity.quiet,
    )
    def campaign(module: Module) -> None:
        problems = judge(module)
        if problems:
            placement = module.summary.split("placement=")[1].split(" ")[0]
            FOUND.setdefault(
                (_classify(problems[0]), placement),
                (module.summary, module.source, problems),
            )

    campaign()
    print(f"examples: {max_examples}")
    print("outcomes:", dict(STATS))
    for (kind, placement), (summary, source, problems) in sorted(FOUND.items()):
        print()
        print("=" * 72)
        print(f"{kind} / placement={placement}")
        print(problems[0])
        print(summary)
        print("-" * 72)
        print(source)
    if FOUND:
        print()
        print(f"{len(FOUND)} class(es) to minimise and promote into corpus/scoping.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1]) if len(sys.argv) > 1 else 3000))
