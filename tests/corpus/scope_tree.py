"""Scope trees composed at random, for `fuzz_scoping`.

`scoping.py` crosses axes: a block of shadow lines against a call, both in one
scope. That shape cannot spell a form whose meaning is a *pair* of places -- a
`global`/`nonlocal` written in one function and read in another -- and two of
its cases say so in a comment, having been written by hand for exactly that
reason. This composes instead: scopes nested freely, each interesting
statement placed at its own path in the tree, independently of the others.

Total by construction, because the oracle *runs* what comes out: the statement
inventory is fixed, nothing loops or recurses, and no generated module touches
anything outside its own names.

Three orderings are imposed on every scope, and each one pins an assumption
the scan is entitled to make (see `_ordered`). Without them the generator
spends its time on programs that raise `NameError` before reaching the call --
a question with no answer to compare, not a defect.
"""

from dataclasses import dataclass

from hypothesis import strategies as st

INDENT = "    "

TEMPLATE_TEXT = "query Q { f @slot { __typename } }"
SHADOW_TEMPLATE_TEXT = "query R { g @slot { __typename } }"
FRAGMENT_TEXT = "fragment F on T { x }"
TEMPLATE = f'api_gql("{TEMPLATE_TEXT}")'
SHADOW_TEMPLATE = f'api_gql("{SHADOW_TEMPLATE_TEXT}")'
FRAGMENT = f'api_gql("{FRAGMENT_TEXT}")'

# A literal tuple, not a bare fragment -- see `scoping.BIND_EXPR`.
BIND_EXPR = "tmpl.bind(f=(frag,))"

PREAMBLE: tuple[str, ...] = (
    "import contextlib",
    "def ctx(): return contextlib.nullcontext(object())",
    "def loaders(): return [object(), object()]",
    "def load(): return object()",
    "def deco(value): return lambda fn: fn",
    "def base_of(value): return object",
    "class Configurable:",
    "    def __init_subclass__(cls, value=None, **kwargs):",
    "        super().__init_subclass__(**kwargs)",
)

# Where the call is written. Everything but `statement` and the two generator
# forms puts it inside a definition's eager part, which runs before that
# definition binds its own name.
PLACEMENTS: tuple[str, ...] = (
    "statement",
    "default",
    "decorator",
    "class-base",
    "class-keyword",
    "lambda-default",
    "genexp",
    "genexp-deferred",
)

# One entry per way Python binds a name, which is also one entry per place the
# scan writes a name into a scope. Placed as an ordinary shadow, and reused as
# what a `global`/`nonlocal` declaration binds -- the crossing that found
# `def tmpl` under `global tmpl` writing the wrong scope.
BINDING_FORMS: tuple[tuple[str, ...], ...] = (
    (f"tmpl = {SHADOW_TEMPLATE}",),
    ("for tmpl in loaders():", f"{INDENT}pass"),
    ("with ctx() as tmpl:", f"{INDENT}pass"),
    ("try:", f"{INDENT}pass", "except ValueError as tmpl:", f"{INDENT}pass"),
    ("match load():", f"{INDENT}case object() as tmpl:", f"{INDENT * 2}pass"),
    ("tmpl, _other = loaders()",),
    ("tmpl: object",),
    ("def tmpl(): pass",),
    ("class tmpl: pass",),
    ("import tmpl",),
    ("from app.other import tmpl",),
    ("tmpls = [(tmpl := load()) for _ in loaders()]",),
)

type Path = tuple[int, ...]
type Lines = tuple[str, ...]
# (group, name, lines). The group decides where the element may sit in its
# scope; the name ties a call to the definition it calls.
type Element = tuple[str, str, list[str]]


@dataclass(frozen=True, kw_only=True)
class Scope:
    kind: str  # "function" | "class"
    name: str
    path: Path
    children: tuple["Scope", ...]


@dataclass(frozen=True, kw_only=True)
class Placed:
    # Everything the generator decided to put somewhere, as data: the renderer
    # stays dumb and `Module.plain` stays honest.
    template: Path
    template_kind: str
    bind: Path
    placement: str
    shadows: tuple[tuple[Path, Lines], ...] = ()
    # (declaring scope, `global`/`nonlocal`, the form bound under it). A
    # declaration that binds nothing is legal and means something else, which
    # is what `None` spells.
    declaration: tuple[Path, str, Lines | None] | None = None


@dataclass(frozen=True, kw_only=True)
class Module:
    source: str
    # Whether `tmpl` has exactly one binding in the whole program. On such a
    # module the scan is not merely *allowed* to answer -- it must, or a real
    # bind was lost. The other half of that judgement is the interpreter
    # actually reaching the call, which only the run can say.
    plain: bool
    summary: str


