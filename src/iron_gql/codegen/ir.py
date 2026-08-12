import dataclasses
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass

from iron_gql.codegen.util import capitalize_first
from iron_gql.slots import CombinationKey

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
    # The type arguments this reference passes, when the artifact it names is
    # generic: the slot phantoms threaded down the path to a slot node (see
    # `collect.parametrize_slot_paths`). Empty for every other reference, which
    # is most of them.
    params: tuple[str, ...] = ()
    nullable: bool = False


@dataclass(kw_only=True, frozen=True)
class ListRef:
    element: "TypeRef"
    nullable: bool = False


type TypeRef = ScalarRef | NamedRef | ListRef


def render_type_expr(typ: TypeRef) -> str:
    # A reference carries type arguments exactly on the path to a slot node:
    # the phantom saying which fragments are readable there is a parameter of
    # every model on the way, and each binding fills it in when it names its
    # own result type. Every other reference is written bare.
    match typ:
        case ScalarRef(expr=expr):
            body = expr
        case NamedRef(name=name, params=params):
            body = f"{name}[{', '.join(params)}]" if params else name
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
    # The slot phantoms this model is generic over: one per slot reachable from
    # it, in template order, threaded down to the node models where each one
    # lands in `GQLSlotModel[...]` (see `collect.parametrize_slot_paths`). Empty
    # for every model off a slot path, which is most of them.
    type_params: tuple[str, ...] = ()
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
    # References rather than bare names: a variant on the path to a slot node
    # is generic, and one beside it -- a sibling branch of the same union that
    # holds no slot -- is not, so each variant carries its own arguments.
    variants: tuple[NamedRef, ...]
    discriminator: str | None = None
    # The same contract as CollectedModel.type_params: the slot phantoms this
    # alias is generic over, which are exactly the ones its variants pass on.
    type_params: tuple[str, ...] = ()

    @property
    def variant_names(self) -> tuple[str, ...]:
        return tuple(variant.name for variant in self.variants)

    @property
    def type_expr(self) -> str:
        union_expr = " | ".join(render_type_expr(variant) for variant in self.variants)
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
            variants=tuple(
                dataclasses.replace(
                    variant, name=rename.get(variant.name, variant.name)
                )
                for variant in self.variants
            ),
        )

    @property
    def dependencies(self) -> tuple[str, ...]:
        return self.variant_names


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
class CollectedRequiredFragmentArg:
    gql_name: str
    python_name: str
    explicit_value_type: TypeRef


@dataclass(kw_only=True, frozen=True)
class CollectedOmittableFragmentArg:
    gql_name: str
    python_name: str
    explicit_value_type: TypeRef


type CollectedFragmentArg = CollectedRequiredFragmentArg | CollectedOmittableFragmentArg


def slot_param_name(slot_python_name: str) -> str:
    # The one statement of the rule, read by `collect.parametrize_slot_paths`
    # for the models it parametrises and by the renderer for the TypeVar it
    # declares and the scaffold reserves. Named after the slot's `bind()`
    # keyword, so two templates with a slot of the same name share one
    # parameter -- a TypeVar is a variable, and one is enough.
    return f"TSlot{field_name_to_pascal(slot_python_name)}"


def result_model_name(class_name: str) -> str:
    # The one statement of the rule. The model collector names the artifact
    # with it, both IR kinds report it back under `result_type`, and `execute`
    # renders that into `client.query(...)`; a second spelling of it would
    # surface as a NameError in the generated module, not as a generation
    # error.
    return f"{class_name}Result"


def on_type_base_name(graphql_type_name: str) -> str:
    # The one statement of the rule, mirrored by `collect` (which decides
    # which types get a base at all) and `render` (which writes the class).
    # A second spelling of it would surface as a NameError in the generated
    # module -- the fragment's own class references this name in its base
    # expression -- rather than as a generation error.
    return f"On{capitalize_first(graphql_type_name)}"


def applied_fragment_class_name(fragment_class_name: str) -> str:
    # Каноническое правило имени читает `collect`, а `render` только использует
    # готовое имя. Маркер `0` не даёт имени начаться с dunder: иначе ссылка из
    # класса factory подверглась бы Python name mangling.
    if fragment_class_name.startswith("_"):
        return f"_0{fragment_class_name}Applied"
    return f"_{fragment_class_name}Applied"


@dataclass(kw_only=True, frozen=True)
class CollectedOperation:
    # Every distinct literal spelling the operation was discovered under:
    # deduplication compares dedented text, but the dispatch dict is keyed by
    # the exact literal, so each spelling needs its own entry.
    stmt_texts: tuple[str, ...]
    class_name: str
    # The printed operation text: static, sent as-is by `execute` — a plain
    # operation never has a slot to expand at bind time (see
    # `collect.collect_package_ir`: one with `@slot` is a template instead).
    exec_source: str
    variables: tuple[CollectedOperationVar, ...]
    is_subscription: bool
    # Every call site that discovered this operation, in discovery order.
    locations: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)

    @property
    def result_type(self) -> str:
        return result_model_name(self.class_name)


