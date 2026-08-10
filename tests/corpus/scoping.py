"""The scoping corpus: every case is one `.bind(...)` written somewhere.

Four axes, crossed rather than listed. Listing them by hand is what left the
order axis empty -- a name assigned *after* the call that reads it -- which is
where four separate scan-versus-Python divergences were found at once.

A case is emitted only if it compiles: `compile()` is the authority on which
crossings are legal Python (a walrus inside a comprehension in a class body is
not), so no grammar rule is restated here to keep the corpus honest.
"""

import itertools
from dataclasses import dataclass
from pathlib import Path

INDENT = "    "

TEMPLATE_TEXT = "query Q { f @slot { __typename } }"
# The shadowing assignment's template is a *different* statement on purpose:
# with one text for both, a case that resolves to the wrong one of the two
# reads as a pass, which is how "the scan sees the later assignment the call
# never reaches" stayed invisible.
SHADOW_TEMPLATE_TEXT = "query R { g @slot { __typename } }"
FRAGMENT_TEXT = "fragment F on T { x }"
TEMPLATE = f'api_gql("{TEMPLATE_TEXT}")'
SHADOW_TEMPLATE = f'api_gql("{SHADOW_TEMPLATE_TEXT}")'
FRAGMENT = f'api_gql("{FRAGMENT_TEXT}")'

BIND_EXPR = "tmpl.bind(f=frag)"
BIND_LINE = f"bound = {BIND_EXPR}"

# `ctx`/`loaders`/`load` stand in for whatever a real call site would use: the
# scan reads the binding form, and the interpreter has to actually run it.
PREAMBLE = [
    "import contextlib",
    "def ctx(): return contextlib.nullcontext(object())",
    "def loaders(): return [object(), object()]",
    "def load(): return object()",
    # Stand-ins for the eager parts of a definition: a decorator factory, a
    # base-class factory, and a base that takes a class keyword. A definition
    # runs all three *before* it binds its own name, which is the order the
    # `placement` corpus below is written to cross.
    "def deco(value): return lambda fn: fn",
    "def base_of(value): return object",
    "class Configurable:",
    "    def __init_subclass__(cls, value=None, **kwargs):",
    "        super().__init_subclass__(**kwargs)",
]

# Every way Python's grammar has of binding a name, written without leading
# indentation so the corpus can place each at whatever depth a case needs.
# Shared with `test_bind_discovery`, which asks its own questions of the same
# forms -- two copies of this list would drift the day the grammar grows one.
BINDER_FORMS: tuple[tuple[str, ...], ...] = (
    ("for tmpl in loaders():", "    pass"),
    ("with ctx() as tmpl:", "    pass"),
    ("try:", "    pass", "except ValueError as tmpl:", "    pass"),
    ("match load():", "    case object() as tmpl:", "        pass"),
    ("match load():", "    case [*tmpl]:", "        pass"),
    ("match load():", "    case {'k': 1, **tmpl}:", "        pass"),
    ("tmpls = [tmpl for tmpl in loaders()]",),
    ("tmpls = {tmpl for tmpl in loaders()}",),
    ("tmpls = {tmpl: 1 for tmpl in loaders()}",),
    ("tmpls = tuple(tmpl for tmpl in loaders())",),
    ("tmpls = [tmpl for _a in loaders() for tmpl in loaders()]",),
    ("tmpl, _other = loaders()",),
    ("*tmpl, _other = loaders()",),
    ("tmpl = loaders()", "del tmpl"),
    ("type tmpl = int",),
    ("from app.other import tmpl",),
    ("import tmpl",),
    # PEP 526: a bare annotation in a function body binds the name even though
    # it assigns nothing.
    ("tmpl: object",),
    # A definition binds its name like any other statement. Listed here so the
    # shadow axis crosses them; where in a definition the *call* can sit is a
    # separate question, crossed by `build_placement_corpus`.
    ("def tmpl(): pass",),
    ("class tmpl: pass",),
    # PEP 572: a walrus binds in the scope the comprehension is written in,
    # not in the comprehension's own.
    ("tmpls = [(tmpl := load()) for _ in loaders()]",),
    ("tmpls = {(tmpl := load()) for _ in loaders()}",),
    ("tmpls = tuple((tmpl := load()) for _ in loaders())",),
    ("tmpls = [_ for _ in loaders() if (tmpl := load())]",),
)


