import dataclasses
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass

from iron_gql.codegen.util import capitalize_first

type StrTransform = Callable[[str], str]


# A ValueError so call sites that predate the aggregated error keep working;
# every diagnosed rejection of the GraphQL input flows through this one type
# (malformed api_gql call sites are rejected earlier, as TypeError, before any
# GraphQL is read).
class GraphQLGenerationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


BUILTIN_SCALARS = {
    "String": "str",
    "Int": "int",
    "Float": "float",
    "Boolean": "bool",
    "Date": "datetime.date",
    "DateTime": "datetime.datetime",
    "JSON": "object",
    "Upload": "runtime.FileVar",
}


@dataclass(kw_only=True, frozen=True)
class ImportRef:
    module: str
    symbol: str

    @classmethod
    def parse(cls, raw: str) -> "ImportRef":
        module, symbol = raw.split(":", maxsplit=1)
        return cls(module=module, symbol=symbol)

    @property
    def dotted_path(self) -> str:
        return f"{self.module}.{self.symbol}"

    @property
    def root_symbol(self) -> str:
        # The name `from {module} import ...` binds.
        return self.symbol.split(".", maxsplit=1)[0]

    @property
    def root_module(self) -> str:
        # The name a plain `import {module}` binds.
        return self.module.split(".", maxsplit=1)[0]

    def import_statement(self) -> str:
        return f"from {self.module} import {self.root_symbol}"


def field_name_to_pascal(name: str) -> str:
    return "".join(
        capitalize_first(part) for part in name.strip("_").split("_") if part
    )


@dataclass(kw_only=True, frozen=True)
class ScalarRef:
    expr: str
    name_hint: str | None = None
    nullable: bool = False


@dataclass(kw_only=True, frozen=True)
class NamedRef:
    name: str
    nullable: bool = False


@dataclass(kw_only=True, frozen=True)
class ListRef:
    element: "TypeRef"
    nullable: bool = False


type TypeRef = ScalarRef | NamedRef | ListRef


def render_type_expr(typ: TypeRef) -> str:
    match typ:
        case ScalarRef(expr=expr):
            body = expr
        case NamedRef(name=name):
            body = name
        case ListRef(element=element):
            body = f"list[{render_type_expr(element)}]"
    if typ.nullable:
        body += " | None"
    return body


def make_optional(typ: TypeRef) -> TypeRef:
    if typ.nullable:
        return typ
    return dataclasses.replace(typ, nullable=True)


def rename_type(typ: TypeRef, rename: dict[str, str]) -> TypeRef:
    match typ:
        case NamedRef(name=name) if name in rename:
            return dataclasses.replace(typ, name=rename[name])
        case ListRef(element=element):
            return dataclasses.replace(typ, element=rename_type(element, rename))
        case _:
            return typ


def referenced_names(typ: TypeRef) -> Iterator[str]:
    match typ:
        case NamedRef(name=name):
            yield name
        case ListRef(element=element):
            yield from referenced_names(element)
        case ScalarRef():
            return


@dataclass(kw_only=True, frozen=True)
class CollectedField:
    name: str
    # The key this field arrives under in the raw JSON response: the alias when
    # one is given, the GraphQL field name otherwise.
    response_key: str
    type_info: TypeRef
    default_expr: str | None = None
    is_conditional: bool = False

    @property
    def alias(self) -> str | None:
        # Derived from the response key, not stored: only a `__`-prefixed
        # introspection key needs an explicit pydantic alias — its python
        # name moves the prefix to a suffix, out of the alias generator's
        # reach.
        if self.response_key.startswith("__"):
            return self.response_key
        return None

    @property
    def rendered_type(self) -> TypeRef:
        if self.is_conditional:
            return make_optional(self.type_info)
        return self.type_info

    def renamed(self, rename: dict[str, str]) -> "CollectedField":
        return dataclasses.replace(self, type_info=rename_type(self.type_info, rename))


@dataclass(kw_only=True, frozen=True)
class CollectedModel:
    name: str
    fields: list[CollectedField]
    graphql_type_name: str | None = None
    slot_name: str | None = None
    # The runtime typenames this model's selection covers: one for a concrete
    # object, several for a uniform composite or a polymorphic fallback group.
    # Consumed by the handles' covered-typename snapshots; empty for models
    # those walks never reach (operation roots).
    covered_typenames: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = dataclasses.field(init=False, compare=False)

    def __post_init__(self) -> None:
        deps = sorted({
            name
            for model_field in self.fields
            for name in referenced_names(model_field.type_info)
            if name != self.name
        })
        object.__setattr__(self, "dependencies", tuple(deps))

    @property
    def shape_key(self) -> tuple[CollectedField, ...]:
        return tuple(sorted(self.fields, key=lambda field: field.name))

    @property
    def field_names_key(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.shape_key)

    def renamed(self, rename: dict[str, str]) -> "CollectedModel":
        return dataclasses.replace(
            self,
            name=rename.get(self.name, self.name),
            fields=[field.renamed(rename) for field in self.fields],
        )