@dataclass(kw_only=True, frozen=True)
class CollectedTemplateSlot:
    # A slot carries two names, and which one a layer uses is not a detail:
    # `python_name` is the whole public side (the `bind()` keyword, the type
    # parameter, the dispatch key), while `name` is the wire side (the JSON
    # response key, the point `_SlotFiller` splices at, the key of the
    # runtime's fragments context). `expand_binding` is the one place that
    # translates between them.
    name: str  # response key
    python_name: str  # snake_case kwarg / dispatch key
    type_name: str  # the slot field's GraphQL type; what a fragment spreads into
    # The slot's node models, in walk order -- every model whose `slot_name`
    # is this slot's, which is where that fact is actually recorded; this is a
    # re-keying of it, not a second copy. One key routinely reaches several
    # models: a polymorphic slot type gives it one per variant (the union
    # alias over them is not a node model and is reached from them through the
    # dependency graph), and the key may also be selected under two parents.
    # All of them carry the same spliced fragments, so every one of them is
    # readable through those fragments' own `read(node)`.
    node_types: tuple[str, ...]
    # On-type bases, от которых может наследоваться фрагмент для этого slot:
    # по одному на каждое совместимое с `type_name` условие типа среди
    # фрагментов пакета, в отсортированном порядке. Это полный набор того, что
    # `bind()` принимает для slot: base группирует все фрагменты своего типа,
    # поэтому сигнатура короче перечисления, но принимает те же definitions и
    # applications.
    on_type_bases: tuple[str, ...]


@dataclass(kw_only=True, frozen=True)
class CollectedTemplate:
    # Same contract as CollectedOperation.stmt_texts: one dispatch entry per
    # distinct literal spelling.
    stmt_texts: tuple[str, ...]
    # The GraphQL operation name, which `_dedup_statements` has already made
    # injective -- unlike `class_name`, which two operation names differing
    # only in the first letter's case collapse onto. Carried because a
    # combination has to name the template it belongs to, and naming it by
    # `class_name` answered one template's slots with the other's.
    name: str
    class_name: str  # capitalised operation name, e.g. "GetAttachment"
    variables: tuple[CollectedOperationVar, ...]
    slots: tuple[CollectedTemplateSlot, ...]
    is_subscription: bool
    # Every call site that discovered this template, in discovery order.
    locations: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)

    @property
    def bound_base_name(self) -> str:
        return f"{self.class_name}Bound"

    @property
    def result_type(self) -> str:
        return result_model_name(self.class_name)


@dataclass(kw_only=True, frozen=True)
class CollectedOnTypeBase:
    # Один плоский base на каждый фактический type condition фрагментов пакета.
    # Совместимость конкретного slot с этими bases кодирует его bind signature.
    name: str  # On{Type}
    graphql_type_name: str


@dataclass(kw_only=True, frozen=True)
class CollectedPlainFragment:
    # Как у `CollectedOperation.stmt_texts`: один dispatch entry на каждое
    # уникальное literal spelling.
    stmt_texts: tuple[str, ...]
    # Все call sites в discovery order: одно имя может встречаться в нескольких
    # модулях, поэтому диагностика должна показывать их все.
    locations: tuple[str, ...]
    class_name: str
    fragment_name: str
    model_name: str
    # On-type base собственного type condition фрагмента.
    on_type: str
    # Readable closure фрагмента: он сам плюс каждый фрагмент, достижимый через
    # его root-level spreads, с пересечением по всем совместимым slot types
    # пакета. Это имена классов: сначала собственный класс фрагмента, затем
    # остальные по алфавиту. Closure записывается в base expression класса как
    # phantom без собственного runtime-значения; IR хранит его для renderer.
    closure: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)

    @property
    def bound_closure(self) -> tuple[str, ...]:
        return self.closure

    @property
    def dispatch_class_name(self) -> str:
        return self.class_name


@dataclass(kw_only=True, frozen=True)
class CollectedFactoryFragment:
    stmt_texts: tuple[str, ...]
    locations: tuple[str, ...]
    class_name: str
    fragment_name: str
    model_name: str
    on_type: str
    closure: tuple[str, ...]
    applied_class_name: str
    arg_vars: tuple[CollectedFragmentArg, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)

    @property
    def bound_closure(self) -> tuple[str, ...]:
        return self.closure

    @property
    def dispatch_class_name(self) -> str:
        return self.applied_class_name


type CollectedFragment = CollectedPlainFragment | CollectedFactoryFragment


