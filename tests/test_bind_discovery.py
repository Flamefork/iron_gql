import json
import re
import sys
from pathlib import Path

import pytest

from iron_gql.codegen import generate_gql_package
from iron_gql.codegen.discovery import DiscoveredPackage
from iron_gql.codegen.discovery import discover_package
from tests.conftest import ProjectBuilder
from tests.conftest import write_text


def _write(root: Path, rel: str, text: str) -> None:
    write_text(root / rel, text)


def _discover(root: Path) -> DiscoveredPackage:
    # Every test in this file scans one throwaway tree for the same call name,
    # skipping the generated package that would otherwise be scanned as source.
    return discover_package(root, "api_gql", skip_path=root / "gql.py")


TEMPLATE = 'api_gql("query Q { f @slot { __typename } }")'
FRAGMENT = 'api_gql("fragment F on T { x }")'

# The one diagnosis every "our template, written where this call cannot read
# it" case comes to. Matched by the phrase rather than by the whole sentence so
# the wording can be improved without rewriting a dozen tests, and spelled once
# so no test pins a different half of it.
_UNREACHABLE = "not where this call stands"


# Nothing about a bind has to live at module level: a template, its fragments
# and the bind itself are ordinary names in whatever scope the code that uses
# them sits in. Module level is how a template is shared *between* modules,
# not the price of using one.
def test_template_and_fragment_are_resolved_as_local_names(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            "def go():\n"
            f"    tmpl = {TEMPLATE}\n"
            f"    frag = {FRAGMENT}\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    bind = package.binds[0]
    assert bind.template.raw_text == "query Q { f @slot { __typename } }"
    assert [stmt.raw_text for _, stmts in bind.slot_args for stmt in stmts] == [
        "fragment F on T { x }"
    ]


# The template a caller sees is the one its own scope binds. Resolving the
# module-level name of the same spelling would generate a class for a
# combination nobody wrote, and the runtime — which keys on the handles it is
# actually passed — would then fail to find it.
def test_a_local_name_shadows_the_module_level_one(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f'tmpl = api_gql("query Outer {{ f @slot {{ __typename }} }}")\n'
            f"frag = {FRAGMENT}\n"
            "def go():\n"
            f'    tmpl = api_gql("query Inner {{ f @slot {{ __typename }} }}")\n'
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == (
        "query Inner { f @slot { __typename } }"
    )


# The same shadowing, with the local name bound to something that is not a
# statement at all: the module-level template must still not answer for it.
# Silence rather than a raise, because this is exactly the shape of an
# unrelated `.bind()` on a local object -- and it is also the line the
# unreachable-template diagnosis stops at: the name *is* bound here, by
# something the call site can see, so what the tree calls its own elsewhere
# says nothing about it.
def test_a_local_non_gql_name_hides_the_module_level_template(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "def go():\n"
            "    tmpl = object()\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert package.binds == []
    [ignored] = package.ignored
    assert "an enclosing function or class body binds that name" in ignored.reason


# Where that line has to hold in practice: two packages generated from one tree
# (an async one and a sync one, as this repository's own example does) each scan
# the other's statements as plain values, so `tmpl = api_sync_gql(...)` binds an
# opaque name during the `api_gql` run. Reading "some scope of the tree assigns
# this name a statement" as proof that a bind meant *that* statement stopped the
# async run over the sync file, and every second run has that shape.
def test_a_name_bound_by_another_runs_gql_call_stays_silent(tmp_path: Path):
    _write(
        tmp_path,
        "app/async_side.py",
        (
            "def go():\n"
            f"    tmpl = {TEMPLATE}\n"
            f"    frag = {FRAGMENT}\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    _write(
        tmp_path,
        "app/sync_side.py",
        (
            "def go():\n"
            '    tmpl = api_sync_gql("query Q { f @slot { __typename } }")\n'
            '    frag = api_sync_gql("fragment F on T { x }")\n'
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    for gql_fn_name, owner, other in (
        ("api_gql", "app/async_side.py", "app/sync_side.py"),
        ("api_sync_gql", "app/sync_side.py", "app/async_side.py"),
    ):
        package = discover_package(tmp_path, gql_fn_name, skip_path=tmp_path / "gql.py")
        assert [bind.location for bind in package.binds] == [f"{owner}:4"]
        [ignored] = package.ignored
        assert ignored.location == f"{other}:4"


# Whether a binding form hides the module-level template is not the walker's
# opinion to hold: Python's own scoping decides it, and a comprehension target
# famously does *not* leak while a `for` target does. So every form below is
# checked against the interpreter itself rather than against an expected answer
# written out here -- an expected answer is exactly what pinned the walker's
# comprehension bug as if it were the specification.
#
# The preamble each form runs against, shared by the oracle and the fixture so
# the two see the same source. `ctx`/`loaders`/`load` stand in for whatever a
# real call site would use: the walker reads the binding form, and the oracle
# needs it to actually execute.
_BINDER_PREAMBLE = """\
import contextlib
def ctx(): return contextlib.nullcontext(object())
def loaders(): return [object(), object()]
def load(): return object()
"""

_BINDER_FORMS = [
    "    for tmpl in loaders():\n        pass\n",
    "    with ctx() as tmpl:\n        pass\n",
    "    try:\n        pass\n    except ValueError as tmpl:\n        pass\n",
    "    match load():\n        case object() as tmpl:\n            pass\n",
    "    match load():\n        case [*tmpl]:\n            pass\n",
    "    match load():\n        case {'k': 1, **tmpl}:\n            pass\n",
    "    tmpls = [tmpl for tmpl in loaders()]\n",
    "    tmpls = {tmpl for tmpl in loaders()}\n",
    "    tmpls = {tmpl: 1 for tmpl in loaders()}\n",
    "    tmpls = tuple(tmpl for tmpl in loaders())\n",
    "    tmpls = [tmpl for _a in loaders() for tmpl in loaders()]\n",
    "    tmpl, _other = loaders()\n",
    "    *tmpl, _other = loaders()\n",
    "    tmpl = loaders()\n    del tmpl\n",
    "    type tmpl = int\n",
    "    from app.other import tmpl\n",
    "    import tmpl\n",
    # PEP 526: a bare annotation in a function body binds the name even though
    # it assigns nothing, so the module-level template is unreachable from
    # there. Reading it as a declaration that binds nothing generated a class
    # for a call that raises UnboundLocalError.
    "    tmpl: object\n",
    # PEP 572: a walrus binds in the scope the comprehension is written in, not
    # in the comprehension's own — the mirror image of the `for` target one
    # line up, which does not leak.
    "    tmpls = [(tmpl := load()) for _ in loaders()]\n",
    "    tmpls = {(tmpl := load()) for _ in loaders()}\n",
    "    tmpls = tuple((tmpl := load()) for _ in loaders())\n",
    "    tmpls = [_ for _ in loaders() if (tmpl := load())]\n",
]

# The same question asked of the definition's own header rather than its body:
# a star parameter and a PEP 695 type parameter both bind inside the function
# without appearing anywhere in it. All three kinds of type parameter are here,
# because all three bind a name and only one of them is spelled `TypeVar` in
# the AST.
_SIGNATURE_FORMS = [
    "def go(*tmpl):",
    "def go(**tmpl):",
    "def go[tmpl]():",
    "def go[*tmpl]():",
    "def go[**tmpl]():",
]


# The one line each fixture below ends on, kept out of the f-strings that build
# those fixtures so neither has to concatenate.
_BIND_RETURN = "    return tmpl.bind(f=frag)\n"
_BIND_ATTRIBUTE = "    bound = tmpl.bind(f=frag)\n"


def _module_level_is_visible(binder: str, root: Path) -> bool:
    # The oracle: run the very shape the fixture below discovers, with the
    # template replaced by a sentinel, and ask the interpreter what the call
    # site sees. `NameError`/`UnboundLocalError` means the form bound the name
    # locally and the module-level one is unreachable -- the same answer as
    # seeing a different object.
    source = (
        f"{_BINDER_PREAMBLE}SENTINEL = object()\ntmpl = SENTINEL\ndef go():\n"
        + binder
        + "    return tmpl is SENTINEL\n"
    )
    namespace: dict[str, object] = {}
    sys.path.insert(0, str(root))
    try:
        exec(compile(source, "<oracle>", "exec"), namespace)
        probe = namespace["go"]
        assert callable(probe)
        return bool(probe())
    except (NameError, UnboundLocalError):
        return False
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("tmpl", None)


@pytest.mark.parametrize("binder", _BINDER_FORMS)
def test_binding_form_scoping_agrees_with_the_interpreter(tmp_path: Path, binder: str):
    # The walker resolves `tmpl` by reading the AST; the interpreter resolves
    # it by running. A form the walker thinks hides the module-level template
    # while Python leaves it visible costs the user a bind that never
    # generates -- or, worse, a hard "binds it more than once" error over a
    # collision that does not exist at runtime.
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/other.py", "tmpl = object()\n")
    _write(tmp_path, "tmpl.py", "")
    _write(
        tmp_path,
        "app/mod.py",
        f"{_BINDER_PREAMBLE}tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\ndef go():\n"
        + binder
        + "    return tmpl.bind(f=frag)\n",
    )
    expected_binds = 1 if _module_level_is_visible(binder, tmp_path) else 0
    package = _discover(tmp_path)
    assert len(package.binds) == expected_binds


# The mirror of the case above: a target that binds no name of its own leaves
# the module-level template reachable.
@pytest.mark.parametrize(
    "binder",
    [
        "    holder.tmpl = loaders()\n",
        "    holder['tmpl'] = loaders()\n",
    ],
)
def test_an_attribute_or_subscript_target_binds_nothing(tmp_path: Path, binder: str):
    _write(
        tmp_path,
        "app/mod.py",
        f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\ndef go(holder):\n"
        + binder
        + "    return tmpl.bind(f=frag)\n",
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1


@pytest.mark.parametrize("signature", _SIGNATURE_FORMS)
def test_signature_scoping_agrees_with_the_interpreter(tmp_path: Path, signature: str):
    # Same oracle as the binder forms, asked of the header: `go()` is called
    # with no arguments, so a star parameter holds an empty container and a
    # PEP 695 type parameter holds a TypeVar -- either way not the template,
    # and the walker has to agree.
    source = (
        f"SENTINEL = object()\ntmpl = SENTINEL\n{signature}\n"
        "    return tmpl is SENTINEL\n"
    )
    namespace: dict[str, object] = {}
    exec(compile(source, "<oracle>", "exec"), namespace)
    probe = namespace["go"]
    assert callable(probe)
    visible = bool(probe())

    _write(
        tmp_path,
        "app/mod.py",
        f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\n{signature}\n{_BIND_RETURN}",
    )
    package = _discover(tmp_path)
    assert len(package.binds) == (1 if visible else 0)


_CLASS_TYPE_PARAM_ORACLE = """\
SENTINEL = object()
tmpl = SENTINEL
class C[tmpl]:
    visible = tmpl is SENTINEL
seen = C.visible
"""


_LOCAL_FRAG_IMPORT = "    from app.frags import image_parts\n"
_LOCAL_TMPL_IMPORT = "    from app.templates import tmpl\n"
_BIND_LOCAL_FRAG = "    return tmpl.bind(f=image_parts)\n"
_REBIND_AND_BIND = "tmpl = None\nbound = tmpl.bind(f=frag)\n"
_MODULE_LEVEL_BIND = "bound = tmpl.bind(f=frag)\n"


def test_a_function_local_import_resolves_as_a_slot_argument(tmp_path: Path):
    # A local import is the standard way to break an import cycle, and the
    # README lists a directly imported name as a supported spelling. Recording
    # it as "bound, not ours" made it a hard error at the one place a slot
    # argument may not fail.
    _write(tmp_path, "app/frags.py", f"image_parts = {FRAGMENT}\n")
    _write(
        tmp_path,
        "app/mod.py",
        f"tmpl = {TEMPLATE}\ndef make():\n{_LOCAL_FRAG_IMPORT}{_BIND_LOCAL_FRAG}",
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert [s.raw_text for _, stmts in package.binds[0].slot_args for s in stmts] == [
        "fragment F on T { x }"
    ]


def test_a_function_local_import_resolves_as_the_template(tmp_path: Path):
    # The mirror: a locally imported *base* used to read as "not ours" and the
    # bind was dropped without a word, so the call site raised LookupError at
    # runtime instead of generating a class.
    _write(tmp_path, "app/templates.py", f"tmpl = {TEMPLATE}\n")
    _write(
        tmp_path,
        "app/mod.py",
        f"frag = {FRAGMENT}\ndef make():\n{_LOCAL_TMPL_IMPORT}{_BIND_RETURN}",
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"
    assert package.ignored == []


def test_a_module_level_name_rebound_after_an_import_is_a_hard_error(tmp_path: Path):
    # An import is one of the bindings the module scope holds, not a table
    # consulted when the others say nothing: a name the module also assigns
    # cannot be resolved through the import as if that assignment were absent.
    # And the import leads to a discovered statement, which makes the collision
    # ours to report -- reading it as "not ours" is how the name that reaches a
    # template through an import ended up in the same silence a third-party
    # `.bind()` gets.
    _write(tmp_path, "app/templates.py", f"tmpl = {TEMPLATE}\n")
    _write(
        tmp_path,
        "app/mod.py",
        f"from app.templates import tmpl\nfrag = {FRAGMENT}\n{_REBIND_AND_BIND}",
    )
    with pytest.raises(TypeError, match="exactly one binding") as exc:
        _discover(tmp_path)
    assert "app/mod.py:4" in str(exc.value)


def test_a_class_type_parameter_hides_the_module_level_template(tmp_path: Path):
    # The class half of PEP 695, asked the same way: inside `class C[tmpl]`
    # the name is the type parameter, so the module-level template is not what
    # the class body sees.
    namespace: dict[str, object] = {}
    exec(
        compile(
            _CLASS_TYPE_PARAM_ORACLE,
            "<oracle>",
            "exec",
        ),
        namespace,
    )
    assert namespace["seen"] is False

    _write(
        tmp_path,
        "app/mod.py",
        f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\nclass C[tmpl]:\n{_BIND_ATTRIBUTE}",
    )
    package = _discover(tmp_path)
    assert package.binds == []


# A lambda's parameter sits outside the plain parameter list and has no body
# the oracle above can run a `return` in, so it keeps its own direct pin.
@pytest.mark.parametrize(
    "body",
    ["go = lambda tmpl: tmpl.bind(f=frag)\n"],
)
def test_a_parameter_hides_the_module_level_template(tmp_path: Path, body: str):
    _write(
        tmp_path,
        "app/mod.py",
        (f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\n" + body),
    )
    package = _discover(tmp_path)
    assert package.binds == []


# `nonlocal` hands the name back to the enclosing function, so resolution has
# to follow it out of the scope that declared it -- the same way `global`
# hands it to the module.
def test_nonlocal_declaration_resolves_in_the_enclosing_function(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"frag = {FRAGMENT}\n"
            "def outer():\n"
            f"    tmpl = {TEMPLATE}\n"
            "    def inner():\n"
            "        nonlocal tmpl\n"
            "        return tmpl.bind(f=frag)\n"
            "    return inner\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# Resolution reads a name where exactly one binding gives it its value. A
# second binding makes the answer a flow question, and guessing it would
# silently generate a binding for a combination the call site may never reach.
@pytest.mark.parametrize(
    "body",
    [
        # The reassigned name is the template...
        (
            "def go():\n"
            f"    tmpl = {TEMPLATE}\n"
            f'    tmpl = api_gql("query Other {{ f @slot {{ __typename }} }}")\n'
            f"    frag = {FRAGMENT}\n"
            "    return tmpl.bind(f=frag)\n"
        ),
        # ...or one of its fragments.
        (
            "def go():\n"
            f"    tmpl = {TEMPLATE}\n"
            f"    frag = {FRAGMENT}\n"
            f'    frag = api_gql("fragment G on T {{ y }}")\n'
            "    return tmpl.bind(f=frag)\n"
        ),
        # A conditional reassignment is the same question, which is why the
        # rule counts assignments instead of trying to follow branches.
        (
            "def go(flag):\n"
            f"    tmpl = {TEMPLATE}\n"
            "    if flag:\n"
            f'        tmpl = api_gql("query Other {{ f @slot {{ __typename }} }}")\n'
            f"    frag = {FRAGMENT}\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    ],
)
def test_a_reassigned_name_is_a_hard_error(tmp_path: Path, body: str):
    _write(tmp_path, "app/mod.py", body)
    with pytest.raises(TypeError, match="exactly one binding"):
        _discover(tmp_path)


def test_the_same_fragment_twice_in_one_slot_is_a_hard_error(tmp_path: Path):
    # README's "what the generator rejects": a slot spreads each of its
    # fragments once, so this combination cannot exist. It used to generate a
    # real binding whose expanded operation carried the spread twice and whose
    # overload unioned the handle class with itself.
    _write(
        tmp_path,
        "app/mod.py",
        "".join([
            f"tmpl = {TEMPLATE}\n",
            f"frag = {FRAGMENT}\n",
            "bound = tmpl.bind(f=[frag, frag])\n",
        ]),
    )
    with pytest.raises(TypeError, match="more than once"):
        _discover(tmp_path)


def test_two_different_fragments_in_one_slot_stay_legal(tmp_path: Path):
    # The mirror: the rule is about repetition, not about list binds.
    _write(
        tmp_path,
        "app/mod.py",
        "".join([
            f"tmpl = {TEMPLATE}\n",
            f"one = {FRAGMENT}\n",
            'two = api_gql("fragment G on T { y }")\n',
            "bound = tmpl.bind(f=[one, two])\n",
        ]),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert [s.raw_text for _, stmts in package.binds[0].slot_args for s in stmts] == [
        "fragment F on T { x }",
        "fragment G on T { y }",
    ]


def test_a_reassigned_module_level_name_is_a_hard_error(tmp_path: Path):
    # The same rule one scope out. Module level reaches it through the module
    # graph rather than the lexical walk, so it is a second path to the same
    # diagnosis and not covered by the function-scope cases above.
    _write(
        tmp_path,
        "app/mod.py",
        "".join([
            f"tmpl = {TEMPLATE}\n",
            f"tmpl = {TEMPLATE}\n",
            f"frag = {FRAGMENT}\n",
            _MODULE_LEVEL_BIND,
        ]),
    )
    with pytest.raises(TypeError, match="exactly one binding"):
        _discover(tmp_path)


# A template and its fragments can be written straight into the bind. This is
# the same freedom every other statement in the package has: `api_gql(...)` is
# an expression, and naming it is the caller's choice, not a requirement.
def test_inline_template_and_fragments_are_resolved(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            "def go():\n"
            f"    return {TEMPLATE}.bind(\n"
            f'        f=[{FRAGMENT}, api_gql("fragment G on T {{ y }}")]\n'
            "    )\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    bind = package.binds[0]
    assert bind.template.raw_text == "query Q { f @slot { __typename } }"
    assert [stmt.raw_text for _, stmts in bind.slot_args for stmt in stmts] == [
        "fragment F on T { x }",
        "fragment G on T { y }",
    ]
    # The inline statements are discovered like any others, so the template
    # and both fragments reach the package on their own too.
    assert len(package.statements) == 3


# A bind is read from the `.bind(...)` call itself, not from the statement it
# is part of, so chaining onto its result is just as readable as assigning it.
def test_a_bind_chained_into_another_call_is_resolved(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "bound = tmpl.bind(f=frag).with_args(width=1)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# A slot's fragments may be named and inline in the same list: both spellings
# resolve to a statement, which is all a slot argument ever needed to be.
def test_named_and_inline_fragments_mix_in_one_slot(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            'bound = tmpl.bind(f=[frag, api_gql("fragment G on T { y }")])\n'
        ),
    )
    package = _discover(tmp_path)
    assert [
        stmt.raw_text for _, stmts in package.binds[0].slot_args for stmt in stmts
    ] == ["fragment F on T { x }", "fragment G on T { y }"]


# One combination is one binding, however many places write it. Without this,
# a bind inside a function body could not be legal at all: shared code and its
# caller reaching the same combination independently is the normal case, not a
# conflict to report.
def test_the_same_combination_written_twice_is_one_bind(tmp_path: Path):
    _write(tmp_path, "app/tmpl.py", f"tmpl = {TEMPLATE}\n")
    _write(tmp_path, "app/frags.py", f"frag = {FRAGMENT}\n")
    _write(
        tmp_path,
        "app/a.py",
        (
            "from app.frags import frag\n"
            "from app.tmpl import tmpl\n"
            "first = tmpl.bind(f=frag)\n"
        ),
    )
    _write(
        tmp_path,
        "app/b.py",
        (
            "from app.frags import frag\n"
            "from app.tmpl import tmpl\n"
            "def go():\n"
            # The list spelling of a single fragment is the same combination:
            # the key sorts and unwraps before anything compares it.
            "    return tmpl.bind(f=[frag])\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].locations == ("app/a.py:3", "app/b.py:4")


# `global` and `nonlocal` say the name is not this scope's, so resolution has
# to follow them out instead of reading the assignment it can see.
def test_global_declaration_resolves_at_module_level(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "def go():\n"
            "    global tmpl\n"
            "    tmpl = load()\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# A class body's names are invisible to its methods, so a method resolves past
# them, exactly as Python does.
def test_a_method_does_not_see_the_class_body_name(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "class Reader:\n"
            "    tmpl = object()\n"
            "    def go(self):\n"
            "        return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# A parameter binds its name too: the bind is ours (its base resolves), so the
# value it cannot resolve is a hard error rather than silence.
def test_a_parameter_shadowing_a_fragment_is_a_hard_error(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "def go(frag):\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match="cannot resolve 'frag'"):
        _discover(tmp_path)


def test_bind_resolved_through_import_chain(tmp_path: Path):
    _write(
        tmp_path,
        "app/infra.py",
        ('tmpl = api_gql("query Q { f @slot { __typename } }")\n'),
    )
    _write(tmp_path, "app/reexport.py", "from app.infra import tmpl as template\n")
    _write(
        tmp_path,
        "app/user.py",
        (
            "from app.reexport import template\n"
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = template.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    bind = package.binds[0]
    assert bind.locations == ("app/user.py:3",)
    assert bind.template.raw_text == "query Q { f @slot { __typename } }"
    assert [kwarg for kwarg, _ in bind.slot_args] == ["f"]
    assert [s.raw_text for _, stmts in bind.slot_args for s in stmts] == [
        "fragment F on T { x }"
    ]


def test_bind_list_literal_of_names(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
            'a = api_gql("fragment A on T { x }")\n'
            'b = api_gql("fragment B on T { y }")\n'
            "bound = tmpl.bind(f=[a, b])\n"
        ),
    )
    package = _discover(tmp_path)
    names = [s.raw_text for s in package.binds[0].slot_args[0][1]]
    assert names == ["fragment A on T { x }", "fragment B on T { y }"]


# The chain reaches the template through a *local* module import — the standard
# way to break an import cycle. An import binds a name where it is written, so
# reading the module binding off a module-level side table left this bind
# unrecognized and silently ignored, while the very same chain one line up was
# diagnosed.
_LOCAL_MODULE_IMPORT_CHAIN = (
    "def make():\n    import app.infra as infra\n    return infra.tmpl.bind(f=frag)\n"
)


# Two kinds of raise here:
#   - the base resolves to a discovered gql statement, whether through a name
#     or written inline (this bind is confirmed ours), and something about its
#     own shape is wrong; or
#   - the base is an attribute chain and *both* of its halves are ours: the
#     prefix names a module of the scanned tree, and the name behind the dot
#     resolves in that module to a discovered statement. Neither half alone
#     proves ownership — a scanned module holds sockets and widgets next to its
#     templates, and `.bind()` is an ordinary method name on all of them.
@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("bound = tmpl.bind(frag)\n", "keyword"),
        ("bound = tmpl.bind(f=[frag][0])\n", "must be a fragment name"),
        # `**kwargs` expansion has no keyword name to thread through the
        # resolver, so it is rejected the same way a positional argument is.
        ("bound = tmpl.bind(**{'f': frag})\n", "keyword"),
        # The base ('tmpl') resolves fine, so this bind is confirmed ours —
        # an unresolvable *value* name is still a hard error even though an
        # unresolvable *base* is now silently ignored (see the "ignored"
        # test below).
        ("bound = tmpl.bind(f=unknown_frag)\n", "resolve"),
        # Base is a template written inline — unambiguously ours by shape, so
        # a value that does not resolve is a hard error here too, with no name
        # for the base to hide behind.
        (
            "bound = api_gql('query Q { f @slot { __typename } }').bind(f=x)\n",
            "resolve",
        ),
        # Base is an attribute chain whose prefix names a scanned module —
        # unambiguously ours by shape (the unsupported form users hit when
        # they reach a module instead of importing the template name). Every
        # spelling that binds a module has to be caught, not just the one that
        # happens to spell the module's full dotted path at the call site:
        # reading that path off the chain alone dropped the aliased forms
        # without a word, and the generated LookupError they hit at import
        # time told them to regenerate, which never helped.
        (
            "import app.infra\nbound = app.infra.tmpl.bind(f=frag)\n",
            r"import the template name directly",
        ),
        (
            "import app.infra as infra\nbound = infra.tmpl.bind(f=frag)\n",
            r"import the template name directly",
        ),
        (
            "from app import infra\nbound = infra.tmpl.bind(f=frag)\n",
            r"import the template name directly",
        ),
        (
            "from . import infra\nbound = infra.tmpl.bind(f=frag)\n",
            r"import the template name directly",
        ),
        (_LOCAL_MODULE_IMPORT_CHAIN, r"import the template name directly"),
    ],
)
def test_malformed_bind_forms_raise(tmp_path: Path, body: str, match: str):
    _write(tmp_path, "app/infra.py", 'tmpl = api_gql("query Q { f }")\n')
    _write(
        tmp_path,
        "app/mod.py",
        'from app.infra import tmpl\nfrag = api_gql("fragment F on T { x }")\n' + body,
    )
    with pytest.raises(TypeError, match=match):
        _discover(tmp_path)


# `ast.parse` alone does not reject `bind(f=a, f=b)` the way `compile()`
# would (that check lives in the compiler, not the parser) — so this must be
# caught explicitly, or the second value silently wins in `_validated_bind`'s
# `slot_args` list and the first fragment vanishes from the generated bind
# with no diagnostic at all.
def test_repeated_slot_kwarg_is_rejected(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
            'frag_a = api_gql("fragment A on T { x }")\n'
            'frag_b = api_gql("fragment B on T { y }")\n'
            "bound = tmpl.bind(f=frag_a, f=frag_b)\n"
        ),
    )
    with pytest.raises(TypeError, match=r"repeats keyword 'f'"):
        _discover(tmp_path)


# A `.bind(...)` call whose base is a plain name the scan cannot resolve *and*
# the tree assigns no statement to is a third-party call to leave untouched:
# from where this scan stands there is nothing to tell it from one. The name is
# the whole evidence -- spell it like one of our statements and the call is
# diagnosed instead (see the unreachable-template tests).
@pytest.mark.parametrize(
    "body",
    [
        # Base is a Name, but the module it is imported from was never scanned.
        "from app.missing import other\nbound = other.bind(f=frag)\n",
        # Base is a Name that was never imported or assigned at all.
        "bound = nope.bind(f=frag)\n",
    ],
)
def test_unresolvable_or_unrelated_bind_is_ignored(tmp_path: Path, body: str):
    _write(tmp_path, "app/infra.py", 'tmpl = api_gql("query Q { f }")\n')
    _write(
        tmp_path,
        "app/mod.py",
        'from app.infra import tmpl\nfrag = api_gql("fragment F on T { x }")\n' + body,
    )
    package = _discover(tmp_path)
    assert package.binds == []
    # Ignored on purpose, not lost: the reason distinguishes the two, which an
    # empty `binds` on its own cannot.
    [ignored] = package.ignored
    assert "cannot resolve" in ignored.reason


def test_plain_statements_still_discovered(tmp_path: Path):
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    package = _discover(tmp_path)
    assert [s.raw_text for s in package.statements] == ["query Q { f }"]
    assert package.binds == []


# Base resolution failure (whatever the cause) is swallowed as "not ours",
# so a chain that only cycles when resolving the *base* is silently ignored,
# not raised — the same laziness rule applies here too.
def test_circular_import_chain_in_base_is_ignored(tmp_path: Path):
    _write(tmp_path, "app/a.py", "from app.b import y as tmpl\n")
    _write(tmp_path, "app/b.py", "from app.a import tmpl as y\n")
    _write(
        tmp_path,
        "app/mod.py",
        (
            "from app.a import tmpl\n"
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert package.binds == []
    [ignored] = package.ignored
    assert "circular import chain" in ignored.reason


# Once the base resolves (this bind is confirmed ours), a cycle while
# resolving a *value* name is a hard error — the resolver is
# "cycle-safe via a visited set", pinned here through the branch that a
# resolvable base cannot reach.
def test_circular_import_chain_in_value_raises(tmp_path: Path):
    _write(
        tmp_path,
        "app/infra.py",
        'tmpl = api_gql("query Q { f @slot { __typename } }")\n',
    )
    _write(tmp_path, "app/a.py", "from app.b import y as frag\n")
    _write(tmp_path, "app/b.py", "from app.a import frag as y\n")
    _write(
        tmp_path,
        "app/mod.py",
        (
            "from app.infra import tmpl\n"
            "from app.a import frag\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match="circular"):
        _discover(tmp_path)


# Same split as the circular-chain pair above: a star import that only
# affects the *base* is ignored (base doesn't resolve => not ours), one that
# affects a *value* is a hard error (bind confirmed ours, broken operand).
def test_star_import_in_value_raises(tmp_path: Path):
    _write(
        tmp_path,
        "app/infra.py",
        'tmpl = api_gql("query Q { f @slot { __typename } }")\n',
    )
    _write(tmp_path, "app/lib.py", 'frag = api_gql("fragment F on T { x }")\n')
    _write(
        tmp_path,
        "app/mod.py",
        (
            "from app.infra import tmpl\n"
            "from app.lib import *\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match="star imports"):
        _discover(tmp_path)


# Same as above, but the star import is itself relative — a `from .lib
# import *` records `star_imports` through the relative-import branch, not
# just the absolute one.
def test_relative_star_import_in_value_raises(tmp_path: Path):
    _write(tmp_path, "app/__init__.py", "")
    _write(
        tmp_path,
        "app/infra.py",
        'tmpl = api_gql("query Q { f @slot { __typename } }")\n',
    )
    _write(tmp_path, "app/lib.py", 'frag = api_gql("fragment F on T { x }")\n')
    _write(
        tmp_path,
        "app/mod.py",
        ("from app.infra import tmpl\nfrom .lib import *\nbound = tmpl.bind(f=frag)\n"),
    )
    with pytest.raises(TypeError, match="star imports"):
        _discover(tmp_path)


# The plan author's note calls out that unrelated third-party `.bind()`
# calls must never be touched; this pins the non-module-level angle
# specifically (a `.bind()` used as a bare expression, not an assignment at
# all) on top of the module-level cases above.
def test_unrelated_nested_bind_call_is_ignored(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            'q = api_gql("query Q { f }")\n'
            "def go():\n"
            "    unrelated = object()\n"
            "    return unrelated.bind(x=1)\n"
        ),
    )
    package = _discover(tmp_path)
    assert package.binds == []


# An annotated assignment binds its target exactly like a plain one, so a
# statement written that way is discovered and resolves the same.
def test_annotated_gql_assignment_is_resolved(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            'tmpl: object = api_gql("query Q { f @slot { __typename } }")\n'
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# A bind is an expression like any other: what it is assigned to, or whether
# it is assigned at all, decides nothing. The binding class is named after the
# combination, so there is no target name left for a call site to have to
# supply.
@pytest.mark.parametrize(
    "body",
    [
        "bound: object = tmpl.bind(f=frag)\n",
        "tmpl.bind(f=frag)\n",
        "def go():\n    return tmpl.bind(f=frag)\n",
        "def go():\n    return use(tmpl.bind(f=frag))\n",
        "class Reader:\n    bound = tmpl.bind(f=frag)\n",
        "class Reader:\n    def go(self):\n        return tmpl.bind(f=frag)\n",
    ],
)
def test_bind_is_resolved_wherever_it_is_written(tmp_path: Path, body: str):
    _write(
        tmp_path,
        "app/mod.py",
        f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\n" + body,
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# A bare annotation with no value is a declaration, not an assignment: it
# must not be treated as a gql or bind statement, and must not error.
def test_bare_annotation_without_value_is_skipped(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        ('bound: object\nq = api_gql("query Q { f }")\n'),
    )
    package = _discover(tmp_path)
    assert [s.raw_text for s in package.statements] == ["query Q { f }"]
    assert package.binds == []


# A file the scan cannot parse aborts it whenever it could own something the
# package would lose -- which is decided by whether it names the gql function
# or `.bind(` at all, since nothing short of parsing tells more than that.
def test_syntax_error_in_a_file_naming_the_gql_function_aborts_the_scan(
    tmp_path: Path,
):
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "app/unrelated.py", 'api_gql("query R { f }")\ndef broken(\n')
    with pytest.raises(SyntaxError, match=r"Failed to parse .*unrelated\.py"):
        _discover(tmp_path)


def test_syntax_error_in_a_file_that_owns_nothing_does_not_abort_the_scan(
    tmp_path: Path,
):
    # A tree may hold a snippet, a fixture, or a file written for another
    # interpreter. Naming neither the gql function nor `.bind(`, it can own no
    # statement and no bind, so it has no say in whether this package
    # generates -- aborting on it left the package unregenerable until an
    # unrelated file was fixed or moved out of the scanned tree.
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "app/legacy.py", "print 'hello'\n")
    package = _discover(tmp_path)
    assert [s.raw_text for s in package.statements] == ["query Q { f }"]


def test_a_resolution_reaching_an_unparsable_module_is_a_hard_error(tmp_path: Path):
    # Deferring is not forgetting: a broken file names no gql call, but a
    # chain that travels through it cannot be answered, and answering it as
    # "not ours" would drop a binding the file may be re-exporting.
    _write(tmp_path, "app/broken.py", "def broken(\n")
    _write(
        tmp_path,
        "app/mod.py",
        "".join([
            "from app.broken import tmpl\n",
            f"frag = {FRAGMENT}\n",
            _MODULE_LEVEL_BIND,
        ]),
    )
    with pytest.raises(SyntaxError, match=r"failed to parse"):
        _discover(tmp_path)


def test_statements_of_an_unparsable_file_are_never_silently_dropped(tmp_path: Path):
    # The failure the abort exists for: the broken file carries the only
    # definition of `q`, and a scan that skipped it would report success with
    # `q` gone from the generated package.
    _write(tmp_path, "app/mod.py", 'other = api_gql("query Other { f }")\n')
    _write(tmp_path, "app/broken.py", 'q = api_gql("query Q { f }")\ndef broken(\n')
    with pytest.raises(SyntaxError, match=r"Failed to parse .*broken\.py"):
        _discover(tmp_path)


# `Path.glob` walks the tree in whatever order the filesystem hands back, and
# every consumer downstream -- the IR, the rendered binding classes, the text
# of a collision diagnosis, the generated file's diff between two machines --
# inherits the order established here. The template lives in the
# alphabetically last file on purpose, so "whatever order the scan produced"
# cannot pass by accident.
def test_binds_are_returned_in_file_then_lineno_order(tmp_path: Path):
    _write(
        tmp_path, "app/z.py", 'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
    )
    _write(
        tmp_path,
        "app/a.py",
        (
            "from app.z import tmpl\n"
            'frag_a = api_gql("fragment A on T { x }")\n'
            'frag_b = api_gql("fragment B on T { y }")\n'
            "a_line_four = tmpl.bind(f=frag_a)\n"
            "a_line_five = tmpl.bind(f=frag_b)\n"
        ),
    )
    _write(
        tmp_path,
        "app/b.py",
        (
            "from app.z import tmpl\n"
            'frag_c = api_gql("fragment C on T { z }")\n'
            "b_bind = tmpl.bind(f=frag_c)\n"
        ),
    )
    package = _discover(tmp_path)
    assert [bind.location for bind in package.binds] == [
        "app/a.py:4",
        "app/a.py:5",
        "app/b.py:3",
    ]


# `.bind(...)` is an ordinary method name — sockets, tkinter widgets and LDAP
# connections all have one — so an attribute chain is only evidence of
# ownership when its prefix names a module of the scanned tree. Rejecting on
# the chain's shape alone made every `self.x.bind(...)` in the scanned tree
# stop generation with a message about GraphQL templates.
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            "class S:\n    def go(self):\n        self.sock.bind((self.host, 1))\n",
            "names no module of the scanned tree",
        ),
        # The base is a call rather than a dotted name, so no resolution can be
        # attempted at all -- which is recorded as its own reason rather than
        # dropped, so this call is still accounted for.
        (
            "import socket\n\n\ndef go(s):\n    socket.socket().bind(('', 1))\n",
            "neither a name nor an inline",
        ),
        # The chain's head *is* an imported module, but a third-party one:
        # nothing under it was ever scanned, so this cannot be our template.
        (
            "import logging\n\n\ndef go():\n    logging.root.bind(x=1)\n",
            "names no module of the scanned tree",
        ),
        # The head is bound nowhere the scan can see -- a name from a star
        # import, or simply a NameError waiting to happen. Either way it names
        # no module of ours.
        (
            "def go():\n    return unbound.tmpl.bind(x=1)\n",
            "names no module of the scanned tree",
        ),
    ],
)
def test_unrelated_attribute_chain_bind_is_ignored(
    tmp_path: Path, body: str, reason: str
):
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "app/server.py", body)
    package = _discover(tmp_path)
    assert [s.raw_text for s in package.statements] == ["query Q { f }"]
    assert package.binds == []
    [ignored] = package.ignored
    assert reason in ignored.reason


# The other half of that rule, and the half a prefix check alone cannot see: a
# module *of the scanned tree* holds ordinary objects next to its templates,
# and every one of them may carry a `.bind`. The name behind the dot decides,
# so it has to be resolved before anything is said about the chain — reading
# ownership off the prefix stopped generation of the whole package over a
# socket, and did it for a name the module does not even define.
@pytest.mark.parametrize(
    "chain",
    [
        "import app.net\n\n\ndef go():\n    app.net.sock.bind(('', 1))\n",
        "from app import net\n\n\ndef go():\n    net.sock.bind(('', 1))\n",
        # Not defined in `app.net` at all: an attribute error where the code
        # runs, and nothing this scan may claim is a GraphQL template.
        "from app import net\n\n\ndef go():\n    net.absent.bind(('', 1))\n",
    ],
)
def test_a_non_statement_behind_a_scanned_module_chain_is_ignored(
    tmp_path: Path, chain: str
):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/net.py", "import socket\n\nsock = socket.socket()\n")
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "app/server.py", chain)
    package = _discover(tmp_path)
    assert [s.raw_text for s in package.statements] == ["query Q { f }"]
    assert package.binds == []
    [ignored] = package.ignored
    assert "cannot resolve" in ignored.reason


# A name spelled like an imported module, bound to something else where the
# call site stands, is not that module — Python resolves the head of a chain
# by the same scope rules as any other name. Answering from a module-level
# import table regardless made a parameter named after a module of the tree
# stop generation.
@pytest.mark.parametrize(
    "body",
    [
        "import app.ui\n\n\ndef go(app):\n    app.ui.widget.bind('<Key>', print)\n",
        "from app import ui\n\n\ndef go(ui):\n    ui.widget.bind('<Key>', print)\n",
    ],
)
def test_a_locally_shadowed_module_name_is_not_the_module(tmp_path: Path, body: str):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/ui.py", "widget = object()\n")
    _write(tmp_path, "app/server.py", body)
    package = _discover(tmp_path)
    assert package.binds == []
    [ignored] = package.ignored
    assert "names no module of the scanned tree" in ignored.reason


# A module directly under the scanned root has an empty package name, so
# `from .frags import x` there climbs to the root itself. Assembling the
# resolved name by concatenation gave it a leading dot ('.frags'), which
# matches no scanned module -- the template-side spelling of the same bug then
# dropped the bind silently and only surfaced as a LookupError at import time.
def test_relative_import_at_the_scanned_root_resolves(tmp_path: Path):
    _write(tmp_path, "__init__.py", "")
    _write(tmp_path, "frags.py", 'frag = api_gql("fragment F on T { x }")\n')
    _write(
        tmp_path, "tmpl.py", 'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
    )
    _write(
        tmp_path,
        "queries.py",
        (
            "from .frags import frag\n"
            "from .tmpl import tmpl\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"
    assert [
        stmt.raw_text for _, stmts in package.binds[0].slot_args for stmt in stmts
    ] == ["fragment F on T { x }"]


# `from .mod import x` resolves relative to the importing module's own
# package; recording it as the absolute module "mod" loses the bind.
def test_bind_resolved_through_relative_import_chain(tmp_path: Path):
    _write(tmp_path, "app/__init__.py", "")
    _write(
        tmp_path,
        "app/infra.py",
        'tmpl = api_gql("query Q { f @slot { __typename } }")\n',
    )
    _write(tmp_path, "app/reexport.py", "from .infra import tmpl as template\n")
    _write(
        tmp_path,
        "app/user.py",
        (
            "from .reexport import template\n"
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = template.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# The other relative-import form: `from . import x` (no submodule) resolves
# to the importing module's own package.
def test_bind_resolved_through_bare_dot_import(tmp_path: Path):
    _write(tmp_path, "app/__init__.py", 'tmpl = api_gql("query Q { f }")\n')
    _write(
        tmp_path,
        "app/user.py",
        (
            "from . import tmpl\n"
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f }"


# Round 2 cleanup: pin the "climbs above the scanned root" path rather than
# only reasoning about it. `app` is a top-level package with no parent, so
# `from .. import frag` (level 2) climbs one package past what exists; the
# computed base must not accidentally alias a real scanned module, and must
# surface as "outside the scanned tree" like any other import that points
# outside the tree. Checked through a *value* name so the failure is a hard
# raise (the base, `tmpl`, resolves fine — this bind is confirmed ours).
def test_relative_import_above_root_is_outside_scanned_tree(tmp_path: Path):
    _write(
        tmp_path,
        "app/infra.py",
        'tmpl = api_gql("query Q { f @slot { __typename } }")\n',
    )
    _write(
        tmp_path,
        "app/mod.py",
        (
            "from app.infra import tmpl\n"
            "from .. import frag\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match="outside the scanned tree"):
        _discover(tmp_path)


def test_a_module_chain_through_a_root_relative_import_is_diagnosed(tmp_path: Path):
    # `from . import infra` at the scanned root resolves against an empty
    # package name, so the module it binds is just `infra` -- joining blindly
    # would look for `.infra`, match no scanned module, and drop the bind
    # without a word.
    _write(tmp_path, "__init__.py", "")
    _write(
        tmp_path, "infra.py", 'tmpl = api_gql("query Q { f @slot { __typename } }")\n'
    )
    _write(
        tmp_path,
        "queries.py",
        (
            "from . import infra\n"
            'frag = api_gql("fragment F on T { x }")\n'
            "bound = infra.tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match=r"import the template name directly"):
        _discover(tmp_path)


# PEP 572 moves *where* a name is written, not *what* it holds: the walrus
# spelling of `tmpl = api_gql(...)` assigns the same statement to the same name.
# Recording it as opaque hid the template from the very call site that had just
# written it, and the bind vanished with the scan still reporting success.
@pytest.mark.parametrize(
    "body",
    [
        f"def go():\n    if (tmpl := {TEMPLATE}):\n        return tmpl.bind(f=frag)\n",
        # Written through a comprehension's own scope into the function's, which
        # is the case the walrus is handled separately for at all.
        (
            "def go():\n"
            f"    _seen = [_ for _ in range(1) if (tmpl := {TEMPLATE})]\n"
            "    return tmpl.bind(f=frag)\n"
        ),
        f"if (tmpl := {TEMPLATE}):\n    bound = tmpl.bind(f=frag)\n",
    ],
)
def test_a_walrus_carries_its_statement_into_the_name(tmp_path: Path, body: str):
    _write(tmp_path, "app/mod.py", f"frag = {FRAGMENT}\n" + body)
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# Two imports of one name are two imports, not two assignments -- and reading
# "is one of the bindings ours" off the *form* of each binding left this shape
# looking exactly like a third-party name bound twice. What decides is where
# each binding leads: one of these leads to a discovered statement, so the
# collision is ours to report.
@pytest.mark.parametrize(
    "body",
    [
        (
            "try:\n"
            "    from app.a import tmpl\n"
            "except ImportError:\n"
            "    from app.b import tmpl\n"
            f"frag = {FRAGMENT}\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
        # The same shape one scope in, which reaches the rule through the
        # lexical walk instead of the module graph.
        (
            f"frag = {FRAGMENT}\n"
            "def go():\n"
            "    try:\n"
            "        from app.a import tmpl\n"
            "    except ImportError:\n"
            "        from app.b import tmpl\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    ],
)
def test_a_name_bound_by_two_imports_is_a_hard_error(tmp_path: Path, body: str):
    _write(tmp_path, "app/a.py", f"tmpl = {TEMPLATE}\n")
    _write(tmp_path, "app/b.py", f"tmpl = {TEMPLATE}\n")
    _write(tmp_path, "app/mod.py", body)
    with pytest.raises(TypeError, match="exactly one binding"):
        _discover(tmp_path)


# The mirror, and the reason the rule asks where a binding leads rather than
# counting bindings: a name imported twice from modules this scan never read is
# the ordinary optional-dependency shim, and nothing about it is ours.
def test_two_imports_of_a_name_the_tree_does_not_own_stay_silent(tmp_path: Path):
    _write(tmp_path, "app/mod.py", f"tmpl = {TEMPLATE}\n")
    _write(
        tmp_path,
        "app/shim.py",
        (
            "try:\n"
            "    from ext.fast import conn\n"
            "except ImportError:\n"
            "    from ext.slow import conn\n"
            "bound = conn.bind(('', 1))\n"
        ),
    )
    package = _discover(tmp_path)
    assert package.binds == []
    [ignored] = package.ignored
    assert ignored.location == "app/shim.py:5"


# A class body is a scope nothing outside it resolves into, so `Queries.tmpl`
# reaches a template the tree really holds in a way this call really cannot.
# Answering "names no module of the scanned tree" and moving on dropped the
# bind: the class body is exactly where a project that groups its queries puts
# them.
def test_a_template_held_in_a_class_body_is_diagnosed(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"frag = {FRAGMENT}\n"
            "class Queries:\n"
            f"    tmpl = {TEMPLATE}\n"
            "bound = Queries.tmpl.bind(f=frag)\n"
        ),
    )
    with pytest.raises(TypeError, match=_UNREACHABLE) as exc:
        _discover(tmp_path)
    assert "app/mod.py:3" in str(exc.value)


# A star import brings in names this scan cannot enumerate, so the template it
# carries is unreachable here -- and unreachable is not the same as somebody
# else's. The reason the resolution failed for travels into the diagnosis, so
# the fix (import the name itself) is readable off the message.
def test_a_template_arriving_through_a_star_import_is_diagnosed(tmp_path: Path):
    _write(tmp_path, "app/templates.py", f"tmpl = {TEMPLATE}\n")
    _write(
        tmp_path,
        "app/mod.py",
        f"from app.templates import *\nfrag = {FRAGMENT}\n{_MODULE_LEVEL_BIND}",
    )
    with pytest.raises(TypeError, match=_UNREACHABLE) as exc:
        _discover(tmp_path)
    assert "star imports are not followed" in str(exc.value)
    assert "app/templates.py:1" in str(exc.value)


# The threshold the diagnosis above deliberately sits at: a `.bind()` is left
# alone when the name it hangs off is bound where it stands, or names no
# statement of ours anywhere. Both are the everyday case -- a scan that stopped
# over a socket, a widget or a driver connection would be unusable in any real
# tree -- so they have to stay silent while our own binds go on generating.
def test_third_party_binds_stay_silent_beside_our_own(tmp_path: Path):
    _write(
        tmp_path,
        "app/queries.py",
        f"tmpl = {TEMPLATE}\nfrag = {FRAGMENT}\n{_MODULE_LEVEL_BIND}",
    )
    _write(
        tmp_path,
        "app/net.py",
        "class Server:\n    def listen(self, host):\n        self.sock.bind(host)\n",
    )
    _write(
        tmp_path, "app/ui.py", "def wire(widget):\n    widget.bind('<Key>', print)\n"
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert [entry.location for entry in package.ignored] == [
        "app/net.py:3",
        "app/ui.py:2",
    ]


# `.bind(` in a source file has exactly two outcomes: a binding, or a recorded
# reason it is not one. Neither is a silent third: an empty `binds` list is what
# a lost bind and a tree full of third-party `.bind()` calls looked identical
# through, and the whole point of recording the ignored ones is that they no
# longer do.
def test_every_bind_call_reaches_binds_or_ignored(tmp_path: Path):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/templates.py", f"tmpl = {TEMPLATE}\n")
    _write(
        tmp_path,
        "app/mod.py",
        (
            "from app.templates import tmpl\n"
            f"frag = {FRAGMENT}\n"
            "one = tmpl.bind(f=frag)\n"
            "two = tmpl.bind()\n"
            f"three = {TEMPLATE}.bind(f=frag)\n"
            "four = tmpl.bind(f=frag).with_args(width=1)\n"
            "holder = {'q': tmpl}\n"
            "five = holder['q'].bind(f=frag)\n"
            "six = nowhere.bind(f=frag)\n"
            "def wire(widget, sock):\n"
            "    widget.bind('<Key>', print)\n"
            "    sock.bind(('', 1))\n"
            "    return tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    accounted = {location for bind in package.binds for location in bind.locations} | {
        entry.location for entry in package.ignored
    }
    # Every `.bind(` written on the line its call starts on -- which is every
    # one of them above, so the text of the tree can say what the scan owes.
    written = {
        f"{path.relative_to(tmp_path)}:{lineno}"
        for path in sorted(tmp_path.glob("**/*.py"))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if ".bind(" in line
    }
    assert accounted == written


# README promises that generation fails "naming the file and line" for every
# bind it rejects. A diagnosis that names neither leaves the developer grepping
# a whole tree for a call the scan is looking straight at, so the promise is
# checked against every form of rejection rather than against the handful whose
# text a test happens to quote.
_ATTRIBUTE_CHAIN_BIND = "import app.infra\nbound = app.infra.tmpl.bind(f=frag)\n"
_CYCLIC_VALUE_BIND = "from app.cycle_a import loop\nbound = tmpl.bind(f=loop)\n"


@pytest.mark.parametrize(
    "body",
    [
        # The call's own shape.
        "bound = tmpl.bind(frag)\n",
        "bound = tmpl.bind(**{'f': frag})\n",
        "bound = tmpl.bind(f=frag, f=frag)\n",
        "bound = tmpl.bind(f=[frag][0])\n",
        "bound = tmpl.bind(f=[frag, frag])\n",
        # What one of its names resolves to.
        "bound = tmpl.bind(f=unknown)\n",
        _CYCLIC_VALUE_BIND,
        # What the base resolves to, or fails to.
        _ATTRIBUTE_CHAIN_BIND,
        f"tmpl = {TEMPLATE}\nbound = tmpl.bind(f=frag)\n",
        "from app.hidden import *\nbound = held.bind(f=frag)\n",
        f"class Held:\n    inner = {TEMPLATE}\nbound = Held.inner.bind(f=frag)\n",
    ],
)
def test_every_bind_diagnosis_names_a_file_and_line(tmp_path: Path, body: str):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/infra.py", f"tmpl = {TEMPLATE}\n")
    _write(tmp_path, "app/hidden.py", f"held = {TEMPLATE}\n")
    _write(tmp_path, "app/cycle_a.py", "from app.cycle_b import ring as loop\n")
    _write(tmp_path, "app/cycle_b.py", "from app.cycle_a import loop as ring\n")
    _write(
        tmp_path,
        "app/mod.py",
        f"from app.infra import tmpl\nfrag = {FRAGMENT}\n" + body,
    )
    with pytest.raises(TypeError) as exc:
        _discover(tmp_path)
    assert re.search(r"\.py:\d+", str(exc.value))


# Decorators are written and read outside the definition entirely: neither the
# body's names nor the type parameters of a PEP 695 definition are in scope for
# them, which is what the interpreter does with `@use(tmpl.bind(...))` above a
# `def go[tmpl]()`.
@pytest.mark.parametrize(
    "definition",
    [
        "def go():\n    tmpl = object()\n    return tmpl\n",
        "def go[tmpl]():\n    return tmpl\n",
        "class Reader:\n    tmpl = object()\n",
        "class Reader[tmpl]:\n    pass\n",
    ],
)
def test_a_decorator_resolves_outside_the_definition_it_decorates(
    tmp_path: Path, definition: str
):
    _write(
        tmp_path,
        "app/mod.py",
        (
            "def use(x): return lambda obj: obj\n"
            f"tmpl = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "@use(tmpl.bind(f=frag))\n"
        )
        + definition,
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].locations == ("app/mod.py:4",)


# `a = b = api_gql(...)` binds both names to that one statement, so a bind
# through either reads the same template -- and both spellings of the same
# combination are one binding with two call sites, not two bindings.
def test_a_chained_assignment_carries_the_statement_into_every_name(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            f"first = second = {TEMPLATE}\n"
            f"frag = {FRAGMENT}\n"
            "one = first.bind(f=frag)\n"
            "two = second.bind(f=[frag])\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].locations == ("app/mod.py:3", "app/mod.py:4")


# A class body does not hide its own names from itself -- only from the
# functions nested in it, which `test_a_method_does_not_see_the_class_body_name`
# pins from the other side. Grouping a template, its fragments and their bind as
# class attributes is an ordinary way to write them.
def test_a_class_body_resolves_the_names_it_binds_itself(tmp_path: Path):
    _write(
        tmp_path,
        "app/mod.py",
        (
            "class Queries:\n"
            f"    tmpl = {TEMPLATE}\n"
            f"    frag = {FRAGMENT}\n"
            "    bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].template.raw_text == "query Q { f @slot { __typename } }"


# The climb that lands, next to the one above the root that does not: two dots
# from `pkg.sub.mod` reach `pkg`, so `from ..frags import frag` is a name of the
# scanned tree and has to resolve like any other.
def test_a_two_level_relative_import_resolves(tmp_path: Path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/frags.py", f"frag = {FRAGMENT}\n")
    _write(tmp_path, "pkg/templates.py", f"tmpl = {TEMPLATE}\n")
    _write(tmp_path, "pkg/sub/__init__.py", "")
    _write(
        tmp_path,
        "pkg/sub/mod.py",
        (
            "from ..frags import frag\n"
            "from ..templates import tmpl\n"
            "bound = tmpl.bind(f=frag)\n"
        ),
    )
    package = _discover(tmp_path)
    assert len(package.binds) == 1
    assert package.binds[0].locations == ("pkg/sub/mod.py:3",)
    assert [s.raw_text for _, stmts in package.binds[0].slot_args for s in stmts] == [
        "fragment F on T { x }"
    ]


# Whether an unparsable file may own a bind is decided on the text alone, so the
# question it answers has to be one the text can answer: an AST `.bind(...)`
# spells the attribute `bind` literally, whatever the source puts around it.
# Matching the punctuation instead let a file that owns a bind pass for one that
# owns nothing -- and a scan that defers such a file regenerates the package
# without whatever it holds.
@pytest.mark.parametrize(
    "broken",
    [
        "tmpl .bind (f=frag)\ndef broken(\n",
        "tmpl. \\\n    bind(f=frag)\ndef broken(\n",
        "tmpl.\n# a comment between the dot and the name is legal too\nbind(f=frag)\n",
    ],
)
def test_a_bind_spelled_around_the_dot_still_aborts_the_scan(
    tmp_path: Path, broken: str
):
    _write(tmp_path, "app/mod.py", 'q = api_gql("query Q { f }")\n')
    _write(tmp_path, "app/legacy.py", broken)
    with pytest.raises(SyntaxError, match=r"Failed to parse .*legacy\.py"):
        _discover(tmp_path)


# The ignored binds reach a debug run, where "left alone on purpose" and "ours
# and lost" stop being the same empty `binds` list. Written from the generator
# rather than from the parser -- they are discovery's output, and the parser
# never reads them -- so this is where the artifact is pinned.
def test_ignored_binds_are_written_to_the_debug_directory(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="""
        type Query {
            ping: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql("query Ping { ping }")
        def listen(sock, host):
            sock.bind(host)
        """,
    )
    debug_dir = test_project.root / "debug_out"
    generate_gql_package(
        mode="async",
        schema_path=test_project.root / "schema.graphql",
        src_path=test_project.root,
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        debug_path=debug_dir,
    )
    assert json.loads((debug_dir / "ignored_binds.json").read_text("utf-8")) == [
        {
            "location": "sample_app/queries.py:4",
            "reason": (
                "cannot resolve 'sock' at sample_app/queries.py:4: an enclosing "
                "function or class body binds that name to something other than "
                "a single gql statement"
            ),
        }
    ]
