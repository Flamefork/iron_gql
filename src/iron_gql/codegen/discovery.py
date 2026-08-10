import ast
import functools
import hashlib
import textwrap
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path

from iron_gql.slots import BindKey


@dataclass(kw_only=True, frozen=True)
class Statement:
    raw_text: str
    file: Path
    lineno: int

    @property
    def location(self) -> str:
        return f"{self.file}:{self.lineno}"

    @functools.cached_property
    def clean_text(self) -> str:
        return textwrap.dedent(self.raw_text).strip()

    @functools.cached_property
    def hash_str(self) -> str:
        return hashlib.md5(self.clean_text.encode(), usedforsecurity=False).hexdigest()


@dataclass(kw_only=True, frozen=True)
class BindDecl:
    template: Statement
    slot_args: tuple[tuple[str, tuple[Statement, ...]], ...]
    # Every `.bind(...)` call site that produced this exact combination, in
    # (file, lineno) order. Two call sites that bind the same template to the
    # same fragments are one binding, not a conflict: the generated class is
    # named after the combination and the runtime dispatches on it, so nothing
    # downstream could tell the two apart anyway.
    locations: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)


@dataclass(kw_only=True, frozen=True)
class IgnoredBind:
    # A `.bind(...)` call the scan resolved and then left alone, with the
    # reason it did. Not a diagnosis -- a third-party `.bind()` is the common
    # case and nothing is wrong with it -- but recorded so that "ignored on
    # purpose" and "ours and silently lost" are different observable outcomes.
    # Without it the only evidence either way is an empty `binds`, which is
    # what let a lost bind pass for a correct one.
    location: str
    reason: str


@dataclass(kw_only=True, frozen=True)
class DiscoveredPackage:
    statements: list[Statement]
    binds: list[BindDecl]
    ignored: list[IgnoredBind]


@dataclass(kw_only=True, frozen=True, slots=True)
class _NotOurs:
    # The name does not resolve to a gql statement this scan discovered, and
    # nothing about the failure says the call site meant one: an unscanned
    # module, an unknown name, a shadowing binding, a circular import. A base
    # that lands here is a third-party `.bind()` to leave alone; a slot
    # argument that lands here is a hard error, because the base already
    # proved that call ours. `reason` is written for the developer either way.
    reason: str
    # Whether a binding inside the scanned tree gives the name its value where
    # the call site stands. A bound name is *answered*: the call site reads a
    # parameter, a plain object, a module, whatever that binding holds, and a
    # statement of the same spelling somewhere else in the tree says nothing
    # about it -- two generator runs over one tree, each with its own gql
    # function, produce exactly that coincidence in ordinary code. An unbound
    # name has no such answer: nothing here gives it a value, which is the
    # shape a template written out of reach takes (see `_unreachable_here`).
    bound: bool


@dataclass(kw_only=True, frozen=True, slots=True)
class _AmbiguousName:
    # A name bound more than once where at least one binding *is* a gql
    # statement. Ours enough to diagnose whatever asked: reading it as "not
    # ours" would turn the routine extract-a-variable refactor into a silently
    # missing binding class.
    reason: str


# What resolving a name can come to. Kept as values rather than as exception
# types, because the two callers want opposite policies for `_NotOurs` and the
# same policy for `_AmbiguousName` -- a distinction an exception hierarchy can
# only express by having one caller re-raise a subclass it carved back out.
type _Resolution = Statement | _NotOurs | _AmbiguousName


@dataclass(kw_only=True, frozen=True, slots=True)
class _GqlAssign:
    # `NAME = <gql_fn>("...")`: the binding whose value this scan knows
    # outright.
    statement: Statement


@dataclass(kw_only=True, frozen=True, slots=True)
class _ImportedName:
    # `from <module> import <name> [as alias]`: a binding whose value is known
    # exactly too, one module over. Carried on the occurrence itself rather
    # than in a module-only side table, so an import inside a function -- the
    # standard way to break an import cycle -- resolves like any other, and so
    # a name that is *also* bound by something else cannot be resolved through
    # the import as if it were not.
    module: str
    name: str


@dataclass(kw_only=True, frozen=True, slots=True)
class _ImportedModule:
    # `import a.b` binds `a` to that module, `import a.b as c` binds `c` to
    # `a.b`. Carried on the occurrence like every other binding form, and for
    # the same reason `_ImportedName` is: an `import` written inside a function
    # binds a local name, and a module-only side table answered for it with
    # whatever the module level happened to bind of that spelling -- or with
    # nothing, dropping a chain the call site can see. As a *value* a module is
    # as opaque as a parameter; it is the one binding form that proves an
    # attribute chain reaches into the scanned tree.
    module: str


@dataclass(kw_only=True, frozen=True, slots=True)
class _OpaqueBinding:
    # Every other binding form: a parameter, a loop target, a `def`, a plain
    # assignment of something that is not a gql call. The name is bound, and
    # what it holds is not this scan's to know.
    #
    # A bare PEP 526 annotation claims the name without giving it a value, and
    # in a class body the difference is visible: the claim severs the closure,
    # so an enclosing function's name of that spelling is unreachable, while
    # the module's is still read (CPython falls back to globals). Recording it
    # as an ordinary binding refused a call that Python answers from the
    # module; ignoring it answered one that Python raises on.
    annotation_only: bool = False


_OPAQUE = _OpaqueBinding()
_ANNOTATION = _OpaqueBinding(annotation_only=True)

# How one scope binds one name, at the resolution it is recorded with. Four
# cases rather than "a statement or None", because "bound by an import of a
# name", "bound by an import of a module" and "bound by something opaque" are
# different answers to every question below.
type _Occurrence = _GqlAssign | _ImportedName | _ImportedModule | _OpaqueBinding


def _assigned(statement: Statement | None) -> _Occurrence:
    return _OPAQUE if statement is None else _GqlAssign(statement=statement)


@dataclass(kw_only=True, frozen=True, slots=True)
class _Bound:
    # One binding of one name, with the line that writes it. The line is what
    # tells a binding a call site can read from one it cannot: a name bound
    # only *below* the call is not the value that call sees -- Python raises
    # there, or, in a class body, looks past the class entirely.
    lineno: int
    occurrence: _Occurrence


@dataclass(kw_only=True, eq=False)
class _Scope:
    # Every name this scope binds, in source order, and how. A name bound
    # exactly once *above the call that reads it* is the only shape a bind
    # resolves through -- with a single binding there is only one value the
    # name can hold, whichever branch or loop it sits in, so no flow analysis
    # is needed to know what it is.
    names: dict[str, list[_Bound]] = field(default_factory=dict)
    global_names: set[str] = field(default_factory=set)
    nonlocal_names: set[str] = field(default_factory=set)
    # Class bodies are skipped when a nested scope looks outward, exactly as
    # Python skips them: a method never sees its class body's names.
    is_class: bool = False
    # The module scope ends the lexical walk: names that reach it resolve
    # through the module graph instead, which alone knows about imports.
    is_module: bool = False
    # A comprehension's own scope, which a walrus target writes *through*
    # rather than into (PEP 572). The one binding form whose name outlives the
    # scope it is written in, so the one scope kind that has to say what it is.
    is_comprehension: bool = False
    # A generator expression's body runs when the generator is *iterated*, not
    # where it is written. So a call inside one reads whatever the surrounding
    # names hold by then, exactly as a function body does -- and unlike a list,
    # set or dict comprehension, which runs on the spot.
    is_lazy: bool = False

    # A function body runs all at once, long after the module that defines it;
    # a module body and a class body run line by line as they are read. So a
    # call site sees every binding of the scopes it sits inside only when no
    # function definition stands between the two -- which is what
    # `_positional_depth` counts and this flag spells for one scope.
    @property
    def is_deferred(self) -> bool:
        if self.is_lazy:
            return True
        return not (self.is_class or self.is_module or self.is_comprehension)

    # Where a `nonlocal` can land. The declaration names an *enclosing
    # function*: a class body, a comprehension and the module are all scopes
    # it steps over, so a name they bind is never the one a `nonlocal` writes
    # (see `_Module.nonlocal_rebound`).
    @property
    def is_function(self) -> bool:
        return not (self.is_class or self.is_module or self.is_comprehension)

    def record(self, name: str, occurrence: _Occurrence, *, lineno: int) -> None:
        self.names.setdefault(name, []).append(
            _Bound(lineno=lineno, occurrence=occurrence)
        )

    def visible_at(self, name: str, lineno: int | None) -> list[_Bound] | None:
        # The bindings of `name` a call at `lineno` can read. `None` for a
        # line means "no position to compare against" -- a call reached through
        # a function definition, which runs after this scope's body is
        # complete, so every binding counts.
        bindings = self.names.get(name)
        if bindings is None or lineno is None:
            return bindings
        return [bound for bound in bindings if bound.lineno <= lineno]


