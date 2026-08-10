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
    # Every reference is written bare: no artifact this generator emits takes
    # type parameters. A slot's offered fragments reach its node model as a
    # concrete base (`GQLSlotModel[ImageParts]`), stamped per binding by
    # `collect.specialize_bindings`, so nothing has to be threaded through the
    # references on the way there.
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
    # The fragment handle classes readable on this node, written into its base
    # as the offered-fragments phantom. Stamped by
    # `collect.specialize_bindings`, which copies a template's result models
    # once per binding; empty is `Never` -- nothing is readable there, because
    # the binding left the slot unfilled or because no bind names the template
    # at all. Only a node model (`slot_name` set) carries a phantom.
    offered_fragments: tuple[str, ...] = ()
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
        union_expr = " | ".join(
            render_type_expr(NamedRef(name=variant)) for variant in self.variants
        )
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
class CollectedBindingArg:
    # A binding's `with_args` parameter: the synthesized fragment variable
    # together with the one fact that makes it differ from an operation
    # variable. Carried on the variable itself rather than as a set of GraphQL
    # names beside it, so no layer has to re-join the two by string -- a join
    # that would keep type-checking and silently stop matching the day a
    # variable is identified by anything but its raw GraphQL name.
    var: CollectedOperationVar
    # Whether a caller may leave this one out of `with_args`: every position
    # it fills declares a schema default, and an absent variable is the only
    # spelling that lets that default apply (see
    # `bindings.SynthesizedVar.omittable`). Every other fragment variable is a
    # required keyword whose `None` is sent as an explicit null, exactly like
    # an operation variable of `execute`.
    omittable: bool


def result_model_name(class_name: str) -> str:
    # The one statement of the rule. The model collector names the artifact
    # with it, both IR kinds report it back under `result_type`, and `execute`
    # renders that into `client.query(...)`; a second spelling of it would
    # surface as a NameError in the generated module, not as a generation
    # error.
    return f"{class_name}Result"


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


@dataclass(kw_only=True, frozen=True)
class CollectedTemplate:
    # Same contract as CollectedOperation.stmt_texts: one dispatch entry per
    # distinct literal spelling.
    stmt_texts: tuple[str, ...]
    class_name: str  # operation name, e.g. "GetAttachment"
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
class CollectedFragment:
    # Same contract as CollectedOperation.stmt_texts: one dispatch entry per
    # distinct literal spelling.
    stmt_texts: tuple[str, ...]
    # Every call site that discovered this fragment, in discovery order -- the
    # same contract as CollectedOperation.locations, for the same reason: one
    # name may be written in several modules (dedup keeps the first and checks
    # the rest agree), and a diagnosis that quotes one of them sends the
    # developer to a file that may not be the one they have to edit.
    locations: tuple[str, ...]
    class_name: str
    singleton_name: str
    fragment_name: str
    model_name: str

    @property
    def location(self) -> str:
        return ", ".join(self.locations)


@dataclass(kw_only=True, frozen=True)
class CollectedSlotHandle:
    fragment: CollectedFragment
    # Sorted, so the rendered literal does not depend on set iteration order.
    typenames: tuple[str, ...]
    # Whether the bind named this fragment for this slot; see
    # `bindings.ReadableFragment.direct`, which is where it is decided.
    direct: bool


