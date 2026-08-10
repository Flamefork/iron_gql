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
test +args="":
    uv run pytest -vv --color=yes --showlocals --cov=iron_gql '{{ args }}'

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
mutants:
    uv run mutmut run

# The same, scoped to what a branch actually changed: minutes rather than
# hours, and the question it answers is "did this change weaken a check".
#
# Scoped by mutant name rather than by path: mutmut 3 takes the files to
# mutate from its config alone, and the only scope its CLI accepts is a list
# of mutant names matched as globs (`mutmut/utils/format_utils.py` spells one
# as the dotted module, `src.` stripped, then the mangled function). An empty
# list means "every mutant" to mutmut, so a diff that touches no source file
# has to stop here rather than start a full run under a recipe that promises
# a scoped one.
mutants-diff:
    #!/usr/bin/env bash
    set -euo pipefail
    names=$(git diff main --name-only -- 'src/**/*.py' | sed -e 's#^src/##' -e 's#\.py$##' -e 's#/#.#g' -e 's#$#.*#')
    if [ -z "$names" ]; then
        echo "no changed files under src/: nothing to mutate"
        exit 0
    fi
    echo "mutating: $names"
    uv run mutmut run $names

# The scoping fuzzer: scope trees composed at random and judged by the same
# oracle the committed corpus uses. Minutes rather than seconds, and its
# verdict is a survey rather than pass/fail, so it sits here beside the
# mutation recipes. What it finds is minimised into `tests/corpus/scoping.py`,
# where the snapshot holds it in six seconds on every run.
fuzz-scoping examples="3000":
    uv run python -m tests.fuzz_scoping {{ examples }}