@dataclass(kw_only=True, frozen=True)
class _Module:
    # Names some function in this module assigns through a `global` or a
    # `nonlocal` declaration, kept apart by which declaration wrote them.
    # Such a name has a value that depends on whether that function ran, which
    # is a flow question -- so a bind reading it is refused rather than
    # answered. Recording *where* the assignment lands instead (the scope the
    # declaration names) is what the scan used to do, and it was the most
    # delicate code here: the target may not be filled when the assignment is
    # walked, an intermediate function that binds nothing must be stepped
    # over, and a chain of `nonlocal` hands the search further out again.
    # Three defects came out of those three sentences.
    #
    # Two sets rather than one, because the two declarations address different
    # scopes and are therefore read at different places: `global` writes the
    # module's name, so only the module walk asks about it, and `nonlocal`
    # writes an enclosing *function*'s, so only the lexical walk does. One set
    # read in one place is what left half the family open -- the module half
    # refused while the enclosing-function half answered with a template the
    # call no longer reads -- and, the other way round, refused a module-level
    # name that only a `nonlocal` somewhere else in the file ever rebound.
    global_rebound: frozenset[str] = frozenset()
    nonlocal_rebound: frozenset[str] = frozenset()
    # The module scope's own bindings, in the same shape every other scope
    # uses. Names travelling between modules need no table of their own: an
    # import -- of a name or of a module -- is one of the binding forms a scope
    # records, so both kinds obey the scope rules the rest of the walk obeys.
    scope: _Scope
    star_imports: bool


@dataclass(kw_only=True, eq=False)
class _BindCandidate:
    call: ast.Call
    module: str
    # What the `.bind(...)` hangs off: a dotted name -- one part for a plain
    # name (`tmpl.bind(...)`), several for an attribute chain
    # (`app.infra.tmpl.bind(...)`) -- or, when the template is written inline
    # (`api_gql("query ...").bind(...)`), the statement itself.
    base: tuple[str, ...] | Statement
    # The lexical scope chain the call sits in, innermost first, ending at the
    # module scope.
    scopes: tuple[_Scope, ...]
    file: Path

    @property
    def location(self) -> str:
        return f"{self.file}:{self.call.lineno}"


def _module_name(path: Path, src_path: Path) -> str:
    rel = path.relative_to(src_path).with_suffix("")
    parts = rel.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# The dotted package a relative import climbs from: a package's own
# `__init__.py` is its own package, while a regular module's package is
# everything but its last component (empty for a top-level module).
def _package_name(module: str, *, is_init: bool) -> str:
    if is_init:
        return module
    if "." not in module:
        return ""
    return module.rsplit(".", 1)[0]


def _relative_module(package: str, level: int, submodule: str | None) -> str:
    # Mirrors importlib._bootstrap._resolve_name over module names relative to
    # the scanned root: `level - 1` dots beyond the first climb one package
    # each, and too few components left to climb means the import points above
    # the root. Such an import has no name in this tree, so it keeps the
    # spelling the file wrote — no scanned module matches it, and
    # `_ModuleGraph._resolve_module` reports it as outside the tree, quoting
    # what the source actually says.
    # The parts are joined rather than concatenated so a module directly under
    # the root (empty package) does not grow a leading dot.
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return "." * level + (submodule or "")
    return ".".join(part for part in (bits[0], submodule) if part)


def _gql_statement(
    call: ast.Call, *, gql_fn_name: str, relative_path: Path
) -> Statement:
    if (
        len(call.args) != 1
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
    ):
        msg = (
            f"Invalid positional arguments for {gql_fn_name} "
            f"at {relative_path}:{call.lineno}, "
            "expected a single string literal"
        )
        raise TypeError(msg)
    return Statement(
        raw_text=call.args[0].value, file=relative_path, lineno=call.lineno
    )


def _gql_call(
    node: ast.AST | None, *, gql_fn_name: str, relative_path: Path
) -> Statement | None:
    # The one shape a gql statement takes at a call site: a bare-name call of
    # the configured function. Three places ask the question -- an assignment's
    # value, a bare expression statement, and a `.bind(...)` slot argument --
    # so the shape they recognize is spelled here rather than at each of them.
    # `None` is a legal argument (a bare `x: Foo` annotation has no value) and
    # answers the question the same way anything else that is not a call does.
    match node:
        case ast.Call(func=ast.Name(id=called)) if called == gql_fn_name:
            return _gql_statement(
                node, gql_fn_name=gql_fn_name, relative_path=relative_path
            )
        case _:
            return None


def _dotted(package: str, name: str) -> str:
    # A module directly under the scanned root has an empty package, and
    # joining blindly would grow it a leading dot.
    if not package:
        return name
    return f"{package}.{name}"


def _unresolved_reason(name: str, location: str, mod: "_Module") -> str:
    hint = "; star imports are not followed" if mod.star_imports else ""
    return f"cannot resolve '{name}' at {location}{hint}"


# The (module, name) pairs one resolution has already been through. Threaded
# rather than created per step, because the walk is not the only thing that
# follows imports: `_lone_binding` sends a probe down every binding of an
# ambiguous name, and a probe that re-enters a pair the walk is already
# resolving is the import cycle the walk itself is guarding against.
type _Seen = set[tuple[str, str]]