@dataclass(kw_only=True, frozen=True)
class CollectedBindingSlot:
    slot: CollectedTemplateSlot
    # Every fragment readable at this slot's root -- what the bind named plus
    # whatever those fragments spread at their own root level -- sorted by
    # fragment name, each with the typenames it is reachable at and whether
    # the bind named it. Renders as `slot_handles__`: the whole set is offered
    # to `validate_slot__` so each one reads independently and is
    # boundary-validated.
    readable_handles: tuple[CollectedSlotHandle, ...]

    @property
    def direct_fragments(self) -> tuple[CollectedFragment, ...]:
        # Exactly the set the caller passed to `bind()` for this slot, sorted by
        # GraphQL fragment name; () = empty slot. Drives the binding's
        # overloads, its class name and the runtime dispatch key. Read off the
        # readable set rather than stored beside it: a bind's own fragment is
        # always readable at the slot it was passed to
        # (`bindings._validate_slot_args` rejects one whose type cannot
        # overlap), so a second tuple would only be the same fact written twice
        # and kept in step by nothing.
        #
        # The order of the `bind()` call is not preserved, and could not be:
        # `bindings._readable_fragments` reaches these through a graph walk that
        # unions each fragment's typenames over every path to it and emits the
        # result by name -- a fragment reached along two paths has no single
        # call position to keep, and one reached transitively has none at all.
        # Nor would keeping it help: a binding *is* its combination, so the two
        # consumers that turn this into an identity (the class name, the
        # dispatch key) sort for themselves through `slots.bind_key_shape`, and
        # two call sites listing the same fragments in different orders must
        # land on one class. What is left is the rendered text of `bind()`'s
        # overloads, which only needs to be deterministic.
        return tuple(
            handle.fragment for handle in self.readable_handles if handle.direct
        )


@dataclass(kw_only=True, frozen=True)
class CollectedBinding:
    # Named after the combination it is, never after the name a call site
    # happened to assign it to: `{Template}With{Slot}{Fragments…}` per filled
    # slot, slots and fragments in the canonical order of `slots.bind_key`
    # (see `collect.binding_class_name`). That is what lets the same
    # combination be written in several places, and in any scope, and still
    # mean one class.
    class_name: str
    template: CollectedTemplate
    exec_source: str
    slots: tuple[CollectedBindingSlot, ...]  # every template slot, template order
    arg_vars: tuple[CollectedBindingArg, ...]  # fragment variables (for with_args)
    # Every call site that binds this combination, in discovery order.
    locations: tuple[str, ...]

    @property
    def location(self) -> str:
        return ", ".join(self.locations)

    @property
    def required_arg_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(arg.var.gql_name for arg in self.arg_vars if not arg.omittable)
        )

    def specialized_name(self, name: str) -> str:
        # The name of this binding's own copy of one of its template's result
        # artifacts: the binding's name where the template's stood, so
        # `GetAttachmentResultPostAttachmentSlot` becomes
        # `GetAttachmentWithAttachmentImagePartsResultPostAttachmentSlot`. A
        # name the rename pass shortened out of that prefix (`Post`) simply
        # takes the binding's name in front; every binding class name starts
        # with its template's, so both cases are the one rule.
        #
        # One statement of it, used by `specialize_bindings` for the models it
        # writes and by `result_type` below for the one `execute` names -- a
        # second spelling would surface as a NameError in the generated module
        # rather than as a generation error.
        return self.class_name + name.removeprefix(self.template.class_name)

    @property
    def result_type(self) -> str:
        return self.specialized_name(self.template.result_type)


def bindings_by_template(
    bindings: list[CollectedBinding],
) -> dict[str, list[CollectedBinding]]:
    # Bindings stay in their overall (file, lineno) discovery order within each
    # group.
    grouped: dict[str, list[CollectedBinding]] = {}
    for binding in bindings:
        grouped.setdefault(binding.template.class_name, []).append(binding)
    return grouped


@dataclass(kw_only=True, frozen=True)
class CollectedPackageIR:
    result_artifacts: list[CollectedArtifact]
    # A binding's own result models: the artifacts on the path to one of its
    # template's slot nodes, copied per binding with the offered fragments
    # written in (see `collect.specialize_bindings`). Kept apart from the
    # shared ones because a node model names fragment handle *classes* in its
    # base, and a base -- unlike every other reference the module makes -- is
    # evaluated where it is written: these are rendered after the handles,
    # the rest before them. Empty until specialization runs.
    binding_artifacts: list[CollectedArtifact]
    input_artifacts: list[CollectedArtifact]
    operations: list[CollectedOperation]
    fragments: list[CollectedFragment]
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
        # Statements the scan discovered but nothing typed: fragment bundles
        # and single fragments no bind accepts. Their fragments are spread
        # statically by name, and the call site legitimately receives the
        # untyped catch-all -- only a statement the generator has never seen
        # is an error there.
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
