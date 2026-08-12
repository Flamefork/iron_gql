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
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import TypeAliasType
from typing import cast

import graphql

from iron_gql.codegen.render import method_body_fixed_names
from iron_gql.runtime import BoundSpec
from iron_gql.runtime import GQLTemplate


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
    # 2. Every name a body reads from an enclosing scope is claimed. No
    #    spelling is exempt from this: `to_snake_fn` is a documented hook, and
    #    nothing stops one from returning `_API_GQL_CAST` for a variable named
    #    that -- which is how a parameter came to shadow the cast alias its own
    #    body calls, and `execute` raised "'str' object is not callable". The
    #    generated classes are the one kind of name checked by shape rather
    #    than by list: each is claimed against the single scope that reaches
    #    for it (`render.bind_body_free_names` and the `execute` lists beside
    #    it), and a symbol table says which names a body reads without saying
    #    which artifact wrote the body.
    #
    # Methods only: the module-level `api_gql` takes no name from the schema,
    # so what its body binds is nobody's business but its own.
    package_name = module.__name__.rsplit(".", maxsplit=1)[-1]
    claimed = method_body_fixed_names(package_name)
    problems: list[str] = []
    for label, function in _generated_methods(module):
        bound = sorted(set(function.get_locals()) - set(function.get_parameters()))
        if bound:
            problems.append(f"{label} binds {', '.join(bound)}")
        read = set(function.get_globals()) | set(function.get_frees())
        unclaimed = sorted(
            name
            for name in read - claimed
            if not isinstance(getattr(module, name, None), (type, TypeAliasType))
        )
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
    #
    # Every table, not just the module's own children: PEP 695 wraps a generic
    # class or method in a "type parameters" table of its own and hangs the
    # real one under it, so a walk of the top level alone sees only the
    # non-generic classes. That is how every bound base's `execute` -- the one
    # body in the package that reads `cast` -- stayed outside this check while
    # it read an unclaimed name.
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    top = symtable.symtable(source, str(module.__file__), "exec")
    return [
        (f"{owner.get_name()}.{member.get_name()}", member)
        for owner in _tables(top)
        if isinstance(owner, symtable.Class)
        for member in owner.get_children()
        if isinstance(member, symtable.Function)
    ]


def _tables(table: symtable.SymbolTable) -> Iterator[symtable.SymbolTable]:
    for child in table.get_children():
        yield child
        yield from _tables(child)


def assert_documents_are_valid(
    module: ModuleType, schema: graphql.GraphQLSchema
) -> None:
    # Every document the generator synthesized -- a combination's expanded
    # source, where fragments are spliced into slots and variables are invented
    # for them -- has to be a valid document against the schema it was built
    # from. The IR tests can only say the generator agrees with itself, and
    # `bindings.expand_binding`'s own `graphql.validate` runs on the AST before
    # anything is rendered; graphql-core reading what actually reached the
    # module is a different question, and this is where it is asked.
    documents = _documents(module)
    if _template_names(module) and not documents:
        # A silent zero is how this oracle went vacuous once already: it used
        # to read a per-combination `exec_source__` ClassVar, that ClassVar
        # stopped being generated, and every package kept "passing" with
        # nothing checked. A template always has at least the empty
        # combination, so "no documents" can only mean this walk has lost
        # track of where they are kept.
        msg = (
            f"generated module {module.__name__} declares templates "
            f"({', '.join(_template_names(module))}) but this oracle found no "
            "document to validate -- the bind dispatch table has moved"
        )
        raise AssertionError(msg)
    problems: list[str] = []
    for name, source in documents:
        errors = graphql.validate(schema, graphql.parse(source))
        problems.extend(f"{name}: {error}" for error in errors)
    if problems:
        listed = "\n  ".join(problems)
        msg = f"generated module {module.__name__} wrote invalid documents:"
        msg += f"\n  {listed}"
        raise AssertionError(msg)


def _template_names(module: ModuleType) -> list[str]:
    return sorted(
        name
        for name, value in _namespace(module).items()
        if inspect.isclass(value) and issubclass(value, GQLTemplate)
    )


def _documents(module: ModuleType) -> list[tuple[str, str]]:
    # The package's bind dispatch table is where a combination's document is
    # kept: one row per combination, the document its first element (see
    # `runtime.BoundSpec`). Found by the suffix the renderer spells it with,
    # because the rest of the name is the package's own
    # (`render._module_binding_name`).
    #
    # Plain operations are deliberately absent: their document is a literal
    # inside `execute`'s body, reachable by no reflection, and it is the
    # statement the developer wrote rather than one the generator synthesized.
    tables = [
        cast("dict[object, BoundSpec]", value)
        for name, value in _namespace(module).items()
        if name.endswith("_GQL_BIND_DISPATCH") and isinstance(value, dict)
    ]
    if not tables:
        return []
    [table] = tables
    return [(str(key), exec_source) for key, (exec_source, _readers) in table.items()]