@dataclass(kw_only=True, frozen=True)
class _ModuleGraph:
    # The scanned tree's name-resolution graph. A name that does not resolve
    # here does not exist in the tree at all -- which `resolve` reports as
    # `_NotOurs`, the one outcome callers may legitimately read as "not ours,
    # leave it alone".
    #
    # The lexical walk lives here too, and not beside the scan that built the
    # scopes: which binding of an ambiguous name wins is decided by what
    # resolving it comes to, and only the graph can resolve anything.
    modules: dict[str, _Module]
    # module -> why it failed to parse, for the files the scan deferred: they
    # mention neither the gql function nor `bind`, so they can contribute no
    # statement and no bind of their own. They can still be a link in a
    # resolution chain, and a resolution that reaches one cannot be answered
    # -- reporting it as "not ours" would drop a binding such a file may well
    # be re-exporting.
    unparsable: dict[str, str]
    # Every name any scope of any scanned module assigns a gql statement to,
    # and the statements assigned to it. The graph answers "what does this name
    # resolve to *here*"; this answers "does the tree have such a statement at
    # all", which is what tells a third-party `.bind()` from one of ours written
    # in a form no resolution can reach (behind a class, a star import, a
    # shadowing binding).
    statement_names: dict[str, list[Statement]]

    def resolve(
        self,
        module: str,
        name: str,
        scopes: tuple[_Scope, ...],
        *,
        location: str,
        lineno: int,
    ) -> _Resolution:
        seen: _Seen = set()
        local = self._resolve_local(
            module, scopes, name, location=location, lineno=lineno, seen=seen
        )
        match local:
            case None:
                return self._resolve_module(
                    module,
                    name,
                    reported=name,
                    location=location,
                    # The module scope is the last one position speaks for, and
                    # only while no function definition stands between it and
                    # the call. Past the first hop the chain is other modules,
                    # whose lines say nothing about this call.
                    lineno=lineno if _positional_depth(scopes) >= len(scopes) else None,
                    seen=seen,
                )
            case _ImportedName(module=source, name=source_name):
                # A local import names its source exactly, so it continues
                # into the graph exactly like a module-level one -- under the
                # name the developer wrote, which is what a diagnosis quotes.
                return self._resolve_module(
                    source, source_name, reported=name, location=location, seen=seen
                )
            case Statement() | _NotOurs() | _AmbiguousName() as resolution:
                return resolution

    def _resolve_local(
        self,
        module: str,
        scopes: tuple[_Scope, ...],
        name: str,
        *,
        location: str,
        lineno: int,
        seen: _Seen,
    ) -> _Resolution | _ImportedName | None:
        # What the lexical chain has to say about a name a bind reads.
        occurrence = self._lexical_occurrence(
            module, scopes, name, location=location, lineno=lineno, seen=seen
        )
        match occurrence:
            case None:
                return None
            case _GqlAssign(statement=statement):
                return statement
            case _NotOurs() as not_ours:
                return not_ours
            case _ImportedName() as imported:
                # Handed back rather than followed here: the caller continues
                # it into the graph under the name the developer wrote, which
                # is what a diagnosis quotes.
                return imported
            case _AmbiguousName() as ambiguous:
                return ambiguous
            case _ImportedModule() | _OpaqueBinding():
                # Bound by a parameter, a loop target, a `def`, a plain
                # assignment, an imported module. It is not ours, and -- this is
                # the point of reporting it from here rather than falling
                # through -- it must not reach a module-level name of the same
                # spelling, which would resolve the bind against a template the
                # call site cannot see.
                return _NotOurs(
                    reason=(
                        f"cannot resolve '{name}' at {location}: an enclosing "
                        "function or class body binds that name to something "
                        "other than a single gql statement"
                    ),
                    bound=True,
                )

    def _lexical_occurrence(
        self,
        module: str,
        scopes: tuple[_Scope, ...],
        name: str,
        *,
        location: str,
        lineno: int | None,
        seen: _Seen,
    ) -> _Occurrence | _AmbiguousName | _NotOurs | None:
        # Walks the lexical chain the way Python does: the innermost scope
        # always counts, class bodies are skipped on the way out, `global` jumps
        # straight to the module and `nonlocal` skips the scope that declared
        # it. Returns None when no function or class scope in the chain binds
        # the name -- those go on to the module graph.
        #
        # The binding form itself is handed back, not an answer about it: a bind
        # asks whether the name is a gql statement and an attribute chain asks
        # whether it is a module, and only one walk can be the one that obeys
        # Python's scoping for both.
        deferred = _positional_depth(scopes)
        rebound = self.modules[module].nonlocal_rebound
        for depth, scope in enumerate(scopes):
            if scope.is_module:
                return None
            if depth and scope.is_class:
                continue
            if name in scope.global_names:
                return None
            if name in scope.nonlocal_names:
                continue
            at = lineno if depth < deferred else None
            occurrences = scope.visible_at(name, at)
            if occurrences is None:
                continue
            if scope.is_function and name in rebound:
                # This scope holds the name, and some function in the file
                # writes a name of that spelling into an enclosing function of
                # its own. Which one is a question the scan does not ask (see
                # `_Module.nonlocal_rebound`): whichever it is, what the name
                # holds here depends on whether that function has run, and the
                # walk stops rather than answering with the binding written
                # above the call.
                return _rebound_outward(name, location)
            if not occurrences or (
                scope.is_class
                and all(
                    isinstance(bound.occurrence, _OpaqueBinding)
                    and bound.occurrence.annotation_only
                    for bound in occurrences
                )
            ):
                # The scope binds the name, but not in a way this call can
                # read: below the line, or by an annotation that gives it no
                # value. In a function or at module level the call raises. A
                # class body is the one scope that looks past itself, and
                # where it looks is the module -- a name claimed by the class
                # body is loaded by name, not through the closure, so the
                # enclosing function's binding of that spelling is not what
                # runs here.
                if scope.is_class:
                    return None
                return _bound_below_call(name, location)
            return self._lone_binding(
                occurrences, name=name, location=location, seen=seen
            )
        # Every chain a candidate carries ends at the module scope, which
        # returns above; falling out of the loop would mean the walk built one
        # that does not.
        msg = "scope chain does not end at the module scope"
        raise AssertionError(msg)

    def _lone_binding(
        self, bindings: list[_Bound], *, name: str, location: str, seen: _Seen
    ) -> _Occurrence | _AmbiguousName:
        # A name a bind reads must be bound exactly once in the scope it
        # resolves in. More than once and the answer depends on control flow:
        # that is a hard error when one of the bindings leads to a gql statement
        # -- extracting a template into a variable, or importing one under a
        # name something else also binds, must not silently drop the binding --
        # and simply opaque otherwise, which is the same answer a third-party
        # name gets.
        match bindings:
            case [only]:
                return only.occurrence
            case _ if any(
                self._leads_to_statement(bound.occurrence, location=location, seen=seen)
                for bound in bindings
            ):
                return _AmbiguousName(
                    reason=(
                        f"'{name}' at {location} resolves in a scope that binds "
                        "it more than once; a bind reads a name only where "
                        "exactly one binding gives it its value"
                    )
                )
            case _:
                return _OPAQUE

    def _leads_to_statement(
        self, occurrence: _Occurrence, *, location: str, seen: _Seen
    ) -> bool:
        # Whether this one binding puts a discovered statement into the name --
        # asked of what resolving it comes to, not of the form it is written in.
        # Two imports of one name are two `_ImportedName`s, so reading "ours" off
        # the form alone left the try/except import of a template looking like
        # any third-party name, and the bind was dropped without a word.
        match occurrence:
            case _GqlAssign():
                return True
            case _ImportedName(module=module, name=name):
                return isinstance(
                    self._resolve_module(
                        module, name, reported=name, location=location, seen=seen
                    ),
                    Statement,
                )
            case _ImportedModule() | _OpaqueBinding():
                return False

    def _resolve_module(
        self,
        module: str,
        name: str,
        *,
        reported: str,
        location: str,
        lineno: int | None = None,
        seen: _Seen,
    ) -> _Resolution:
        # `lineno` speaks for the first module only, and only when the caller
        # established that the call runs inside that module's own top-to-bottom
        # body. Every hop after it is a different module, whose lines have no
        # order relative to this call.
        current_module, current_name = module, name
        current_lineno = lineno
        while True:
            if (current_module, current_name) in seen:
                # Bound, and by the tree's own imports: they answer each other
                # in a ring, which is a defect of theirs and not evidence about
                # what this name was meant to hold.
                return _NotOurs(
                    reason=(
                        f"circular import chain while resolving "
                        f"'{reported}' at {location}"
                    ),
                    bound=True,
                )
            seen.add((current_module, current_name))
            mod = self.modules.get(current_module)
            if mod is None:
                failure = self.unparsable.get(current_module)
                if failure is not None:
                    msg = (
                        f"cannot resolve '{reported}' at {location}: it leads "
                        f"through a module that failed to parse. {failure}"
                    )
                    raise SyntaxError(msg)
                return _NotOurs(
                    reason=(
                        f"cannot resolve '{reported}' at {location}: "
                        f"module '{current_module}' is outside the scanned tree"
                    ),
                    # The chain leaves the tree, so nothing the scan read gives
                    # this name its value.
                    bound=False,
                )
            occurrences = _module_bindings(
                mod,
                current_name,
                current_lineno,
                reported=reported,
                location=location,
            )
            if isinstance(occurrences, _NotOurs):
                return occurrences
            match self._lone_binding(
                occurrences, name=current_name, location=location, seen=seen
            ):
                case _GqlAssign(statement=statement):
                    return statement
                case _ImportedName(module=next_module, name=next_name):
                    current_module, current_name = next_module, next_name
                    current_lineno = None
                case _AmbiguousName() as ambiguous:
                    return ambiguous
                case _ImportedModule() | _OpaqueBinding():
                    # Bound, and not by anything this scan can follow. The
                    # import of the same spelling is *one of* those bindings,
                    # so following it here would answer with a value the name
                    # may well not hold.
                    return _NotOurs(
                        reason=_unresolved_reason(reported, location, mod), bound=True
                    )

    def resolve_attribute(
        self, module: str, name: str, *, reported: str, location: str
    ) -> _Resolution:
        # A name read as an attribute of a scanned module resolves in that
        # module's own scope: the chain named the module, so nothing the
        # reading scope binds applies to it.
        return self._resolve_module(
            module, name, reported=reported, location=location, seen=set()
        )

    def prefix_module(
        self,
        module: str,
        scopes: tuple[_Scope, ...],
        prefix: tuple[str, ...],
        *,
        location: str,
        lineno: int,
    ) -> str | None:
        # The scanned module an attribute chain's dotted prefix names, or None
        # when it names no module of ours. `.bind(...)` is an ordinary method
        # name -- sockets, tkinter widgets and LDAP connections all have one --
        # so this is what separates `app.infra.tmpl.bind(...)` from
        # `self.sock.bind(...)`: the head has to be a module bound where the
        # call site stands, and the whole prefix has to land on a file the scan
        # read.
        head, *rest = prefix
        base = self._module_named(
            module, head, scopes, location=location, lineno=lineno
        )
        if base is None:
            return None
        candidate = ".".join([base, *rest])
        if candidate in self.modules:
            return candidate
        return None

    def _module_named(
        self,
        module: str,
        name: str,
        scopes: tuple[_Scope, ...],
        *,
        location: str,
        lineno: int,
    ) -> str | None:
        # Which module a name is bound to where the call site stands, or None
        # when it is bound to something else. The lexical chain answers first
        # and the module scope only when it binds nothing -- a parameter or a
        # local object spelled like an imported module hides that module here
        # exactly as it does at runtime.
        seen: _Seen = set()
        deferred = _positional_depth(scopes)
        occurrence = self._lexical_occurrence(
            module, scopes, name, location=location, lineno=lineno, seen=seen
        )
        if occurrence is None:
            at = lineno if deferred >= len(scopes) else None
            occurrences = self.modules[module].scope.visible_at(name, at)
            if not occurrences:
                return None
            occurrence = self._lone_binding(
                occurrences, name=name, location=location, seen=seen
            )
        match occurrence:
            case _ImportedModule(module=source):
                return source
            case _ImportedName(module=source, name=source_name):
                # `from pkg import name` binds a module when `pkg.name` is one.
                return _dotted(source, source_name)
            case _:
                return None