@st.composite
def _scopes(draw: st.DrawFn, depth: int, prefix: Path) -> tuple[Scope, ...]:
    built: list[Scope] = []
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        kind = draw(st.sampled_from(["function", "class"]))
        path = (*prefix, index)
        stem = "s" + "".join(str(step) for step in path)
        children = draw(_scopes(depth - 1, path)) if depth else ()
        built.append(
            Scope(
                kind=kind,
                name=stem.upper() if kind == "class" else stem,
                path=path,
                children=children,
            )
        )
    return tuple(built)


def _walk(scopes: tuple[Scope, ...]) -> list[Scope]:
    return [found for scope in scopes for found in (scope, *_walk(scope.children))]


def _scope_at(scopes: tuple[Scope, ...], path: Path) -> Scope:
    scope = scopes[path[0]]
    for step in path[1:]:
        scope = scope.children[step]
    return scope


def _readable_from(scopes: tuple[Scope, ...], bind_path: Path) -> list[Path]:
    # Where a template can sit and still be readable at `bind_path`: the call
    # site's own scope, or a function (or the module) enclosing it. A class
    # body is invisible from anything nested inside it, exactly as at runtime.
    found: list[Path] = [()]
    for length in range(1, len(bind_path) + 1):
        prefix = bind_path[:length]
        if prefix == bind_path or _scope_at(scopes, prefix).kind == "function":
            found.append(prefix)
    return found


def _bind_lines(placement: str, tag: str) -> list[str]:
    match placement:
        case "statement":
            return [f"bound{tag} = {BIND_EXPR}"]
        case "default":
            return [f"def u{tag}(value={BIND_EXPR}):", f"{INDENT}return value"]
        case "decorator":
            return [f"@deco({BIND_EXPR})", f"def u{tag}():", f"{INDENT}return None"]
        case "class-base":
            return [f"class U{tag}(base_of({BIND_EXPR})):", f"{INDENT}pass"]
        case "class-keyword":
            return [f"class U{tag}(Configurable, value={BIND_EXPR}):", f"{INDENT}pass"]
        case "lambda-default":
            return [f"u{tag} = lambda value={BIND_EXPR}: value"]
        case "genexp":
            return [f"bound{tag} = next({BIND_EXPR} for _ in range(1))"]
        case _:
            # Built here, iterated below: the call reads what the names hold
            # at iteration, not on the line it is written on.
            return [
                f"gen{tag} = ({BIND_EXPR} for _ in range(1))",
                f"bound{tag} = list(gen{tag})",
            ]


@st.composite
def modules(draw: st.DrawFn) -> Module:
    scopes = draw(_scopes(2, ()))
    paths: list[Path] = [(), *(scope.path for scope in _walk(scopes))]

    bind_path = draw(st.sampled_from(paths))
    # Biased towards a template the call can actually read: an unreadable one
    # raises NameError at import, and a case the interpreter never executes
    # teaches the oracle nothing. Unreadable placements stay in the draw,
    # because "the scan must not answer either" is also worth checking.
    readable = _readable_from(scopes, bind_path)
    template_path = draw(st.one_of(st.sampled_from(readable), st.sampled_from(paths)))
    shadows = draw(
        st.lists(
            st.tuples(st.sampled_from(paths), st.sampled_from(BINDING_FORMS)),
            max_size=2,
        )
    )
    function_paths = [scope.path for scope in _walk(scopes) if scope.kind == "function"]
    declaration: tuple[Path, str, Lines | None] | None = None
    if function_paths and draw(st.booleans()):
        decl_path = draw(st.sampled_from(function_paths))
        kinds = ["global"]
        if any(
            _scope_at(scopes, decl_path[:length]).kind == "function"
            for length in range(1, len(decl_path))
        ):
            kinds.append("nonlocal")
        declaration = (
            decl_path,
            draw(st.sampled_from(kinds)),
            draw(st.sampled_from([None, *BINDING_FORMS])),
        )

    placed = Placed(
        template=template_path,
        template_kind=draw(st.sampled_from(["assign", "import"])),
        bind=bind_path,
        placement=draw(st.sampled_from(PLACEMENTS)),
        shadows=tuple(shadows),
        declaration=declaration,
    )
    order = draw(
        st.lists(st.integers(min_value=0, max_value=32), min_size=32, max_size=32)
    )
    summary = (
        f"tree={_shape(scopes)} template@{placed.template}/{placed.template_kind}"
        f" bind@{placed.bind} placement={placed.placement}"
        f" shadows={[path for path, _ in placed.shadows]} decl={placed.declaration}"
    )
    return Module(
        source=_render(scopes, placed, order),
        plain=not placed.shadows and placed.declaration is None,
        summary=summary,
    )


