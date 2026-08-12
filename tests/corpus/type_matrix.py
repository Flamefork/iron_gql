"""The type system crossed with the positions a type can occupy.

Every fixture that writes its own minimal schema covers the cells its feature
needed and leaves the rest of the table empty -- which is how "an enum used
only by a bound fragment's variable" stayed a blank cell while both of its
axes were covered several times over.

So the axes are crossed here instead: each kind of GraphQL type appears in
each position a type can be written in, under each of the canonical wrappers,
in one schema and one generated package.
"""

from dataclasses import dataclass


# (name in the schema, a literal of that type, the value a caller passes, the
# Python type the generator must produce for the non-null bare form).
@dataclass(frozen=True, kw_only=True)
class Kind:
    name: str
    literal: str
    value: object


KINDS: tuple[Kind, ...] = (
    Kind(name="String", literal='"s"', value="s"),
    Kind(name="Int", literal="1", value=1),
    Kind(name="ID", literal='"7"', value="7"),
    Kind(name="Size", literal="SMALL", value="SMALL"),
    Kind(name="Filter", literal='{term: "t"}', value={"term": "t"}),
)

# The four shapes every position has to survive: bare, non-null, a non-null
# list of non-nulls, and a nullable list of nullables. Named by what they do
# to a Python annotation, since that is what the generated module is judged on.
WRAPS: tuple[tuple[str, str], ...] = (
    ("plain", "{name}"),
    ("required", "{name}!"),
    ("list", "[{name}!]!"),
    ("sparse", "[{name}]"),
)


@dataclass(frozen=True, kw_only=True)
class Cell:
    # A cell is named twice over: as the schema spells it and as the generated
    # module does. The two have to differ -- the package's `to_camel` alias
    # generator is what carries a field between them -- so a corpus that used
    # one name for both would be testing a package configured unlike any real
    # one.
    name: str
    python: str
    kind: Kind
    typ: str


def _cells() -> list[Cell]:
    return [
        Cell(
            name=f"{kind.name.lower()}{wrap_name.capitalize()}",
            python=f"{kind.name.lower()}_{wrap_name}",
            kind=kind,
            typ=wrap.format(name=kind.name),
        )
        for kind in KINDS
        for wrap_name, wrap in WRAPS
    ]


CELLS = _cells()

# The one position a scalar cannot occupy (a fragment's own variable must be
# usable as a field argument) is no exception here: every cell is an argument
# of `echo`, so every cell is reachable as an operation variable, as an input
# object's field, and as a bound fragment's variable.
SCHEMA = """
enum Size {{
    SMALL
    LARGE
}}

input Filter {{
    term: String
}}

input Payload {{
{payload_fields}
}}

type Echo {{
    seen: String!
    size: Size
    sizes: [Size!]!
    tagged(size: Size!, term: String): String!
}}

type Query {{
    echo(payload: Payload!{echo_args}): Echo
    labelled(size: Size! = SMALL, term: String = "d"): Echo
}}
"""


def _argument_type(typ: str) -> str:
    # `echo` takes every cell, and a second query selects `echo` without them,
    # so no argument may be required. The variable a call passes keeps its own
    # wrapper -- a `String!` variable satisfies a `String` argument -- so the
    # cell's shape is still what travels.
    return typ.removesuffix("!")


def schema() -> str:
    payload_fields = "\n".join(f"    {c.name}: {c.typ}" for c in CELLS)
    echo_args = "".join(f", {c.name}: {_argument_type(c.typ)}" for c in CELLS)
    return SCHEMA.format(payload_fields=payload_fields, echo_args=echo_args)


def _variable_decls() -> str:
    return ", ".join(f"${c.name}: {c.typ}" for c in CELLS)


def _arguments() -> str:
    return "".join(f", {c.name}: ${c.name}" for c in CELLS)


def _payload_literal() -> str:
    return ", ".join(f"{c.name}: ${c.name}" for c in CELLS)


def queries(package: str) -> str:
    # Three positions in one file: `EveryCell` writes each cell as an operation
    # variable *and* threads the same variables through an input object's
    # fields; `SizeParts` reads a cell through a fragment factory's own
    # variable, which is the position no other fixture reaches. `SizeParts`
    # has no `bind()` of its own (it is a factory: its own closure uses a
    # variable) -- applying it and binding the result is left to the test,
    # which is where the value is actually known.
    return f'''
    from tests.generated.{package}.gql.api import api_gql

    every_cell = api_gql(
        """
        query EveryCell({_variable_decls()}) {{
            echo(payload: {{{_payload_literal()}}}{_arguments()}) {{
                seen
                size
                sizes
            }}
        }}
        """
    )

    defaults = api_gql(
        """
        query Defaults {{
            labelled {{ seen size }}
        }}
        """
    )

    slotted = api_gql(
        """
        query Slotted($payload: Payload!) {{
            echo(payload: $payload) @slot {{ __typename }}
        }}
        """
    )

    size_parts = api_gql(
        """
        fragment SizeParts on Echo {{
            seen
            tagged(size: $frag_size, term: $frag_term)
        }}
        """
    )
    '''


def sample(cell: Cell) -> object:
    # One value per cell, shaped by the cell's own wrapper. `sparse` carries a
    # null element on purpose: a nullable list of nullables is the one wrapper
    # where a null *inside* the list is legal, and the generated model has to
    # accept it coming back.
    if cell.typ.startswith("["):
        return [cell.kind.value, None] if cell.typ.endswith("]") else [cell.kind.value]
    return cell.kind.value


def call_arguments() -> dict[str, object]:
    # Keyed the way a caller writes them: the generated `execute` takes the
    # module's own snake-case names.
    return {cell.python: sample(cell) for cell in CELLS}


def wire_values() -> dict[str, object]:
    # The same values keyed the way the server sees them, which is the schema's
    # spelling. That the two dictionaries carry equal values under different
    # keys is the round trip.
    return {cell.name: sample(cell) for cell in CELLS}