@dataclass(frozen=True, kw_only=True)
class Site:
    # Where the `.bind(...)` call itself is written, as the nesting that has
    # to be opened above it and whatever has to run for that body to execute.
    name: str
    prologue: tuple[tuple[int, str], ...] = ()
    body_level: int = 0
    epilogue: tuple[tuple[int, str], ...] = ()
    # How the runner reaches the body after import, as an attribute chain
    # rather than a snippet to evaluate: `("go",)` calls a module function,
    # `("Holder", "go")` instantiates and calls a method. Empty when importing
    # the module already runs the body.
    invoke: tuple[str, ...] = ()
    # Where an enclosing function can hold the template, for the `enclosing`
    # outer and the `nonlocal` shadow: the prologue index to write after, and
    # the depth to write at.
    enclosing_after: int | None = None
    enclosing_level: int | None = None
    # How the call itself is written. A generator expression defers its body to
    # the moment it is iterated, so the same call reads whatever the names hold
    # by then -- the one place where the line a binding sits on says nothing
    # about whether the call can read it.
    bind_line: str = BIND_LINE


SITES: tuple[Site, ...] = (
    Site(name="module"),
    Site(
        name="function",
        prologue=((0, "def go():"),),
        body_level=1,
        invoke=("go",),
    ),
    Site(
        name="method",
        prologue=((0, "class Holder:"), (1, "def go(self):")),
        body_level=2,
        invoke=("Holder", "go"),
    ),
    Site(name="classbody", prologue=((0, "class Holder:"),), body_level=1),
    Site(
        name="nestedfn",
        prologue=((0, "def outer():"), (1, "def go():")),
        body_level=2,
        epilogue=((1, "return go()"),),
        invoke=("outer",),
        enclosing_after=0,
        enclosing_level=1,
    ),
    # A generator expression around the call: its body runs on iteration, so a
    # name bound below it is what the call actually reads.
    Site(
        name="genexp",
        prologue=((0, "def go():"),),
        body_level=1,
        invoke=("go",),
        bind_line=f"bound = next({BIND_EXPR} for _ in range(1))",
    ),
    # Two functions deep with the middle one binding nothing: what `nonlocal`
    # has to step over to reach the function that really holds the name.
    Site(
        name="deepfn",
        prologue=((0, "def outer():"), (1, "def middle():"), (2, "def go():")),
        body_level=3,
        epilogue=((2, "return go()"), (1, "return middle()")),
        invoke=("outer",),
        enclosing_after=0,
        enclosing_level=1,
    ),
    # A method inside a function: the one shape where a class body stands
    # between a call and the enclosing function whose name it reads, which is
    # what `nonlocal` has to step over on its way out.
    Site(
        name="fnmethod",
        prologue=((0, "def outer():"), (1, "class Holder:"), (2, "def go(self):")),
        body_level=3,
        epilogue=((1, "return Holder().go()"),),
        invoke=("outer",),
        enclosing_after=0,
        enclosing_level=1,
    ),
)

# Where the template the call site is *meant* to read comes from.
OUTERS = ("none", "module", "import", "enclosing")

# What else binds `tmpl` in the call site's own scope. The named three plus
# every grammar form above; `none` is the case with nothing in the way.
NAMED_SHADOWS: dict[str, tuple[str, ...]] = {
    "none": (),
    "assign": (f"tmpl = {SHADOW_TEMPLATE}",),
    "global": ("global tmpl", f"tmpl = {SHADOW_TEMPLATE}"),
    "nonlocal": ("nonlocal tmpl", f"tmpl = {SHADOW_TEMPLATE}"),
}

# Whether the shadow is written above the call or below it. The axis that was
# missing: below the call, a shadow that binds locally makes the call
# unreachable in Python while the scan, which resolves after the whole walk,
# sees the binding all the same.
ORDERS = ("before", "after")


@dataclass(frozen=True, kw_only=True)
class ScopeCase:
    axes: tuple[tuple[str, str], ...]
    files: tuple[tuple[str, str], ...]
    module: str
    invoke: tuple[str, ...]
    # Whether this case is one the scan must actually *bind*. Refinement alone
    # allows a loud refusal for anything, which is right where the answer
    # genuinely depends on flow -- but for a case written to be ordinary,
    # valid code, refusing is losing the binding and the user gets a
    # `LookupError` on a line that is correct.
    must_bind: bool = False

    @property
    def id(self) -> str:
        return "-".join(value for _, value in self.axes)


