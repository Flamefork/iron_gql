# The verdict of a mutation run, read from `mutmut results` on stdin.
#
# mutmut 3 exits 0 whatever survives, so `just mutants` and `just mutants-diff`
# pipe its results here. `mutmut results` lists every mutant the tests did not
# kill -- survived, segfaulted, timed out or reached by no test -- one per line
# as `    <name>: <status>`, and an empty listing is the only passing verdict.
#
# The listing covers every mutant `mutants/` holds, including the ones a scoped
# run never touched, so a scoped run passes the globs it ran with. A mutant name
# is the dotted module and the mangled function, and the recipe's glob is a
# module followed by `.*`, which makes the glob without its `*` a name prefix.

import sys


def main(globs: list[str]) -> int:
    prefixes = tuple(glob.removesuffix("*") for glob in globs)
    unkilled = [
        line
        for line in sys.stdin.read().splitlines()
        if line.strip() and (not prefixes or line.strip().startswith(prefixes))
    ]
    if not unkilled:
        return 0
    print("mutants the tests did not kill:")
    print("\n".join(unkilled))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
