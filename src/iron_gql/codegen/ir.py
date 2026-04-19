import dataclasses
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass

from iron_gql.codegen.util import capitalize_first

type StrTransform = Callable[[str], str]


class GraphQLGenerationError(Exception):
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

    def import_statement(self) -> str:
        root_symbol = self.symbol.split(".", maxsplit=1)[0]
        return f"from {self.module} import {root_symbol}"


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
    type_info: TypeRef
    alias: str | None = None
    default_expr: str | None = None
    is_conditional: bool = False

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
class CollectedOperation:
    stmt_text: str
    class_name: str
    result_type: str
    exec_source: str
    variables: tuple[CollectedOperationVar, ...]
    is_subscription: bool
    locations: tuple[str, ...]

    @property
    def signature_parts(self) -> tuple[str, ...]:
        if not self.variables:
            return ("self",)
        return ("self", "*", *(var.signature_part for var in self.variables))

    @property
    def variables_expr(self) -> str:
        return "{" + ", ".join(var.variable_entry for var in self.variables) + "}"

    @property
    def client_method(self) -> str:
        if self.is_subscription:
            return "subscribe"
        return "query"


@dataclass(kw_only=True, frozen=True)
class CollectedPackageIR:
    result_artifacts: list[CollectedArtifact]
    input_artifacts: list[CollectedArtifact]
    operations: list[CollectedOperation]
    enums: list[CollectedEnum]