def _shadow_blocks() -> dict[str, tuple[str, ...]]:
    blocks = dict(NAMED_SHADOWS)
    for index, form in enumerate(BINDER_FORMS):
        blocks[f"form{index:02d}"] = form
    return blocks


SHADOWS = _shadow_blocks()


def _indent(block: tuple[str, ...] | list[str], level: int) -> list[str]:
    return [INDENT * level + line if line else "" for line in block]


def _module_source(*, outer: str, site: Site, shadow: str, order: str) -> str:
    lines = [f"from {_RECORDER} import api_gql", *PREAMBLE]
    if outer == "module":
        lines.append(f"tmpl = {TEMPLATE}")
    elif outer == "import":
        lines.append("from app.templates import tmpl")
    lines.append(f"frag = {FRAGMENT}")

    for index, (level, text) in enumerate(site.prologue):
        lines.extend(_indent([text], level))
        if outer == "enclosing" and index == site.enclosing_after:
            # Internal invariant: `_valid` only pairs `enclosing` with a site
            # that declares where an enclosing holder goes.
            assert site.enclosing_level is not None  # noqa: S101
            lines.extend(_indent([f"tmpl = {TEMPLATE}"], site.enclosing_level))

    shadow_block = SHADOWS[shadow]
    body = (
        [*shadow_block, BIND_LINE] if order == "before" else [BIND_LINE, *shadow_block]
    )
    lines.extend(_indent(body, site.body_level))
    for level, text in site.epilogue:
        lines.extend(_indent([text], level))
    return "".join(line + "\n" for line in lines)


_RECORDER = "gql_recorder"

# Named by the form that needs them; written for every case, because a file
# nothing imports costs one `write_text` and keeps the tree one shape.
SUPPORT_FILES: tuple[tuple[str, str], ...] = (
    ("app/__init__.py", ""),
    ("app/other.py", "tmpl = object()\n"),
    (
        "app/templates.py",
        f"from {_RECORDER} import api_gql\ntmpl = {TEMPLATE}\n",
    ),
    ("tmpl.py", ""),
)


def _valid(*, outer: str, site: Site, shadow: str, order: str) -> bool:
    # Structural legality only -- whether the case can be *written*. Whether
    # it is legal Python is `compile()`'s answer, taken in `build_corpus`.
    if outer == "enclosing" and site.enclosing_after is None:
        return False
    if shadow == "none":
        # An empty shadow has nothing to order around the call: one case, not
        # two identical ones.
        return order == ORDERS[0]
    return True


def build_corpus() -> list[ScopeCase]:
    cases: list[ScopeCase] = []
    for outer, site, shadow, order in itertools.product(OUTERS, SITES, SHADOWS, ORDERS):
        if not _valid(outer=outer, site=site, shadow=shadow, order=order):
            continue
        source = _module_source(outer=outer, site=site, shadow=shadow, order=order)
        try:
            compile(source, "<corpus>", "exec")
        except SyntaxError:
            # Not a gap: the crossing is not writable Python, so neither the
            # scan nor the interpreter has an opinion to compare.
            continue
        cases.append(
            ScopeCase(
                axes=(
                    ("outer", outer),
                    ("site", site.name),
                    ("shadow", shadow),
                    ("order", order),
                ),
                files=(*SUPPORT_FILES, ("app/mod.py", source)),
                module="app.mod",
                invoke=site.invoke,
            )
        )
    return cases


def write_case(case: ScopeCase, root: Path) -> None:
    for relative, source in case.files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


# Two shapes the generated corpus cannot express, written out by hand and run
# through the same refinement oracle. The generator crosses a *block* of
# shadow lines against a call, and neither of these is a block: one needs the
# call to sit between a generator being built and being iterated, the other
# needs a `nonlocal` declaration above the call and its assignment below.
# Their axes belong in the crossing eventually; until the corpus can spell
# them, they are here rather than nowhere.
HANDWRITTEN: tuple[tuple[str, str, bool], ...] = (
    (
        "genexp-iterated-after-rebind",
        f"""def go():
    gen = ({BIND_EXPR} for _ in range(1))
    tmpl = {SHADOW_TEMPLATE}
    return list(gen)[0]
""",
        True,
    ),
    (
        "nonlocal-through-a-function-that-binds-nothing",
        f"""def outer():
    tmpl = {TEMPLATE}

    def middle():
        def inner():
            nonlocal tmpl
            bound = {BIND_EXPR}
            tmpl = {SHADOW_TEMPLATE}
            return bound

        return inner()

    return middle()
""",
        # Two bindings of the module-declared name reach one scope here, so a
        # refusal is the honest answer and the oracle only pins that it is not
        # a *wrong* answer.
        False,
    ),
)