@dataclass(kw_only=True, frozen=True)
class CollectedUnionAlias:
    name: str
    variants: tuple[str, ...]
    discriminator: str | None = None

    @property
    def type_expr(self) -> str:
        union_expr = " | ".join(self.variants)
        if self.discriminator is None:
            return union_expr
        return (
            f"Annotated[{union_expr}, "
            f'pydantic.Field(discriminator="{self.discriminator}")]'
        )

    def renamed(self, rename: dict[str, str]) -> "CollectedUnionAlias":
        return dataclasses.replace(
            self,
            name=rename.get(self.name, self.name),
            variants=tuple(rename.get(name, name) for name in self.variants),
        )

    @property
    def dependencies(self) -> tuple[str, ...]:
        return self.variants


@dataclass(kw_only=True, frozen=True)
class CollectedEnum:
    name: str
    values: tuple[str, ...]


type CollectedArtifact = CollectedModel | CollectedUnionAlias


def slot_roots(
    artifacts: Iterable[CollectedArtifact],
) -> Iterator[tuple[CollectedModel, str]]:
    # The "slot subtree root" predicate, stated once, paired with the
    # non-None slot name the model type cannot encode.
    for artifact in artifacts:
        if isinstance(artifact, CollectedModel) and artifact.slot_name is not None:
            yield artifact, artifact.slot_name


@dataclass(kw_only=True, frozen=True)
class CollectedOperationVar:
    gql_name: str
    python_name: str
    type_info: TypeRef
    default_expr: str | None = None

    @property
    def signature_part(self) -> str:
        type_expr = render_type_expr(self.type_info)
        if self.default_expr is None:
            return f"{self.python_name}: {type_expr}"
        return f"{self.python_name}: {type_expr} = {self.default_expr}"

    @property
    def variable_entry(self) -> str:
        return f'"{self.gql_name}": {self.python_name}'


@dataclass(kw_only=True, frozen=True)
class CollectedSlot:
    name: str
    python_name: str
    base_name: str

    @property
    def signature_part(self) -> str:
        # Erased to the widest model: the kwarg constrains which fragments the
        # slot accepts, and the handle's own model parameter is what `read`
        # gives back to its owner.
        handle = f"{self.base_name}[pydantic.BaseModel]"
        return f"{self.python_name}: {handle} | Sequence[{handle}]"

    @property
    def fragments_entry(self) -> str:
        return f'"{self.name}": slots.as_handles({self.python_name})'


@dataclass(kw_only=True, frozen=True)
class CollectedOperation:
    # Every distinct literal spelling the operation was discovered under:
    # deduplication compares dedented text, but the dispatch dict is keyed by
    # the exact literal, so each spelling needs its own entry.
    stmt_texts: tuple[str, ...]
    class_name: str
    result_type: str
    # The printed exec source pre-split at each @slot occurrence: the text up
    # to the first gap, then one (slot response key, following text) pair per
    # gap — a slotless operation is the head alone. See
    # `codegen/slots.build_exec_parts`.
    exec_head: str
    exec_splices: tuple[tuple[str, str], ...]
    variables: tuple[CollectedOperationVar, ...]
    slots: tuple[CollectedSlot, ...]
    is_subscription: bool
    locations: tuple[str, ...]

    @property
    def signature_parts(self) -> tuple[str, ...]:
        parts = [var.signature_part for var in self.variables]
        parts.extend(slot.signature_part for slot in self.slots)
        if not parts:
            return ("self",)
        return ("self", "*", *parts)

    @property
    def variables_expr(self) -> str:
        return "{" + ", ".join(var.variable_entry for var in self.variables) + "}"

    @property
    def slot_fragments_expr(self) -> str:
        return "{" + ", ".join(slot.fragments_entry for slot in self.slots) + "}"

    @property
    def client_method(self) -> str:
        if self.is_subscription:
            return "subscribe"
        return "query"


@dataclass(kw_only=True, frozen=True)
class CollectedFragment:
    # Same contract as CollectedOperation.stmt_texts: one dispatch entry per
    # distinct literal spelling.
    stmt_texts: tuple[str, ...]
    location: str
    class_name: str
    singleton_name: str
    fragment_name: str
    model_name: str
    definition_text: str
    base_names: tuple[str, ...]
    # The handle's accepted-typename snapshot: `read` answers None for a
    # payload of any type outside it. Attached after name validation, because
    # the walk resolves NamedRefs through the collected names.
    covered_typenames: frozenset[str] = frozenset()


@dataclass(kw_only=True, frozen=True)
class CollectedPackageIR:
    result_artifacts: list[CollectedArtifact]
    input_artifacts: list[CollectedArtifact]
    operations: list[CollectedOperation]
    fragments: list[CollectedFragment]
    # The slot-field types of the package, stored once: the compatibility
    # bases (`{Type}Fragment`) are derived from these wherever needed — a slot
    # kwarg is typed by its field type's base, and a handle inherits every
    # base it is spread-compatible with.
    slot_types: tuple[str, ...]
    enums: list[CollectedEnum]
    # Models validating inside a slot or fragment subtree: rendered on the
    # open (extra="ignore") base, because their payloads carry other readers'
    # fields next to their own, and excluded from the rename pass so an open
    # model can never converge with a strict one.
    open_model_names: frozenset[str]
