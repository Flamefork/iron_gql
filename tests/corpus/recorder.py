"""The interpreter side of the discovery oracle.

A stand-in for a generated package's `api_gql` that records what each
`.bind(...)` call site *actually* reads when the module runs. The scan reads
the AST and answers the same question statically; `test_discovery_oracle`
holds the two answers against each other.

Registered in `sys.modules` under the name a corpus module imports it by, so
no file of it ever lands in the scanned tree -- the scan must see exactly the
source the interpreter runs, and nothing else.
"""

import importlib
import inspect
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

MODULE_NAME = "gql_recorder"

# One slot's fragments, then every slot of one call, sorted: the shape both
# sides of the oracle are reduced to before they are compared. Sorted because
# `bind(a=..., b=...)` and `bind(b=..., a=...)` are one call to the runtime
# (see `slots.bind_key_shape`), and the oracle is not the place to relitigate
# that.
type Slots = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, kw_only=True)
class ExecutedBind:
    # What the interpreter read at one call site: which statement the base
    # name held, and which statements its keywords held.
    template: str
    slots: Slots


@dataclass(kw_only=True)
class Recording:
    root: Path
    binds: dict[str, ExecutedBind]


# Set by `record_run` for the duration of one corpus case. Module state rather
# than a parameter because the recorded object has to reach `Statement.bind`
# through the corpus module's own source, which takes no arguments from here.
_current: Recording | None = None


def _location(frame: FrameType) -> str:
    # The scan's own spelling for a call site: path relative to the scanned
    # root, then the line. `f_lineno` is the line being executed, which for the
    # single-line `.bind(...)` calls the corpus emits is the line the AST puts
    # the call on.
    assert _current is not None  # noqa: S101
    path = Path(frame.f_code.co_filename).relative_to(_current.root)
    return f"{path.as_posix()}:{frame.f_lineno}"


class Statement:
    """What `api_gql(...)` returns: a template, a fragment, or neither.

    One class for both roles, exactly as the corpus needs it -- the oracle
    records the text a name held, and what kind of statement that text spells
    is the scan's business, not the interpreter's.
    """

    def __init__(self, text: str) -> None:
        self.text = textwrap.dedent(text).strip()

    def bind(self, **fragments: "Statement | list[Statement]") -> "Statement":
        frame = inspect.currentframe()
        # Internal invariant: CPython gives a Python frame here, and its caller
        # is the corpus line that wrote `.bind(...)`.
        assert frame is not None  # noqa: S101
        assert frame.f_back is not None  # noqa: S101
        assert _current is not None  # noqa: S101
        _current.binds[_location(frame.f_back)] = ExecutedBind(
            template=self.text, slots=_slots(fragments)
        )
        return self


def _slots(fragments: dict[str, "Statement | list[Statement]"]) -> Slots:
    entries: list[tuple[str, tuple[str, ...]]] = []
    for slot, value in fragments.items():
        passed = value if isinstance(value, list) else [value]
        entries.append((slot, tuple(stmt.text for stmt in passed)))
    return tuple(sorted(entries))


def api_gql(stmt: str) -> Statement:
    return Statement(stmt)


# The top-level names a corpus tree writes. Every case writes them into a
# different directory, so one left in `sys.modules` answers the next case --
# or the next test file -- with the previous tree's source.
CORPUS_MODULES = ("app", "tmpl")


@contextmanager
def record_run(root: Path) -> Iterator[Recording]:
    # One corpus case's execution. This module is published under the name the
    # corpus imports (`from gql_recorder import api_gql`) rather than written
    # into `root`, so the tree the scan walks holds the case's source and
    # nothing else.
    global _current  # noqa: PLW0603
    recording = Recording(root=root, binds={})
    previous, _current = _current, recording
    sys.modules[MODULE_NAME] = sys.modules[__name__]
    sys.path.insert(0, str(root))
    # Cleared going in as well as coming out: another test may have imported a
    # tree of its own under these names, and inheriting it would run that
    # tree's source against this case's expectations.
    forget_modules()
    importlib.invalidate_caches()
    try:
        yield recording
    finally:
        sys.path.remove(str(root))
        del sys.modules[MODULE_NAME]
        _current = previous
        forget_modules()


def forget_modules() -> None:
    stale = [
        name
        for name in sys.modules
        if name in CORPUS_MODULES
        or any(name.startswith(f"{top}.") for top in CORPUS_MODULES)
    ]
    for name in stale:
        del sys.modules[name]
