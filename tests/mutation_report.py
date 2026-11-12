# The report of a mutation run, read from `mutmut results` on stdin.
#
# A surviving mutant is evidence to look into, not a proven defect (ADR 0007),
# so the report never fails: `just mutants` and `just mutants-diff` fail only
# when the run itself does. `mutmut results` lists every mutant the tests did
# not kill, one per line as `    <name>: <status>`; the report groups them by
# status, because a survivor, a timeout and a mutant no test reached ask
# different questions. A mutant left `not checked` in scope means the run did
# not finish, and the report says so rather than passing it for a result.
#
# The listing covers every mutant `mutants/` holds, including the ones a scoped
# run never touched, so a scoped run reports only the globs it ran with. A
# mutant name is the dotted module and the mangled function, and the recipe's
# glob is a module followed by `.*`, which makes the glob without its `*` a name
# prefix.

import sys
from collections import defaultdict


def main(globs: list[str]) -> int:
    prefixes = tuple(glob.removesuffix("*") for glob in globs)
    by_status: dict[str, list[str]] = defaultdict(list)
    for line in sys.stdin.read().splitlines():
        name, _, status = line.strip().rpartition(": ")
        if name and (not prefixes or name.startswith(prefixes)):
            by_status[status].append(name)
    if "not checked" in by_status:
        print("incomplete run: some mutants in scope were not checked")
    if not by_status:
        print("every mutant in scope was killed")
    for status, names in sorted(by_status.items()):
        print(f"{status}: {len(names)}")
        print("\n".join(f"    {name}" for name in names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