def _shape(scopes: tuple[Scope, ...]) -> str:
    if not scopes:
        return ""
    return "(" + ",".join(f"{s.kind[0]}{_shape(s.children)}" for s in scopes) + ")"


def _render(scopes: tuple[Scope, ...], placed: Placed, order: list[int]) -> str:
    header = ["from gql_recorder import api_gql", *PREAMBLE, f"frag = {FRAGMENT}"]
    body = _render_body(scopes, (), placed, order, counter=[0])
    return "".join(line + "\n" for line in [*header, *body])


def _statements_at(path: Path, placed: Placed, counter: list[int]) -> list[Element]:
    found: list[Element] = []
    if placed.template == path:
        line = (
            f"tmpl = {TEMPLATE}"
            if placed.template_kind == "assign"
            else "from app.templates import tmpl"
        )
        found.append(("template", "", [line]))
    for shadow_path, lines in placed.shadows:
        if shadow_path == path:
            found.append(("stmt", "", list(lines)))
    if placed.declaration is not None and placed.declaration[0] == path:
        _, kind, form = placed.declaration
        # The declaration and the binding under it are separate elements: what
        # they mean depends on where each lands relative to the call, and one
        # block would fix that distance at zero.
        found.append(("decl", "", [f"{kind} tmpl"]))
        if form is not None:
            found.append(("stmt", "", list(form)))
    if placed.bind == path:
        counter[0] += 1
        found.append(("bind", "", _bind_lines(placed.placement, str(counter[0]))))
    return found


def _render_body(
    scopes: tuple[Scope, ...],
    path: Path,
    placed: Placed,
    order: list[int],
    *,
    counter: list[int],
) -> list[str]:
    elements: list[Element] = []
    for scope in scopes:
        inner = _render_body(
            scope.children, scope.path, placed, order, counter=counter
        ) or ["pass"]
        if scope.kind == "function":
            elements.extend([
                (
                    "def",
                    scope.name,
                    [f"def {scope.name}():", *[INDENT + line for line in inner]],
                ),
                ("call", scope.name, [f"{scope.name}()"]),
            ])
        else:
            # A class body runs where it is written, so it is grouped with the
            # calls: whatever it holds sees a scope that has finished running,
            # the same assumption a called function gets.
            # Nameless as an element: nothing calls a class body, so it has
            # no definition to wait for.
            elements.append((
                "call",
                "",
                [f"class {scope.name}:", *[INDENT + line for line in inner]],
            ))
    elements.extend(_statements_at(path, placed, counter))
    return [line for _, _, lines in _ordered(elements, order) for line in lines]


_GROUPS = ("template", "decl", "stmt", "def", "call", "bind")


def _ordered(elements: list[Element], order: list[int]) -> list[Element]:
    # Shuffled by the drawn keys, then grouped. Each boundary pins an
    # assumption the scan is entitled to make, and a generator free to break
    # it produces programs that raise rather than answer:
    #
    #   template/decl first -- a declaration must precede every use of its
    #     name in the scope (Python rejects the other order outright), and a
    #     template read before its line is a NameError, not a divergence;
    #   calls and class bodies after the statements -- `_positional_depth`
    #     reads a body as running once the scope defining it is complete;
    #   the call site last -- so a writer *can* already have run by the time
    #     the bind reads the name, which is the whole point of the tree.
    #
    # Within a group the shuffle stands, and a call still follows its own
    # definition.
    keyed = sorted(
        enumerate(elements), key=lambda item: (order[item[0] % len(order)], item[0])
    )
    grouped: dict[str, list[Element]] = {group: [] for group in _GROUPS}
    for _, element in keyed:
        grouped[element[0]].append(element)
    ordered = [element for group in _GROUPS for element in grouped[group]]

    defined: set[str] = set()
    waiting: dict[str, list[Element]] = {}
    emitted: list[Element] = []
    for element in ordered:
        group, name, _ = element
        if group == "call" and name and name not in defined:
            waiting.setdefault(name, []).append(element)
            continue
        emitted.append(element)
        if group == "def":
            defined.add(name)
            emitted.extend(waiting.pop(name, []))
    return [*emitted, *(item for pending in waiting.values() for item in pending)]
