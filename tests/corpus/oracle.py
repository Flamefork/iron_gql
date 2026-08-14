"""Сравнение результата интерпретатора с результатом статического scan.

Модуль общий для committed corpus и `fuzz_scoping`, чтобы оба источника случаев
проверяли один контракт. Интерпретатор определяет, какие `.bind()` действительно
исполнились. Только для них scan обязан дать тот же ответ или громко отказаться:
статический обход не моделирует runtime reachability.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

from iron_gql.codegen.discovery import BindDecl
from iron_gql.codegen.discovery import discover_package
from tests.corpus.recorder import ExecutedBind
from tests.corpus.recorder import record_run


@dataclass(frozen=True, kw_only=True)
class Observed:
    # One side's answer for a whole case: what each call site resolved to, and
    # the loud refusal if there was one. A refusal is data, not an error to
    # swallow -- it is one of the three acceptable outcomes, and it is quoted
    # back in the failure message when it is not.
    binds: dict[str, ExecutedBind]
    refusal: str | None


def interpreter_answer(
    root: Path, module: str, invoke: tuple[str, ...] = ()
) -> Observed:
    with record_run(root) as recording:
        try:
            imported = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            # Whatever the module raises *is* the observation: a call site the
            # import never reaches is one Python does not execute.
            return Observed(binds=dict(recording.binds), refusal=describe(exc))
        if not invoke:
            return Observed(binds=dict(recording.binds), refusal=None)
        try:
            _call(imported, invoke)
        except Exception as exc:  # noqa: BLE001
            return Observed(binds=dict(recording.binds), refusal=describe(exc))
        return Observed(binds=dict(recording.binds), refusal=None)


def _call(module: ModuleType, chain: tuple[str, ...]) -> None:
    # `("go",)` is a module function; `("Holder", "go")` is a method reached
    # through a fresh instance. Attribute access rather than `eval` of a
    # snippet: the corpus decides *which* body runs, not what runs in it.
    match chain:
        case (function,):
            _callable(module, function)()
        case (owner, method):
            _callable(_callable(module, owner)(), method)()
        case _:
            # Internal invariant: `Site.invoke` is written with one or two
            # names, and nothing else builds a chain.
            msg = f"unsupported invocation chain: {chain}"
            raise AssertionError(msg)


def _callable(owner: object, name: str) -> Callable[[], object]:
    # A corpus module is imported by name, so everything reached on it is
    # `Any`. The cast states the contract the corpus writes and the runner
    # relies on: these attributes are defined, and they take no arguments.
    return cast("Callable[[], object]", getattr(owner, name))


def scan_answer(root: Path) -> Observed:
    try:
        package = discover_package(root, "api_gql", skip_path=root / "unused.py")
    except TypeError as exc:
        # The scan's own diagnosis for a call site it will not guess at.
        return Observed(binds={}, refusal=describe(exc))
    binds = {
        location: _as_executed(bind)
        for bind in package.binds
        for location in bind.locations
    }
    ignored = "; ".join(f"{item.location}: {item.reason}" for item in package.ignored)
    return Observed(binds=binds, refusal=ignored or None)


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _as_executed(bind: BindDecl) -> ExecutedBind:
    return ExecutedBind(
        template=bind.template.clean_text,
        slots=tuple(
            sorted(
                (slot, tuple(statement.clean_text for statement in statements))
                for slot, statements in bind.slot_args
            )
        ),
    )


def divergences(scan: Observed, interpreter: Observed) -> list[str]:
    problems: list[str] = []
    for location, executed in interpreter.binds.items():
        found = scan.binds.get(location)
        if found is not None:
            if found == executed:
                continue
            problem = f"{location}: the scan bound {found.template!r} with "
            problem += f"{found.slots}, the interpreter reads "
            problem += f"{executed.template!r} with {executed.slots}"
            problems.append(problem)
            continue
        if scan.refusal is not None:
            # Громкий отказ допустим: scan может быть строже Python там, где
            # статически нельзя доказать единственное значение.
            continue
        problem = f"{location}: the interpreter binds {executed.template!r} "
        problem += "here, and the scan neither found it nor said why"
        problems.append(problem)
    return problems