def _dotted_parts(node: ast.expr) -> tuple[str, ...] | None:
    # The dotted name a `.bind(...)` hangs off, or None when the base is not a
    # dotted name at all (a call, a subscript, ...).
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _rebound_outward(reported: str, location: str) -> _NotOurs:
    # The one wording for "a function writes this name from the outside".
    return _NotOurs(
        reason=(
            f"cannot resolve '{reported}' at {location}: a function assigns "
            "that name through a global or nonlocal declaration, so which "
            "value it holds depends on whether that function has run"
        ),
        bound=True,
    )


def _bound_below_call(reported: str, location: str) -> _NotOurs:
    # The one wording for "the name is bound here, but not yet". Written once
    # because the lexical walk and the module walk both reach it, and a call
    # site cannot tell which of the two answered it.
    return _NotOurs(
        reason=(
            f"cannot resolve '{reported}' at {location}: every binding of that "
            "name visible here is written below the call, so the call runs "
            "before the name has a value"
        ),
        bound=True,
    )


def _module_bindings(
    mod: _Module,
    name: str,
    lineno: int | None,
    *,
    reported: str,
    location: str,
) -> list[_Bound] | _NotOurs:
    # Only `global` is asked about here: it is the one declaration that writes
    # the module's own name. A `nonlocal` of the same spelling addresses an
    # enclosing function and is answered on the lexical walk.
    if name in mod.global_rebound:
        return _rebound_outward(reported, location)
    bindings = mod.scope.visible_at(name, lineno)
    if bindings is None:
        return _NotOurs(reason=_unresolved_reason(reported, location, mod), bound=False)
    if not bindings:
        return _bound_below_call(reported, location)
    return bindings


def _positional_depth(scopes: tuple[_Scope, ...]) -> int:
    # How far out from a call site the *line* of a binding still decides
    # whether that call can read it. A module body and a class body run top to
    # bottom while the call sits in them, so a binding written below the call
    # has not happened yet; a function body runs later, by which time every
    # binding of every scope around it has. So position counts from the call
    # site outward up to and including the first deferred scope, and not past
    # it -- `def go(): return tmpl.bind(...)` reads a module-level `tmpl`
    # assigned after `go` is defined, because `go()` runs after that line.
    for depth, scope in enumerate(scopes):
        if scope.is_deferred:
            return depth + 1
    return len(scopes)


def _walrus_scopes(scopes: tuple[_Scope, ...]) -> tuple[_Scope, ...]:
    # The chain a walrus target binds in: comprehension scopes are written
    # through, however many of them are nested. The module scope is never one,
    # so the walk always lands.
    while scopes[0].is_comprehension:
        scopes = scopes[1:]
    return scopes


def _arguments(args: ast.arguments) -> Iterator[ast.arg]:
    yield from args.posonlyargs
    yield from args.args
    yield from args.kwonlyargs
    if args.vararg is not None:
        yield args.vararg
    if args.kwarg is not None:
        yield args.kwarg


def _defaults(args: ast.arguments) -> list[ast.expr]:
    return [*args.defaults, *(default for default in args.kw_defaults if default)]