@dataclass(kw_only=True, frozen=True)
class CollectedReadableFragment:
    fragment: CollectedFragment
    # Порядок фиксирован, чтобы rendered literal не зависел от set iteration.
    typenames: tuple[str, ...]
    # Whether the bind named this fragment for this slot; see
    # `bindings.ReadableFragment.direct`, which is where it is decided.
    direct: bool


@dataclass(kw_only=True, frozen=True)
class CollectedBindingSlot:
    slot: CollectedTemplateSlot
    # Все fragments, читаемые в root slot: переданные в bind и достигнутые через
    # их root-level spreads. Каждый entry хранит typenames и признак direct.
    # Renderer переносит набор в bind dispatch table; `bound__` создаёт из spec
    # readers и передаёт весь набор в `validate_slot__` для независимого чтения
    # и boundary validation.
    readable_fragments: tuple[CollectedReadableFragment, ...]

    @property
    def direct_fragments(self) -> tuple[CollectedFragment, ...]:
        # Точный набор fragments, переданных caller в этот slot, в порядке
        # GraphQL fragment names; `()` означает empty slot. Набор выводится из
        # readable set, потому что direct fragment всегда читаем в своём slot и
        # отдельный tuple дублировал бы тот же факт.
        #
        # The order of the `bind()` call is not preserved, and could not be:
        # `bindings.readable_fragments` reaches these through a graph walk that
        # unions each fragment's typenames over every path to it and emits the
        # result by name -- a fragment reached along two paths has no single
        # call position to keep, and one reached transitively has none at all.
        # Binding определяется combination, поэтому logical combination и
        # runtime dispatch независимо сортируют fragments. Два call sites с
        # разным порядком должны попадать в одну dispatch entry; overload text
        # обязан быть только deterministic.
        return tuple(
            readable.fragment for readable in self.readable_fragments if readable.direct
        )


@dataclass(kw_only=True, frozen=True)
class CollectedBinding:
    # Логическая идентичность комбинации: template и GraphQL fragment names по
    # slots. Она нужна discovery и IR, но не runtime dispatch: там fragment
    # идентифицируется generated definition class.
    combination_key: CombinationKey
    template: CollectedTemplate
    exec_source: str
    slots: tuple[CollectedBindingSlot, ...]  # every template slot, template order
    # Where this combination is written, which is one of two places since it
    # stopped coming from a call-site scan: every `.bind(...)` that spells it,
    # in discovery order, when any does -- and otherwise the template's own
    # statement plus the statement of each fragment it spreads, because the
    # schema is what produced it and those are the statements a developer
    # edits to change it (`collect._combination_locations`). Not the same
    # contract as `CollectedOperation.locations`, which is always call sites.
    locations: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)


@dataclass(kw_only=True, frozen=True)
class CollectedPackageIR:
    result_artifacts: list[CollectedArtifact]
    input_artifacts: list[CollectedArtifact]
    operations: list[CollectedOperation]
    fragments: list[CollectedFragment]
    # One entry per composite type the package needs a base for -- see
    # `CollectedOnTypeBase`. Rendered ahead of the fragment classes that
    # derive from them (`render.render_package`).
    on_type_bases: list[CollectedOnTypeBase]
    templates: list[CollectedTemplate]
    bindings: list[CollectedBinding]
    enums: list[CollectedEnum]
    # Models validating inside a slot or fragment subtree: rendered on the
    # open (extra="ignore") base, because their payloads carry other readers'
    # fields next to their own, and excluded from the rename pass so an open
    # model can never converge with a strict one.
    open_model_names: frozenset[str]
    # Every statement the scan discovered, in discovery order. Carried so
    # `passthrough_texts` is derived here rather than beside the renderer:
    # both halves of that subtraction are facts about this IR.
    discovered_texts: tuple[str, ...]

    @property
    def passthrough_texts(self) -> tuple[str, ...]:
        # Discovered statements без typed artifact: каждый bundle и, только в
        # пакете без templates, каждый single-fragment statement. При наличии
        # template любой одиночный fragment становится typed definition
        # независимо от совместимости со slots. Эти call sites корректно
        # получают untyped catch-all.
        #
        # The typed side is enumerated here, where the artifact kinds that own
        # a `stmt_texts` are declared -- a hand-written list of them elsewhere
        # would go stale the day a fourth kind is added, and the statement it
        # forgot would be sent to a server verbatim. A template's text is
        # typed for a reason of its own: `render_templates` renders it, and
        # passing it through would ship the never-stripped `@slot` directive.
        typed = frozenset(
            text
            for artifacts in (self.operations, self.fragments, self.templates)
            for artifact in artifacts
            for text in artifact.stmt_texts
        )
        return tuple(
            dict.fromkeys(text for text in self.discovered_texts if text not in typed)
        )
