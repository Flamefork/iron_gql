"""Total checks every generated package answers, whoever generated it.

Written as post-conditions of the generation helpers rather than as tests of
their own: a check that has to be *remembered* is applied to the packages
someone thought of, and the gaps are exactly the packages nobody thought of.
`basedpyright` was such a check -- called by hand from a handful of tests, so
most committed packages were never type-checked at all.

Every oracle is outside the generator: CPython resolving the module's own
annotations, CPython's own symbol table answering what each generated method
binds and reads, and graphql-core validating the documents the generator wrote
against the schema it wrote them from.
"""

import inspect
import symtable
import typing
from pathlib import Path
from types import ModuleType
from typing import cast

import graphql

from iron_gql.codegen.render import BIND_BODY_FREE_NAMES


def _namespace(module: ModuleType) -> dict[str, object]:
    # `vars` is typed as `dict[str, Any]`, and this module reads a generated
    # module's contents rather than calling into them: `object` is what those
    # contents actually are to the checks below.
    return cast("dict[str, object]", vars(module))


def assert_module_is_self_contained(module: ModuleType) -> None:
    # Every annotation the generated module writes must resolve inside it.
    # `from __future__ import annotations` makes them strings, so a name the
    # generator referenced but never emitted imports and runs perfectly well
    # and only surfaces at a type checker -- or at anything that reflects over
    # the module, pydantic included. Forcing them here is the cheapest
    # statement of "the module is complete".
    namespace = _namespace(module)
    problems: list[str] = []
    for name, value in list(namespace.items()):
        if getattr(value, "__module__", None) != module.__name__:
            # Imported into the module, not written by it: whatever declares
            # it answers for it.
            continue
        for owner, label, local in _annotated(name, value):
            try:
                _ = typing.get_type_hints(owner, namespace, local)
            except NameError as exc:
                problems.append(f"{label}: {exc}")
    if problems:
        listed = "\n  ".join(problems)
        msg = f"generated module {module.__name__} references undefined names:"
        msg += f"\n  {listed}"
        raise AssertionError(msg)


type _Target = tuple[object, str, dict[str, object]]


def _type_params(*owners: object) -> dict[str, object]:
    # PEP 695 parameters live on the class or function that declares them, not
    # in the module, so `get_type_hints` cannot see them from the namespace
    # alone. A method's annotations may name its own parameters and its
    # class's, hence both owners.
    return {
        param.__name__: param
        for owner in owners
        for param in cast(
            "tuple[typing.TypeVar, ...]", getattr(owner, "__type_params__", ())
        )
    }


def _annotated(name: str, value: object) -> list[_Target]:
    if inspect.isclass(value):
        members = inspect.getmembers(value, inspect.isfunction)
        return [
            (value, name, _type_params(value)),
            *(
                (member, f"{name}.{member_name}", _type_params(value, member))
                for member_name, member in members
                if member.__qualname__.startswith(f"{name}.")
            ),
        ]
    if inspect.isfunction(value):
        return [(value, name, _type_params(value))]
    return []


def assert_method_namespaces_are_closed(module: ModuleType) -> None:
    # Every method the generator writes carries parameters named after the
    # schema: slots on `bind()`, variables on `execute()` and `with_args()`.
    # So each name such a method binds or reads from outside shares one
    # namespace with a name the schema is free to spell, and either the
    # generator must not write that name at all or `naming._signature_claims`
    # must reserve it.
    #
    # Asked of CPython's symbol table rather than of a list of the names the
    # renderer is believed to write: that list is a second copy of the
    # renderer, and it drifted -- `bind()`'s fallback body bound a local `cls`
    # that no claim covered, so a slot called `cls` produced a module that ran
    # correctly and failed to type-check. Two conditions, over the whole
    # generated module at once:
    #
    # 1. No method body binds a local at all. Then a method's namespace is its
    #    parameters and nothing else, and no parameter can shadow anything.
    # 2. Every name a body reads from an enclosing scope and that could be
    #    the spelling of a generated parameter is claimed. "Could be" is
    #    "holds no upper-case letter": everything the generator reaches for at
    #    module scope it spells in upper case (`API_CLIENT`, the dispatch
    #    dicts) or in PascalCase (every generated class), so the names left
    #    over are the modules it imports -- which is exactly what the claim
    #    list is for.
    #
    # Methods only: the module-level `api_gql` takes no name from the schema,
    # so what its body binds is nobody's business but its own.
    claimed = {name for name, _ in BIND_BODY_FREE_NAMES}
    problems: list[str] = []
    for label, function in _generated_methods(module):
        bound = sorted(set(function.get_locals()) - set(function.get_parameters()))
        if bound:
            problems.append(f"{label} binds {', '.join(bound)}")
        read = set(function.get_globals()) | set(function.get_frees())
        unclaimed = sorted(name for name in read - claimed if name.lower() == name)
        if unclaimed:
            problems.append(f"{label} reads unclaimed {', '.join(unclaimed)}")
    if problems:
        listed = "\n  ".join(problems)
        msg = f"generated module {module.__name__} writes methods whose"
        msg += f" namespace a schema name can collide with:\n  {listed}"
        raise AssertionError(msg)


def _generated_methods(module: ModuleType) -> list[tuple[str, symtable.Function]]:
    # (label, table) for every function written inside a generated class. A
    # class body's own table holds the methods it declares, so no name-based
    # guess about which functions are methods is needed.
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    top = symtable.symtable(source, str(module.__file__), "exec")
    return [
        (f"{owner.get_name()}.{member.get_name()}", member)
        for owner in top.get_children()
        if isinstance(owner, symtable.Class)
        for member in owner.get_children()
        if isinstance(member, symtable.Function)
    ]


def assert_documents_are_valid(
    module: ModuleType, schema: graphql.GraphQLSchema
) -> None:
    # Every document the generator synthesized -- a bound operation's expanded
    # source above all, where fragments are spliced into slots and variables
    # are invented for them -- has to be a valid document against the schema it
    # was built from. The IR tests can only say the generator agrees with
    # itself; graphql-core is the authority on whether the result is GraphQL.
    problems: list[str] = []
    for name, source in _documents(module):
        errors = graphql.validate(schema, graphql.parse(source))
        problems.extend(f"{name}: {error}" for error in errors)
    if problems:
        listed = "\n  ".join(problems)
        msg = f"generated module {module.__name__} wrote invalid documents:"
        msg += f"\n  {listed}"
        raise AssertionError(msg)


def _documents(module: ModuleType) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name, value in _namespace(module).items():
        if not inspect.isclass(value):
            continue
        # `exec_source__` is the one place a generated class keeps the document
        # it will send; a subclass inherits its base's, so only the class that
        # declares it is asked.
        own = cast("dict[str, object]", dict(value.__dict__))
        source = own.get("exec_source__")
        if isinstance(source, str):
            found.append((name, source))
    return found