def handwritten_cases() -> list[ScopeCase]:
    return [
        _authored_case(
            axes=(("handwritten", name),),
            body=body,
            invoke=("outer",) if "outer" in body else ("go",),
            must_bind=must_bind,
        )
        for name, body, must_bind in HANDWRITTEN
    ]


def _authored_case(
    *,
    axes: tuple[tuple[str, str], ...],
    body: str,
    invoke: tuple[str, ...],
    must_bind: bool,
) -> ScopeCase:
    # One case written as a body rather than crossed out of blocks: the header
    # every case shares, then whatever the body says. `compile` is the same
    # authority here as in `build_corpus` -- a case that is not Python is a
    # corpus defect, and it says so at import rather than at the run.
    header = "".join(
        line + "\n"
        for line in [
            f"from {_RECORDER} import api_gql",
            *PREAMBLE,
            f"frag = {FRAGMENT}",
        ]
    )
    source = header + body
    compile(source, "<corpus>", "exec")
    return ScopeCase(
        axes=axes,
        files=(*SUPPORT_FILES, ("app/mod.py", source)),
        module="app.mod",
        invoke=invoke,
        must_bind=must_bind,
    )


# `global` and `nonlocal` write into a scope that is not the writing one, so
# what they mean is a *pair* of places: where the declaration is written and
# where the name is read. The crossed corpus fixes the two as equal -- every
# shadow block sits in the call site's own scope -- and that is what left the
# family half-covered: the module half (`global`, answered through the module
# graph) was refused, while the enclosing-function half answered with the
# template the call no longer reads.
DECLARATIONS = ("global", "nonlocal")

# Whether the function that rebinds the name has run by the time the call site
# reads it. None of the three is answerable statically, so a refusal is the
# honest answer to all of them; the oracle pins only that the answer is not a
# *wrong* one.
WRITER_CALLS = ("before", "after", "never")

# Where the call sits relative to the scope holding the template: in it, in a
# function of its own beside the writer, or -- the other half of the same
# question -- at module level, reading a name of the same spelling that the
# declaration does not address at all. A `nonlocal` names an enclosing
# function, so the module's `tmpl` is not the one it writes, and a bind
# reading it must still be answered.
DECLARATION_READERS = ("holder", "beside", "module")

# How the writer binds the name under its declaration -- one form per place
# the scan writes a name into a scope. A declaration governs *every* binding
# form in its scope, not the assignment statement alone, and the scan asked
# the question at one of six such places: `def tmpl`, `import tmpl`,
# `except E as tmpl` and a match capture all wrote the declaring function's
# own scope while Python wrote the module's, so a call elsewhere read a
# template the program had already replaced. Found by a generated scope tree,
# kept here because a family needs a crossing, not one example.
DECLARED_FORMS: dict[str, tuple[str, ...]] = {
    "assign": (f"tmpl = {SHADOW_TEMPLATE}",),
    "def": ("def tmpl(): pass",),
    "class": ("class tmpl: pass",),
    "import": ("import tmpl",),
    "from-import": ("from app.other import tmpl",),
    "for": ("for tmpl in loaders():", f"{INDENT}pass"),
    "except": ("try:", f"{INDENT}pass", "except ValueError as tmpl:", f"{INDENT}pass"),
    "match": ("match load():", f"{INDENT}case object() as tmpl:", f"{INDENT * 2}pass"),
}


def _writer_call_lines(writer_call: str, *, reading: list[str]) -> list[str]:
    call = ["switch()"]
    match writer_call:
        case "before":
            return [*call, *reading]
        case "after":
            return [*reading, *call]
        case _:
            return reading


def _reading_lines(reader: str) -> list[str]:
    if reader == "holder":
        return [BIND_LINE]
    return ["def go():", f"{INDENT}return {BIND_EXPR}", "bound = go()"]