@dataclass(kw_only=True)
class _ModuleScan:
    # One module's walk. It records what every scope binds, collects every gql
    # statement wherever it appears, and notes every `.bind(...)` call together
    # with the scope chain it sits in. Scope structure is why this is a walk
    # with a chain rather than `ast.walk`: a name assigned inside a function is
    # not the module-level name of the same spelling, and only the walk knows
    # which of the two a given call site sees.
    gql_fn_name: str
    module: str
    relative_path: Path
    package: str
    module_scope: _Scope = field(default_factory=lambda: _Scope(is_module=True))
    statements: list[Statement] = field(default_factory=list)
    candidates: list[_BindCandidate] = field(default_factory=list)
    # The names this module assigns a statement to, in every scope it opens --
    # a class body and a function body included, which no resolution reaches
    # from outside. That is the point: a name the tree assigns a statement to
    # and a call site cannot read is a bind written in a form that will not
    # work, not a third-party `.bind()`.
    statement_names: dict[str, list[Statement]] = field(default_factory=dict)
    ignored: list[IgnoredBind] = field(default_factory=list)
    star_imports: bool = False
    # Names this module's functions assign through a `global` and through a
    # `nonlocal` declaration, kept apart by the scope each addresses (see
    # `_Module.global_rebound`).
    global_rebound: set[str] = field(default_factory=set)
    nonlocal_rebound: set[str] = field(default_factory=set)

    def run(self, tree: ast.Module) -> _Module:
        self._visit_all(tree.body, (self.module_scope,))
        return _Module(
            scope=self.module_scope,
            star_imports=self.star_imports,
            global_rebound=frozenset(self.global_rebound),
            nonlocal_rebound=frozenset(self.nonlocal_rebound),
        )

    def _gql_call(self, node: ast.AST | None) -> Statement | None:
        return _gql_call(
            node, gql_fn_name=self.gql_fn_name, relative_path=self.relative_path
        )

    def _visit_all(self, nodes: Iterable[ast.AST], scopes: tuple[_Scope, ...]) -> None:
        for node in nodes:
            self._visit(node, scopes)

    def _bind_name(
        self,
        scopes: tuple[_Scope, ...],
        name: str,
        occurrence: _Occurrence,
        *,
        lineno: int,
    ) -> None:
        # The one door a name goes through to enter a scope. Whether a
        # declaration redirects it is a question about the *scope* the binding
        # lands in, not about the syntax that binds it: `def tmpl(): ...`,
        # `import tmpl`, `except E as tmpl` and `case object() as tmpl` all
        # write the module's name under a `global tmpl` exactly as `tmpl = ...`
        # does. Asking that question at the assignment target alone left five
        # other binding forms writing the declaring function's own scope, and a
        # call site elsewhere then read a template the program had replaced.
        #
        # Parameters and PEP 695 type parameters do not come through here:
        # they bind a scope this walk has just opened, where no declaration can
        # have been written, and a `global` naming a parameter is a SyntaxError.
        if name in scopes[0].global_names:
            self.global_rebound.add(name)
        elif name in scopes[0].nonlocal_names:
            self.nonlocal_rebound.add(name)
        else:
            scopes[0].record(name, occurrence, lineno=lineno)

    def _record_target(
        self, target: ast.expr, scopes: tuple[_Scope, ...], occurrence: _Occurrence
    ) -> None:
        # Every assignment target, at the one place binding and walking are
        # told apart: a bare name binds (carrying the statement when the value
        # is a gql call), a tuple binds its elements to pieces of a value that
        # is no longer that statement, and an attribute or subscript target
        # binds nothing but still holds expressions to walk.
        match target:
            case ast.Name(id=name):
                self._bind_name(scopes, name, occurrence, lineno=target.lineno)
                if isinstance(occurrence, _GqlAssign):
                    self.statement_names.setdefault(name, []).append(
                        occurrence.statement
                    )
            case ast.Tuple(elts=elts) | ast.List(elts=elts):
                for elt in elts:
                    self._record_target(elt, scopes, _OPAQUE)
            case ast.Starred(value=value):
                self._record_target(value, scopes, _OPAQUE)
            case _:
                self._visit(target, scopes)

    def _with_type_params(
        self, type_params: Sequence[ast.type_param], scopes: tuple[_Scope, ...]
    ) -> tuple[_Scope, ...]:
        # PEP 695 opens a scope of its own around the whole definition: the
        # type parameters are visible in the defaults, the annotations, the
        # bases and the body, and they shadow anything of that name outside --
        # so a `def go[tmpl]()` hides a module-level `tmpl` from its own body.
        if not type_params:
            return scopes
        scope = _Scope()
        for param in type_params:
            match param:
                case (
                    ast.TypeVar(name=name)
                    | ast.ParamSpec(name=name)
                    | ast.TypeVarTuple(name=name)
                ):
                    scope.record(name, _OPAQUE, lineno=param.lineno)
                case _:
                    # Internal invariant: PEP 695 has exactly these three
                    # kinds of type parameter, and the module parsed.
                    msg = f"Unsupported type parameter: {param}"
                    raise AssertionError(msg)
        return (scope, *scopes)

    def _visit_comprehension(
        self,
        elements: Sequence[ast.expr],
        generators: Sequence[ast.comprehension],
        scopes: tuple[_Scope, ...],
        *,
        lazy: bool = False,
    ) -> None:
        # A comprehension has a scope of its own, so its target never rebinds
        # a name the enclosing scope holds: this is the one binding form where
        # "binds a name" and "hides the enclosing name" come apart, and
        # treating it like a `for` target reports a collision that does not
        # exist at runtime. Only the outermost iterable is evaluated outside.
        outermost, *rest = generators
        self._visit(outermost.iter, scopes)
        inner = (_Scope(is_comprehension=True, is_lazy=lazy), *scopes)
        self._record_target(outermost.target, inner, _OPAQUE)
        self._visit_all(outermost.ifs, inner)
        for generator in rest:
            self._visit(generator.iter, inner)
            self._record_target(generator.target, inner, _OPAQUE)
            self._visit_all(generator.ifs, inner)
        self._visit_all(elements, inner)

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, scopes: tuple[_Scope, ...]
    ) -> None:
        # Decorators are written and read outside the definition entirely;
        # defaults and annotations sit inside its type-parameter scope but
        # outside the function's own, where only parameters and the body live.
        #
        # The name is recorded before any of them, and the order is not what
        # decides whether a call written *inside* the header can read the name
        # this definition replaces: visibility is a comparison of line
        # numbers, and a header shares its `def`'s line. So a
        # `def tmpl(x=tmpl.bind(...))` sees two bindings of `tmpl` and is
        # refused, exactly like any other name a scope binds twice --
        # `tests/corpus/scoping_outcomes.txt` pins that under `placement`.
        self._bind_name(scopes, node.name, _OPAQUE, lineno=node.lineno)
        self._visit_all(node.decorator_list, scopes)
        outer = self._with_type_params(node.type_params, scopes)
        self._visit_all(_defaults(node.args), outer)
        annotations = [
            annotation
            for annotation in (
                *(arg.annotation for arg in _arguments(node.args)),
                node.returns,
            )
            if annotation is not None
        ]
        self._visit_all(annotations, outer)
        inner = _Scope()
        for arg in _arguments(node.args):
            inner.record(arg.arg, _OPAQUE, lineno=arg.lineno)
        self._visit_all(node.body, (inner, *outer))

    def _visit(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> None:
        # Each visitor answers "was this node mine to handle": one opens a
        # scope, one carries a statement into the name it assigns, one binds a
        # name the grammar does not spell as a `Name` node, one is a call of
        # ours. A node no visitor claims is walked child by child -- which is
        # how every remaining binding form (a loop target, a `with ... as`, a
        # comprehension target, a `del`) reaches the `Name` case at the end of
        # `_visit_opaque_binding`.
        claimed = (
            self._visit_scoping(node, scopes)
            or self._visit_assignment(node, scopes)
            or self._visit_import(node, scopes)
            or self._visit_opaque_binding(node, scopes)
            or self._visit_gql_call(node, scopes)
        )
        if not claimed:
            self._visit_all(ast.iter_child_nodes(node), scopes)

    def _visit_scoping(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> bool:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                self._visit_function(node, scopes)
            case ast.Lambda(args=args, body=body):
                self._visit_all(_defaults(args), scopes)
                inner = _Scope()
                for arg in _arguments(args):
                    inner.record(arg.arg, _OPAQUE, lineno=arg.lineno)
                self._visit(body, (inner, *scopes))
            case ast.ClassDef(
                name=name,
                bases=bases,
                keywords=keywords,
                decorator_list=decorators,
                body=body,
                type_params=type_params,
            ):
                self._bind_name(scopes, name, _OPAQUE, lineno=node.lineno)
                self._visit_all(decorators, scopes)
                outer = self._with_type_params(type_params, scopes)
                self._visit_all([*bases, *keywords], outer)
                inner = _Scope(is_class=True)
                self._visit_all(body, (inner, *outer))
            case ast.TypeAlias(name=name_node, type_params=type_params, value=value):
                # `type X[T] = ...` binds `X` here and evaluates its value
                # lazily in a scope of its own, where `T` is visible.
                self._record_target(name_node, scopes, _OPAQUE)
                self._visit(value, self._with_type_params(type_params, scopes))
            case ast.GeneratorExp(elt=element, generators=generators):
                self._visit_comprehension([element], generators, scopes, lazy=True)
            case (
                ast.ListComp(elt=element, generators=generators)
                | ast.SetComp(elt=element, generators=generators)
            ):
                self._visit_comprehension([element], generators, scopes)
            case ast.DictComp(key=key, value=value, generators=generators):
                self._visit_comprehension([key, value], generators, scopes)
            case _:
                return False
        return True

    def _visit_assignment(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> bool:
        # The two forms that can carry a gql statement into a name. Their
        # targets are recorded here rather than walked into, so the statement
        # reaches the name instead of the name arriving as a bare `Name` node
        # with nothing attached.
        match node:
            case ast.Assign(targets=targets, value=value):
                statement = self._gql_call(value)
                for target in targets:
                    self._record_target(target, scopes, _assigned(statement))
                self._visit(value, scopes)
            case ast.AnnAssign(target=target, annotation=annotation, value=value):
                # A bare annotation (`name: T`) declares rather than assigns,
                # but PEP 526 still makes the declaration alone claim the name
                # for the scope it is written in -- in a function body and in a
                # class body alike. So the outer name of that spelling is
                # unreachable from there: reading `name` raises rather than
                # finding the enclosing function's, which a scan reading the
                # annotation as invisible answered with a binding for a call
                # that cannot run.
                #
                # The module scope is the exception, and only because it has no
                # outer scope to be shadowed from: a name annotated and not yet
                # assigned raises there too, which is what the positional
                # filter already says about every module-level binding.
                if value is not None:
                    statement = self._gql_call(value)
                    self._record_target(target, scopes, _assigned(statement))
                    self._visit(value, scopes)
                elif scopes[0].is_class:
                    self._record_target(target, scopes, _ANNOTATION)
                elif not scopes[0].is_module:
                    self._record_target(target, scopes, _OPAQUE)
                self._visit(annotation, scopes)
            case _:
                return False
        return True

    def _visit_import(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> bool:
        # An import binds a name like anything else, wherever it is written --
        # for the `from` form a name this scan can follow, for the plain form a
        # module. `import a.b.c` binds only `a` (the rest of the chain is
        # attribute access from there), `import a.b.c as x` binds `x` to the
        # whole path.
        match node:
            case ast.Import(names=names):
                for alias in names:
                    head = alias.name.split(".", 1)[0]
                    self._bind_name(
                        scopes,
                        alias.asname or head,
                        _ImportedModule(module=alias.name if alias.asname else head),
                        lineno=alias.lineno,
                    )
            case ast.ImportFrom(level=level, module=source_module, names=names):
                base = (
                    source_module
                    if level == 0 and source_module is not None
                    else _relative_module(self.package, level, source_module)
                )
                for alias in names:
                    if alias.name == "*":
                        # A star import is module-level by grammar, and brings
                        # in names this scan cannot enumerate.
                        self.star_imports = True
                        continue
                    self._bind_name(
                        scopes,
                        alias.asname or alias.name,
                        _ImportedName(module=base, name=alias.name),
                        lineno=alias.lineno,
                    )
            case _:
                return False
        return True

    def _visit_opaque_binding(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> bool:
        # Every binding form whose name is not a `Name` node the walk would
        # reach on its own -- an `except ... as`, a match capture -- plus the
        # `Name` case itself, which catches all the rest, and the two
        # declarations that redirect a name out of this scope.
        scope = scopes[0]
        match node:
            case ast.Global(names=names):
                scope.global_names.update(names)
            case ast.Nonlocal(names=names):
                scope.nonlocal_names.update(names)
            case ast.NamedExpr(target=target, value=value):
                # PEP 572 changes *where* a name is written, not what it holds:
                # `if (tmpl := api_gql(...))` is the same assignment `tmpl =
                # api_gql(...)` is, so the statement travels into the name here
                # exactly as it does there. What is special is the scope -- a
                # walrus inside a comprehension binds in the scope the
                # comprehension is *written in*, not in the comprehension's own,
                # the one binding form whose name outlives the scope that spells
                # it. Recording it where every other target goes left the
                # enclosing function's name unbound, and the bind then resolved
                # against a module-level template the call site can no longer
                # see.
                self._visit(value, scopes)
                self._record_target(
                    target, _walrus_scopes(scopes), _assigned(self._gql_call(value))
                )
            case (
                ast.ExceptHandler(name=str() as name)
                | ast.MatchAs(name=str() as name)
                | ast.MatchStar(name=str() as name)
                | ast.MatchMapping(rest=str() as name)
            ):
                # One shape, four spellings: the bound name is a plain string
                # on the node, and everything under it is walked as usual.
                self._bind_name(scopes, name, _OPAQUE, lineno=node.lineno)
                self._visit_all(list(ast.iter_child_nodes(node)), scopes)
            case ast.Name(ctx=ast.Store() | ast.Del(), id=name):
                # Where a loop target, a `with ... as`, a comprehension
                # target, a `del` and any binding form a future grammar adds
                # all land. Recording it as "bound, not ours" is the safe
                # answer: the alternative is resolving the name against a
                # module-level template the call site cannot see.
                self._bind_name(scopes, name, _OPAQUE, lineno=node.lineno)
            case _:
                return False
        return True

    def _visit_gql_call(self, node: ast.AST, scopes: tuple[_Scope, ...]) -> bool:
        match node:
            case ast.Call() if (statement := self._gql_call(node)) is not None:
                self.statements.append(statement)
            case ast.Call(func=ast.Attribute(attr="bind", value=base)):
                self._record_bind_candidate(node, base, scopes)
                self._visit_all(list(ast.iter_child_nodes(node)), scopes)
            case _:
                return False
        return True

    def _record_bind_candidate(
        self, call: ast.Call, base: ast.expr, scopes: tuple[_Scope, ...]
    ) -> None:
        # What the base is decides whether this call can be ours at all:
        #   - a gql call writes the template inline, which no third-party
        #     `.bind()` can imitate;
        #   - a dotted name, whether a bare `tmpl` or an `import module` chain
        #     like `app.infra.tmpl`, is genuinely ambiguous -- `self.sock.bind`
        #     has the same shape -- so it becomes a candidate and resolution
        #     decides;
        #   - anything else -- a subscript, a call, a comprehension -- holds a
        #     value no static walk can read, so no resolution can be attempted
        #     and the call is left alone. Recorded rather than dropped: a
        #     template kept in a dict is a bind that generates nothing, and the
        #     silent return here was the one `.bind(` in a scanned file that
        #     reached neither `binds` nor `ignored`.
        location = f"{self.relative_path}:{call.lineno}"
        inline = self._gql_call(base)
        target = inline if inline is not None else _dotted_parts(base)
        if target is None:
            self.ignored.append(
                IgnoredBind(
                    location=location,
                    reason=(
                        f"'.bind(...)' at {location} hangs off an expression "
                        "that is neither a name nor an inline "
                        f"{self.gql_fn_name}(...) call, so what it holds cannot "
                        "be read statically; name the template and bind the name"
                    ),
                )
            )
            return
        self.candidates.append(
            _BindCandidate(
                call=call,
                module=self.module,
                base=target,
                scopes=scopes,
                file=self.relative_path,
            )
        )


def _slot_statement(
    expr: ast.expr,
    candidate: _BindCandidate,
    graph: _ModuleGraph,
    *,
    keyword: str,
    gql_fn_name: str,
) -> Statement:
    match expr:
        case ast.Name(id=name):
            # Every failure is hard here: the call's base already resolved to a
            # discovered gql statement, so this bind is ours and a slot value
            # that does not resolve is a defect in it, not a sign it belongs to
            # someone else.
            resolution = graph.resolve(
                candidate.module,
                name,
                candidate.scopes,
                location=candidate.location,
                lineno=candidate.call.lineno,
            )
            match resolution:
                case Statement():
                    return resolution
                case _NotOurs(reason=reason) | _AmbiguousName(reason=reason):
                    raise TypeError(reason)
        case _ if (
            statement := _gql_call(
                expr,
                gql_fn_name=gql_fn_name,
                relative_path=candidate.file,
            )
        ) is not None:
            return statement
        case _:
            msg = (
                f"the value for '{keyword}' of '.bind(...)' at "
                f"{candidate.location} must be a fragment name, an inline "
                f"{gql_fn_name}(...) statement, or a list of either"
            )
            raise TypeError(msg)


def _slot_statements(
    value: ast.expr,
    candidate: _BindCandidate,
    graph: _ModuleGraph,
    *,
    keyword: str,
    gql_fn_name: str,
) -> tuple[Statement, ...]:
    exprs = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
    return tuple(
        _slot_statement(
            expr, candidate, graph, keyword=keyword, gql_fn_name=gql_fn_name
        )
        for expr in exprs
    )


def _reject_repeated_fragment(
    statements: tuple[Statement, ...], candidate: _BindCandidate, *, keyword: str
) -> None:
    # A slot spreads each of its fragments once, so naming one twice asks for
    # a combination that cannot exist: the expanded operation would carry the
    # same spread twice and the generated overload would union a class with
    # itself. Rejected here, at the call site that wrote it, rather than
    # de-duplicated -- silently reading `[f, f]` as `[f]` would answer a
    # different question than the one asked.
    seen: dict[str, Statement] = {}
    for statement in statements:
        first = seen.get(statement.hash_str)
        if first is not None:
            msg = (
                f"'.bind(...)' at {candidate.location} passes the statement at "
                f"{first.location} to slot '{keyword}' more than once; a slot "
                "spreads each of its fragments once"
            )
            raise TypeError(msg)
        seen[statement.hash_str] = statement


# Only reached once the call's base has resolved to a discovered gql statement
# -- so every failure from here on is a hard error about a bind we own, not a
# guess about whether it's ours.
def _validated_bind(
    candidate: _BindCandidate,
    template: Statement,
    graph: _ModuleGraph,
    *,
    gql_fn_name: str,
) -> BindDecl:
    call = candidate.call
    if call.args:
        msg = f"'.bind(...)' at {candidate.location} accepts only keyword arguments"
        raise TypeError(msg)
    slot_args: list[tuple[str, tuple[Statement, ...]]] = []
    seen_slots: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None:
            msg = f"'.bind(...)' at {candidate.location} accepts only keyword arguments"
            raise TypeError(msg)
        # `ast.parse` alone does not reject a repeated keyword the way
        # `compile()` would (that check lives in the compiler, not the
        # parser) — caught here, loudly, while the AST still has both
        # keyword nodes, instead of silently keeping only the last one.
        if kw.arg in seen_slots:
            msg = (
                f"'.bind(...)' at {candidate.location} repeats keyword "
                f"'{kw.arg}'; pass every fragment for one slot in a single "
                "keyword (a name or a list of names)"
            )
            raise TypeError(msg)
        seen_slots.add(kw.arg)
        statements = _slot_statements(
            kw.value, candidate, graph, keyword=kw.arg, gql_fn_name=gql_fn_name
        )
        _reject_repeated_fragment(statements, candidate, keyword=kw.arg)
        slot_args.append((kw.arg, statements))
    return BindDecl(
        template=template,
        slot_args=tuple(slot_args),
        locations=(candidate.location,),
    )


def _unreachable_here(
    graph: _ModuleGraph, candidate: _BindCandidate, name: str, not_ours: _NotOurs
) -> _NotOurs:
    # The last word on a base that did not resolve. "Did not resolve here" and
    # "is not ours" are different facts, and only the first one is what a failed
    # resolution establishes: a template held in a class body or brought in by a
    # star import is ours all the same, written where no resolution can reach
    # it. Answering "not ours" there dropped the bind, reported success, and
    # left the call site to raise LookupError at import time with a suggestion
    # to regenerate that never helped.
    #
    # Only a name nothing binds here can be that, which is the line this draws.
    # A name something *does* bind is answered by that binding, and a statement
    # of the same spelling elsewhere in the tree is a coincidence -- one that
    # two generator runs over a single tree produce routinely, each run seeing
    # the other's `<pkg>_gql(...)` calls as plain values.
    #
    # What remains is a third-party `.bind()` on a name nothing binds where it
    # stands and that some statement of ours is also named after. Diagnosing it
    # is the deliberate cost: the statement's own address is in the message, so
    # the mistaken half is visible at a glance and the fix (rename either side)
    # is immediate.
    if not_ours.bound:
        return not_ours
    statements = graph.statement_names.get(name)
    if statements is None:
        return not_ours
    addresses = ", ".join(statement.location for statement in statements)
    msg = (
        f"'.bind(...)' at {candidate.location} reads '{name}', which the "
        f"scanned tree assigns a gql statement at {addresses} -- but not where "
        f"this call stands: {not_ours.reason}. Import that name directly "
        "(`from module import name`) into the scope the bind is written in, or "
        "rename whichever of the two is not the template."
    )
    raise TypeError(msg)


def _resolve_base(
    graph: _ModuleGraph, candidate: _BindCandidate
) -> Statement | _NotOurs:
    if isinstance(candidate.base, Statement):
        return candidate.base
    *prefix, base_name = candidate.base
    if prefix:
        # An attribute chain reaching a template of the scanned tree is the one
        # access form the rules reject. Both halves of "reaching a template of
        # the scanned tree" have to be established before saying so: a scanned
        # module holds sockets, widgets and connections next to its templates,
        # and `.bind(...)` is an ordinary method name on all of them, so
        # diagnosing on the shape of the chain alone stopped generation over
        # `net.sock.bind(...)`.
        target = graph.prefix_module(
            candidate.module,
            candidate.scopes,
            tuple(prefix),
            location=candidate.location,
            lineno=candidate.call.lineno,
        )
        if target is None:
            chain = ".".join(prefix)
            return _unreachable_here(
                graph,
                candidate,
                base_name,
                _NotOurs(
                    reason=(
                        f"'.bind(...)' at {candidate.location} hangs off "
                        f"'{chain}', which names no module of the scanned tree"
                    ),
                    # Whatever the chain's head holds, the name behind the last
                    # dot is not a name of this scope at all -- nothing here
                    # binds it, so a statement the tree owns under that spelling
                    # is the one thing the chain could have been reaching for.
                    bound=False,
                ),
            )
        match graph.resolve_attribute(
            target,
            base_name,
            reported=".".join(candidate.base),
            location=candidate.location,
        ):
            case Statement() | _AmbiguousName():
                # A statement, or a name at least one gql statement assigns:
                # either way the chain reaches something of ours, which is what
                # this diagnosis is about.
                msg = (
                    f"'.bind(...)' at {candidate.location} reaches its template "
                    "through an attribute chain; import the template name "
                    "directly (`from module import name`) instead of "
                    "`import module`"
                )
                raise TypeError(msg)
            case _NotOurs() as not_ours:
                return _unreachable_here(graph, candidate, base_name, not_ours)
    resolution = graph.resolve(
        candidate.module,
        base_name,
        candidate.scopes,
        location=candidate.location,
        lineno=candidate.call.lineno,
    )
    match resolution:
        case Statement():
            return resolution
        case _AmbiguousName(reason=reason):
            # A name some gql statement assigns, assigned again: ours enough to
            # diagnose. Reading it as "not ours" is what would make extracting a
            # template into a variable drop the binding without a word.
            raise TypeError(reason)
        case _NotOurs() as not_ours:
            # The base names nothing the tree assigns a statement to, so this
            # `.bind()` is a third-party call to leave untouched. The reason
            # travels with it into `DiscoveredPackage.ignored` rather than being
            # dropped, so a test -- and a debug run -- can tell this outcome
            # from a bind that was ours and got lost.
            return _unreachable_here(graph, candidate, base_name, not_ours)


def _resolve_binds(
    graph: _ModuleGraph, candidates: list[_BindCandidate], *, gql_fn_name: str
) -> tuple[list[BindDecl], list[IgnoredBind]]:
    binds: list[BindDecl] = []
    ignored: list[IgnoredBind] = []
    for candidate in candidates:
        match _resolve_base(graph, candidate):
            case Statement() as template:
                binds.append(
                    _validated_bind(candidate, template, graph, gql_fn_name=gql_fn_name)
                )
            case _NotOurs(reason=reason):
                ignored.append(IgnoredBind(location=candidate.location, reason=reason))
    return binds, ignored


def _bind_combination_key(bind: BindDecl) -> BindKey:
    # Every keyword the call actually wrote, empty ones included -- which is
    # what makes this *not* the runtime's key. Dropping empty slots here (the
    # normalisation `slots.bind_key_shape` does, rightly, for dispatch) merges
    # a call that names a slot the template does not have into one that names
    # nothing, and the misspelling then reaches no check at all: whether it was
    # diagnosed depended on whether some other file happened to hold the empty
    # bind it collapsed onto. Calls that mean one combination are merged after
    # expansion instead (`collect._merge_expanded`), where the slot names have
    # been checked against the template.
    return (
        bind.template.hash_str,
        tuple(
            sorted(
                (key, tuple(sorted(stmt.hash_str for stmt in stmts)))
                for key, stmts in bind.slot_args
            )
        ),
    )


def _dedupe_binds(binds: list[BindDecl]) -> list[BindDecl]:
    # One binding per combination. The generated class is named after the
    # combination and the runtime dispatches on it, so two call sites that bind
    # the same fragments into the same template are the same binding -- which
    # is what makes a bind legal in a function body at all: shared code and its
    # caller may reach the same combination without knowing about each other.
    merged: dict[BindKey, BindDecl] = {}
    for bind in binds:
        key = _bind_combination_key(bind)
        seen = merged.get(key)
        merged[key] = (
            bind
            if seen is None
            else replace(seen, locations=(*seen.locations, *bind.locations))
        )
    return list(merged.values())


def _statement_names(scans: list[_ModuleScan]) -> dict[str, list[Statement]]:
    # One index over the whole tree: a name is one name whichever module writes
    # it, and the question it answers -- "does the tree assign this spelling a
    # statement at all" -- is asked of a call site that has already failed to
    # resolve it locally.
    names: dict[str, list[Statement]] = {}
    for scan in scans:
        for name, statements in scan.statement_names.items():
            names.setdefault(name, []).extend(statements)
    return names


def discover_package(
    src_path: Path, gql_fn_name: str, *, skip_path: Path
) -> DiscoveredPackage:
    # Every scanned file must be parsed regardless of content: a pure
    # re-export module (`from x import y as z`, no gql call and no `.bind(`
    # of its own) still has to be scanned for its imports so a bind chain can
    # be followed through it.
    modules: dict[str, _Module] = {}
    unparsable: dict[str, str] = {}
    # The finished walks, kept whole rather than unpacked into a list per field
    # they carry: what each of the four is used for is decided below, once, and
    # in terms of all the scans at a time.
    scans: list[_ModuleScan] = []
    skip = skip_path.resolve()

    # Sorted: `glob` walks in whatever order the filesystem hands back, and
    # every consumer of `statements` and `binds` inherits the order this loop
    # produces -- down to the text of a diagnosis and the generated file's
    # diff between two machines.
    for path in sorted(src_path.glob("**/*.py")):
        if path.resolve() == skip:
            continue
        relative_path = path.relative_to(src_path)
        module = _module_name(path, src_path)
        content = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            msg = f"Failed to parse {path}: {exc.msg} (line {exc.lineno})"
            if gql_fn_name in content or "bind" in content:
                # It may well declare a statement or a bind of its own, and
                # nothing short of parsing it can say which. Skipping one that
                # does would rewrite the generated package with every
                # operation, fragment and binding it owns silently removed.
                #
                # Both probes are the bare identifier on purpose: a call the
                # walk would claim spells the gql function's name or the
                # attribute `bind` literally, whatever whitespace, line break
                # or comment the source puts around it, so no file that owns
                # something can slip past. A file that merely says "binding" in
                # a comment is caught too -- one false abort on a file that is
                # already broken, against a whole package regenerated without
                # the statements a real one holds.
                raise SyntaxError(msg) from exc
            # Names neither, so it owns nothing the package could lose. It is
            # still recorded rather than forgotten: a pure re-export module is
            # a link in a resolution chain even when it mentions no gql call,
            # and `_resolve_module` raises if some chain actually reaches it.
            unparsable[module] = msg
            continue

        scan = _ModuleScan(
            gql_fn_name=gql_fn_name,
            module=module,
            relative_path=relative_path,
            package=_package_name(module, is_init=path.name == "__init__.py"),
        )
        modules[module] = scan.run(tree)
        scans.append(scan)

    graph = _ModuleGraph(
        modules=modules,
        unparsable=unparsable,
        statement_names=_statement_names(scans),
    )
    # Sorted here, at the one place binds enter the pipeline, so no consumer
    # has to re-sort: the walk does not visit one file's binds in source order.
    # A comprehension is walked outermost-iterable first, so a bind written in
    # its element expression is recorded after one standing further down the
    # text. The file component keeps that per-file order from interleaving --
    # the loop above already hands the files over sorted, and sorting on the
    # line number alone would mix them together.
    candidates = sorted(
        (candidate for scan in scans for candidate in scan.candidates),
        key=lambda candidate: (candidate.file, candidate.call.lineno),
    )
    binds, ignored = _resolve_binds(graph, candidates, gql_fn_name=gql_fn_name)
    return DiscoveredPackage(
        statements=[statement for scan in scans for statement in scan.statements],
        binds=_dedupe_binds(binds),
        # The two groups are kept apart rather than merged into one order: a
        # base no resolution could be attempted on is recorded by the walk, the
        # rest by resolution, and each group is already deterministic. Nothing
        # reads `ignored` positionally -- it is a debug dump and a test's
        # evidence that a `.bind(` was accounted for.
        ignored=[*(entry for scan in scans for entry in scan.ignored), *ignored],
    )
