format:
    uv run ruff format .
    uv run ruff check . --fix || true

lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright

# `--cov` runs here rather than only in the `coverage` recipe: `fail_under` in
# pyproject is the project's dead-branch gate, and a gate no automatic run
# executes is decoration. CI calls this recipe.
test:
    uv run pytest -vv --color=yes --showlocals --cov=iron_gql

[positional-arguments]
pytest *args:
    uv run pytest -vv --color=yes --showlocals "$@"

coverage:
    rm -rf .coverage/*
    uv run pytest --cov --cov-report=html
    open .coverage/htmlcov/index.html

generate-example:
    uv run python example/generate.py

release version:
    uv version {{ version }}
    git add --all
    git commit --message "Release v{{ version }}"
    git push
    git tag --annotate v{{ version }} --message v{{ version }}
    git push --tags

# Mutation testing: the measure of whether the oracles are strong, which
# coverage cannot answer. Nightly or pre-release, not per-commit -- and only
# meaningful now that `tests/corpus/scoping_outcomes.txt` pins outcomes:
# without it a mutant that refuses everything survives, because refusal is a
# legal answer under the refinement relation.
#
# Reports the mutants left unkilled -- survived, segfaulted, timed out or
# reached by no test -- and fails only when the run itself does: a survivor is
# evidence to look into, not a defect (ADR 0007).
#
# mutmut stops on Ctrl-C and exits 0 over what it ran so far, which would leave
# a partial run to be reported as a whole one; the trap ends the recipe there.
#
# `no_proxy` skips the system proxy lookup an HTTP client makes: on macOS that
# lookup crashes a process forked the way mutmut forks each mutant, and every
# mutant whose tests build a client would read as segfaulted. Only the mutation
# recipes set it, so the plain test run keeps the environment it is given.
mutants:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'echo "mutation run interrupted: incomplete, not reported" >&2; exit 130' INT
    no_proxy='*' NO_PROXY='*' uv run mutmut run
    just _report-unkilled-mutants

# The same, scoped to the files a branch changed: minutes rather than hours.
# The report covers those whole files, so it lists the survivors they already
# held next to any the branch added.
#
# Scoped by mutant name rather than by path: mutmut 3 takes the files to
# mutate from its config alone, and the only scope its CLI accepts is a list
# of mutant names matched as globs (`mutmut/utils/format_utils.py` spells one
# as the dotted module, `src.` stripped, then the mangled function). An empty
# list means "every mutant" to mutmut, so a diff that touches no source file
# has to stop here rather than start a full run under a recipe that promises
# a scoped one. `no_proxy` for the reason given at `mutants`.
mutants-diff:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'echo "mutation run interrupted: incomplete, not reported" >&2; exit 130' INT
    names=$(git diff main --name-only -- 'src/**/*.py' | sed -e 's#^src/##' -e 's#\.py$##' -e 's#/#.#g' -e 's#$#.*#')
    if [ -z "$names" ]; then
        echo "no changed files under src/: nothing to mutate"
        exit 0
    fi
    echo "mutating: $names"
    no_proxy='*' NO_PROXY='*' uv run mutmut run $names
    just _report-unkilled-mutants $names

# The report, read from `mutmut results` by `tests/mutation_report.py`. An
# empty listing reads as every mutant killed, so the listing must come from a
# run: `mutmut results` with no `mutants/` lists nothing and exits 0, and
# `pipefail` catches a `mutmut results` that failed.
_report-unkilled-mutants *scope:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d mutants ]; then
        echo "no mutation results: mutants/ is missing" >&2
        exit 1
    fi
    uv run mutmut results | uv run python -m tests.mutation_report {{ scope }}

# The scoping fuzzer: scope trees composed at random and judged by the same
# oracle the committed corpus uses. Minutes rather than seconds, and its
# verdict is a survey rather than pass/fail, so it sits here beside the
# mutation recipes. What it finds is minimised into `tests/corpus/scoping.py`,
# where the snapshot holds it in six seconds on every run.
fuzz-scoping examples="3000":
    uv run python -m tests.fuzz_scoping {{ examples }}

# A full survey of installed Python code. The main suite checks four
# representative packages, and this recipe keeps the wide external corpus.
scan-real-code:
    uv run python -m tests.scan_real_code