def _declaration_body(
    *, declaration: str, writer_call: str, reader: str, form: str
) -> str:
    if reader == "module":
        # The template the call reads is the module's, and the declaration
        # inside `outer` addresses `outer`'s name of the same spelling. Two
        # names, one spelling: the call has to be answered from the module.
        holder = [
            f"tmpl = {SHADOW_TEMPLATE}",
            "def switch():",
            f"{INDENT}nonlocal tmpl",
            *_indent(DECLARED_FORMS[form], 1),
            "switch()",
        ]
        lines = [
            f"tmpl = {TEMPLATE}",
            "def outer():",
            *_indent(holder, 1),
            "outer()",
            BIND_LINE,
        ]
        return "".join(line + "\n" for line in lines)
    reading = _reading_lines(reader)
    if declaration == "global":
        lines = [
            f"tmpl = {TEMPLATE}",
            "def switch():",
            f"{INDENT}global tmpl",
            *_indent(DECLARED_FORMS[form], 1),
            *_writer_call_lines(writer_call, reading=reading),
        ]
        return "".join(line + "\n" for line in lines)
    body = [
        f"tmpl = {TEMPLATE}",
        "def switch():",
        f"{INDENT}nonlocal tmpl",
        *_indent(DECLARED_FORMS[form], 1),
        *_writer_call_lines(writer_call, reading=reading),
        "return bound",
    ]
    lines = ["def outer():", *_indent(body, 1)]
    return "".join(line + "\n" for line in lines)


def build_declaration_corpus() -> list[ScopeCase]:
    cases: list[ScopeCase] = []
    for declaration, writer_call, reader, form in itertools.product(
        DECLARATIONS, WRITER_CALLS, DECLARATION_READERS, DECLARED_FORMS
    ):
        # `global` writes the module's name, so reading it from the module is
        # the `holder` case already crossed; only `nonlocal` puts a
        # declaration and the module's name of that spelling apart. The writer
        # axis says nothing here either -- the call reads a name no
        # declaration addresses, so whether the writer ran cannot matter, and
        # one case says that.
        skip_module = declaration == "global" or writer_call != WRITER_CALLS[0]
        if reader == "module" and skip_module:
            continue
        cases.append(
            _authored_case(
                axes=(
                    ("declaration", declaration),
                    ("writer", writer_call),
                    ("reader", reader),
                    ("form", form),
                ),
                body=_declaration_body(
                    declaration=declaration,
                    writer_call=writer_call,
                    reader=reader,
                    form=form,
                ),
                invoke=() if declaration == "global" else ("outer",),
                # Every other case turns on whether the writer has run, which
                # no static walk answers -- a refusal is honest there. Reading
                # a name no declaration addresses is ordinary code, and losing
                # that binding is losing a real bind.
                must_bind=reader == "module",
            )
        )
    return cases


# Where the call is *written*, as opposed to which scope it stands in. A
# definition evaluates its decorators, its defaults and its bases before it
# binds its own name, so a call written in any of them reads the name the
# definition is about to replace. A walk that records the name first reads
# that order backwards, and the crossed corpus cannot say so: it writes every
# call as a statement of its own.
PLACEMENTS: dict[str, tuple[str, ...]] = {
    "statement": (BIND_LINE,),
    "default": (f"def {{name}}(value={BIND_EXPR}):", f"{INDENT}return value"),
    "decorator": (f"@deco({BIND_EXPR})", "def {name}():", f"{INDENT}return None"),
    "class-base": (f"class {{name}}(base_of({BIND_EXPR})):", f"{INDENT}pass"),
    "class-keyword": (
        f"class {{name}}(Configurable, value={BIND_EXPR}):",
        f"{INDENT}pass",
    ),
    "lambda-default": ("{name} = lambda value=" + BIND_EXPR + ": value",),
}

# What the definition around the call is called: something of its own, or the
# very name the call reads. The second is the case Python answers by order --
# the call reads the template, and only then does the definition take the name
# -- and it is the one a walk gets wrong.
DEFINITION_NAMES = ("holder", "tmpl")


def build_placement_corpus() -> list[ScopeCase]:
    cases: list[ScopeCase] = []
    for placement, lines in PLACEMENTS.items():
        for name in DEFINITION_NAMES:
            if placement == "statement" and name == "tmpl":
                # A bare statement has no definition to name, so the second
                # value of the axis would spell the same case twice.
                continue
            # `replace` rather than `format`: every template text in this
            # corpus is GraphQL, and GraphQL is made of braces.
            body = "".join(
                line.replace("{name}", name) + "\n"
                for line in [f"tmpl = {TEMPLATE}", *lines]
            )
            cases.append(
                _authored_case(
                    axes=(("placement", placement), ("definition", name)),
                    body=body,
                    invoke=(),
                    # A definition named after something else is ordinary
                    # code: the call reads the template and nothing shadows
                    # it, so losing the binding is losing a real bind.
                    must_bind=name != "tmpl",
                )
            )
    return cases
