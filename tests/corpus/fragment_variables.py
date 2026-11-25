"""One fragment variable at every mix of positions it may fill.

Each position of the variable has three axes: whether its type is non-null,
whether the schema gives it a default, and whether it is an argument or an
input field. An id spells the positions in document order, `+` between them:
`Int!=` is a non-null one with a default, `in:` marks an input field.

The rules the expectations follow: the keyword of `with_args` can be left out
when every position has a schema default, and the operation then declares the
variable without the outermost non-null; a value is of the type every position
accepts, so one non-null position is enough to refuse `None`.
"""

from dataclasses import dataclass
from types import UnionType
from typing import Literal


@dataclass(frozen=True, kw_only=True)
class Position:
    typ: str
    default: str | None = None
    place: Literal["argument", "input-field"] = "argument"

    @property
    def id(self) -> str:
        prefix = "in:" if self.place == "input-field" else ""
        return f"{prefix}{self.typ}{'=' if self.default else ''}"


@dataclass(frozen=True, kw_only=True)
class FragmentVariable:
    positions: tuple[Position, ...]

    @property
    def id(self) -> str:
        return "+".join(position.id for position in self.positions)

    @property
    def schema(self) -> str:
        fields = [
            f"p{index}(value: {position.typ}{_default(position)}): String!"
            if position.place == "argument"
            else f"p{index}(by: By{index}!): String!"
            for index, position in enumerate(self.positions)
        ]
        inputs = [
            f"input By{index} {{ value: {position.typ}{_default(position)} }}"
            for index, position in enumerate(self.positions)
            if position.place == "input-field"
        ]
        return "\n".join([
            "type Query { item: Item }",
            f"type Item {{ id: ID! {' '.join(fields)} }}",
            *inputs,
        ])

    @property
    def fragment(self) -> str:
        usages = [
            f"p{index}(value: $size)"
            if position.place == "argument"
            else f"p{index}(by: {{value: $size}})"
            for index, position in enumerate(self.positions)
        ]
        return f"fragment Parts on Item {{ {' '.join(usages)} }}"


@dataclass(frozen=True, kw_only=True)
class AcceptedVariable(FragmentVariable):
    # The type the operation declares the variable with.
    declared: str
    # The type `with_args` takes the value as, `Omitted` aside.
    value: type | UnionType
    omittable: bool


def _default(position: Position) -> str:
    return "" if position.default is None else f" = {position.default}"


_REQUIRED = Position(typ="Int!")
_NULLABLE = Position(typ="Int")
_REQUIRED_DEFAULT = Position(typ="Int!", default="5")
_NULLABLE_DEFAULT = Position(typ="Int", default="64")
_REQUIRED_DEFAULT_FIELD = Position(typ="Int!", default="7", place="input-field")
_REQUIRED_FIELD = Position(typ="Int!", place="input-field")

ACCEPTED: tuple[AcceptedVariable, ...] = (
    AcceptedVariable(
        positions=(_REQUIRED,), declared="Int!", value=int, omittable=False
    ),
    AcceptedVariable(
        positions=(_NULLABLE,), declared="Int", value=int | None, omittable=False
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT,), declared="Int", value=int, omittable=True
    ),
    AcceptedVariable(
        positions=(_NULLABLE_DEFAULT,),
        declared="Int",
        value=int | None,
        omittable=True,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT_FIELD,),
        declared="Int",
        value=int,
        omittable=True,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT, _REQUIRED_DEFAULT_FIELD),
        declared="Int",
        value=int,
        omittable=True,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT, _NULLABLE_DEFAULT),
        declared="Int",
        value=int,
        omittable=True,
    ),
    AcceptedVariable(
        positions=(_NULLABLE_DEFAULT, _REQUIRED_DEFAULT),
        declared="Int",
        value=int,
        omittable=True,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT, _REQUIRED),
        declared="Int!",
        value=int,
        omittable=False,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT, _NULLABLE),
        declared="Int!",
        value=int,
        omittable=False,
    ),
    AcceptedVariable(
        positions=(_REQUIRED_DEFAULT, _REQUIRED_FIELD),
        declared="Int!",
        value=int,
        omittable=False,
    ),
    AcceptedVariable(
        positions=(_NULLABLE_DEFAULT, _REQUIRED),
        declared="Int!",
        value=int,
        omittable=False,
    ),
    AcceptedVariable(
        positions=(_NULLABLE_DEFAULT, _NULLABLE),
        declared="Int",
        value=int | None,
        omittable=False,
    ),
    AcceptedVariable(
        positions=(Position(typ="[Int]!", default="[1]"),),
        declared="[Int]",
        value=list[int | None],
        omittable=True,
    ),
    AcceptedVariable(
        positions=(
            Position(typ="[Int]!", default="[1]"),
            Position(typ="[Int!]", default="[2]"),
        ),
        declared="[Int!]",
        value=list[int],
        omittable=True,
    ),
    AcceptedVariable(
        positions=(Position(typ="[Int]!", default="[1]"), Position(typ="[Int!]")),
        declared="[Int!]!",
        value=list[int],
        omittable=False,
    ),
)

# Two positions no declaration fits. The diagnosis names the types of the
# schema, the non-null included, whether or not a position has a default.
CONFLICTING: tuple[FragmentVariable, ...] = (
    FragmentVariable(positions=(_REQUIRED, Position(typ="String!"))),
    FragmentVariable(
        positions=(_REQUIRED_DEFAULT, Position(typ="String!", default='"a"'))
    ),
    FragmentVariable(
        positions=(_NULLABLE_DEFAULT, Position(typ="[Int]!", default="[1]"))
    ),
    FragmentVariable(
        positions=(_REQUIRED_DEFAULT_FIELD, Position(typ="String", default='"a"'))
    ),
)
