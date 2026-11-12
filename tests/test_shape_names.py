"""Names of result shapes, result union aliases and the classes the developer named.

ADR 0003: a result union alias is a shape like a model -- equal aliases
merge, and the alias is named after its abstract type -- and a detailed model
name spells nested shapes one level deep. ADR 0004: classes named after an
operation or a fragment carry a kind suffix, and a shape claiming one of their
names fails generation.
"""

import inspect
import re
import types
import typing
from types import ModuleType

import pydantic
import pytest

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.runtime import GQLBoundOperation
from iron_gql.runtime import GQLOperation
from iron_gql.runtime import GQLTemplate
from iron_gql.slots import SlotReaders
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.corpus import kind_suffixes
from tests.corpus import pinned_name_claims
from tests.corpus import shape_merges
from tests.corpus.input_names import INPUT_NAMES
from tests.corpus.input_names import InputName
from tests.corpus.kind_suffixes import KIND_SUFFIXES
from tests.corpus.kind_suffixes import KindSuffix
from tests.corpus.name_collisions import NAME_COLLISIONS
from tests.corpus.pinned_name_claims import PINNED_NAME_CLAIMS
from tests.corpus.pinned_name_claims import PinnedNameClaim
from tests.corpus.shape_merges import SHAPE_MERGES
from tests.corpus.shape_merges import At
from tests.corpus.shape_merges import ShapeMerge

ONE_LEVEL = "shape_names_one_level"
[ONE_LEVEL_CASE] = [
    case
    for case in NAME_COLLISIONS
    if case.name == "shapes-differ-below-the-one-level-candidate"
]

# The committed package imports its own module, not the project's.
generated_package(
    ONE_LEVEL,
    schema=ONE_LEVEL_CASE.schema,
    queries=ONE_LEVEL_CASE.queries.replace(
        "from sample_app.gql.api import",
        f"from tests.generated.{ONE_LEVEL}.gql.api import",
    ),
)

from tests.generated.shape_names_one_level.gql import api as one_level_api

HASH = re.compile(r"[0-9a-fA-F]{32}")


def _queries(*statements: str) -> str:
    lines = ["from sample_app.gql.api import api_gql", ""]
    lines.extend(
        f"q{index} = api_gql({statement!r})"
        for index, statement in enumerate(statements)
    )
    return "\n".join(lines) + "\n"


def _module_values(api: ModuleType) -> dict[str, object]:
    # A module's namespace is `dict[str, Any]`; nothing below needs more than
    # `object` to tell what each value is.
    return typing.cast("dict[str, object]", vars(api))


def _aliases(api: ModuleType) -> set[str]:
    return {
        name
        for name, value in _module_values(api).items()
        if isinstance(value, typing.TypeAliasType)
    }


def _classes_of[T](api: ModuleType, base: type[T]) -> set[str]:
    return {
        name
        for name, value in _module_values(api).items()
        if isinstance(value, type)
        and issubclass(value, base)
        and value.__module__ == api.__name__
    }


def _operation_classes(api: ModuleType) -> set[str]:
    return _classes_of(api, GQLOperation)


def _models(api: ModuleType) -> set[str]:
    return _classes_of(api, pydantic.BaseModel)


def test_an_interface_fallback_and_its_alias_leave_the_bare_name_to_neither(
    test_project: ProjectBuilder,
):
    # The fallback model of `Node` and the alias over `Node` are two shapes of
    # one type and claim `Node` together, so neither holds it.
    case = _merge(
        "abstract=interface/operations=named-named/selection=same/order=a-first"
    )
    test_project.prepare(schema=shape_merges.SCHEMA, queries=case.queries())
    api, _queries_module = test_project.generate_and_import()
    assert "Node" not in vars(api)
    [alias] = _aliases(api)
    assert re.fullmatch(r"Node_[0-9a-f]{8}", alias)
    assert any(name.startswith("NodeWith") for name in _models(api))


def test_a_detailed_name_spells_nested_shapes_one_level_deep():
    # Two shapes of `Post` share the field set `author` and the one-level
    # candidate `PostWithAuthor_Opt_UserWithPosts`: what tells them apart
    # lies two levels down, so their digest suffixes do. The package is
    # committed, so `test_generated_typecheck` holds it to basedpyright.
    first = _model_at(one_level_api, "Q1Query", ("feed",)).__name__
    second = _model_at(one_level_api, "Q2Query", ("feed",)).__name__
    assert first != second
    for name in (first, second):
        assert re.fullmatch(r"PostWithAuthor_Opt_UserWithPosts_[0-9a-f]{8}", name)
    assert _model_at(one_level_api, "Q1Query", ("feed", "author")).__name__ == (
        "UserWithPosts_ListPostWithId"
    )


def test_a_reference_to_a_slot_model_is_spelled_one_level_deep(
    test_project: ProjectBuilder,
):
    # Two templates' `Post` differ only in which slot model `related` is, and
    # slot models keep their path names -- an anonymous template's carries
    # its hash. The parents are still spelled by the slot model's own type
    # and fields, and told apart by their digests.
    case = _merge("slots/abstract=interface/operations=named-anonymous/order=a-first")
    test_project.prepare(schema=shape_merges.SCHEMA, queries=case.queries())
    api, _queries_module = test_project.generate_and_import()
    parents = {name for name in _models(api) if name.startswith("PostWith")}
    assert len(parents) == 2
    for name in parents:
        assert re.fullmatch(
            r"PostWithIdRelated_IDOpt_NodeWithTypename_[0-9a-f]{8}", name
        )


@pytest.mark.parametrize("case", INPUT_NAMES, ids=[case.id for case in INPUT_NAMES])
def test_an_input_alias_keeps_its_type_name(
    test_project: ProjectBuilder, case: InputName
):
    test_project.prepare(schema=case.schema, queries=_queries(case.statement))
    api, _queries_module = test_project.generate_and_import()
    assert case.alias in _aliases(api)


def test_an_operation_named_after_a_type_leaves_the_type_its_name(
    test_project: ProjectBuilder,
):
    # The suffix is what leaves `Post` to the model (ADR 0004).
    [case] = [
        case
        for case in KIND_SUFFIXES
        if case.id == "kind=query/class=operation/name=type-name"
    ]
    test_project.prepare(schema=kind_suffixes.SCHEMA, queries=_queries(case.statement))
    api, _queries_module = test_project.generate_and_import()
    assert _model_at(api, case.class_name, ("post",)).__name__ == "Post"


def _artifact(annotation: object) -> object:
    # Unwraps `| None`, `list[...]` and `Annotated[...]` down to the model
    # class or the alias the generator refers to -- parameterized by a slot's
    # phantom inside a template's result.
    for candidate in (annotation, typing.get_origin(annotation)):
        if isinstance(candidate, typing.TypeAliasType) or (
            isinstance(candidate, type) and issubclass(candidate, pydantic.BaseModel)
        ):
            return candidate
    args = typing.cast("tuple[object, ...]", typing.get_args(annotation))
    [inner] = [arg for arg in args if arg is not type(None)]
    return _artifact(inner)


def _result_class(api: ModuleType, operation: type[object]) -> type[pydantic.BaseModel]:
    if issubclass(operation, GQLTemplate):
        # A template executes only once bound; its result class is the one
        # named after it.
        result = _module_values(api)[f"{operation.__name__}Result"]
    else:
        execute = typing.cast("object", inspect.getattr_static(operation, "execute"))
        hints = typing.cast("dict[str, object]", typing.get_type_hints(execute))
        result = _artifact(hints["return"])
    assert isinstance(result, type)
    assert issubclass(result, pydantic.BaseModel)
    return result


def _walk(root: type[pydantic.BaseModel], path: tuple[str, ...]) -> object:
    artifact: object = root
    for field in path:
        assert isinstance(artifact, type)
        assert issubclass(artifact, pydantic.BaseModel)
        artifact = _artifact(artifact.model_fields[field].annotation)
    return artifact


def _model_at(
    api: ModuleType, operation_class: str, path: tuple[str, ...]
) -> type[pydantic.BaseModel]:
    operation = _module_values(api)[operation_class]
    assert isinstance(operation, type)
    model = _walk(_result_class(api, operation), path)
    assert isinstance(model, type)
    assert issubclass(model, pydantic.BaseModel)
    return model


def _at(api: ModuleType, queries: ModuleType, at: At) -> object:
    operation = type(_module_values(queries)[at.query])
    return _walk(_result_class(api, operation), at.path)


def _assert_groups(
    api: ModuleType, queries: ModuleType, groups: tuple[tuple[At, ...], ...]
) -> list[object]:
    # One object per group, a different one for each group.
    objects: list[object] = []
    for group in groups:
        first, *rest = (_at(api, queries, at) for at in group)
        assert all(other is first for other in rest), group
        objects.append(first)
    assert len({id(obj) for obj in objects}) == len(objects), groups
    return objects


@pytest.mark.parametrize("case", SHAPE_MERGES, ids=[case.id for case in SHAPE_MERGES])
def test_equal_shapes_are_one_class(test_project: ProjectBuilder, case: ShapeMerge):
    test_project.prepare(schema=shape_merges.SCHEMA, queries=case.queries())
    api, queries = test_project.generate_and_import()
    # An alias is reached through its parent's field: the parents of a group
    # refer to the group's alias.
    for alias in _assert_groups(api, queries, case.aliases):
        assert isinstance(alias, typing.TypeAliasType)
        exports = [
            name for name, value in _module_values(api).items() if value is alias
        ]
        assert len(exports) == 1, exports
    _ = _assert_groups(api, queries, case.parents)
    for payload in case.payloads:
        operation = _module_values(queries)[payload.query]
        result = _result_class(api, type(operation))
        try:
            _ = result.model_validate(payload.data, context=_slot_context(operation))
        except pydantic.ValidationError:
            assert not payload.accepted, payload
        else:
            assert payload.accepted, payload


def _slot_context(operation: object) -> SlotReaders | None:
    # A template's result validates its slot nodes against what `bind()`
    # offered them; bound to nothing, every slot gets an empty set.
    if not isinstance(operation, GQLTemplate):
        return None
    # `bind` is rendered on each template class; no runtime base declares it.
    bind = typing.cast(
        "typing.Callable[[GQLTemplate], GQLBoundOperation]",
        vars(type(operation))["bind"],
    )
    return bind(operation).slot_readers


def _merge(case_id: str) -> ShapeMerge:
    [case] = [case for case in SHAPE_MERGES if case.id == case_id]
    return case


DIFFERING_UNIONS = _merge(
    "abstract=union/operations=named-named/selection=differs/order=a-first"
)


def test_merged_aliases_and_parents_carry_no_operation_hash(
    test_project: ProjectBuilder,
):
    # ADR 0003: no name of a shape carries the path of an anonymous
    # operation -- only the result root, named after the operation, does.
    case = _merge(
        "abstract=union/operations=named-anonymous/selection=same/order=a-first"
    )
    test_project.prepare(schema=shape_merges.SCHEMA, queries=case.queries())
    api, _queries = test_project.generate_and_import()
    roots = {f"{operation}Result" for operation in _operation_classes(api)}
    hashed = {name for name in _models(api) | _aliases(api) if HASH.search(name)}
    assert hashed <= roots


def test_two_shapes_of_a_union_take_digest_names(test_project: ProjectBuilder):
    test_project.prepare(schema=shape_merges.SCHEMA, queries=DIFFERING_UNIONS.queries())
    api, _queries = test_project.generate_and_import()
    aliases = _aliases(api)
    assert len(aliases) == 2
    assert all(re.fullmatch(r"Media_[0-9a-f]{8}", alias) for alias in aliases)


def test_a_second_shape_takes_the_bare_name_away(test_project: ProjectBuilder):
    # ADR 0001: a query added to the module may take a name away -- the import
    # of `Media` fails -- but never hands it to another shape.
    test_project.prepare(
        schema=shape_merges.SCHEMA, queries=DIFFERING_UNIONS.queries("a")
    )
    api, _queries = test_project.generate_and_import()
    assert _aliases(api) == {"Media"}
    test_project.prepare(schema=shape_merges.SCHEMA, queries=DIFFERING_UNIONS.queries())
    api, _queries = test_project.generate_and_import()
    assert "Media" not in vars(api)


ANONYMOUS_HASH = "[0-9A-F][0-9a-f]{31}"


def _signature_annotations(named: type[object]) -> list[object]:
    # Where a class the developer named refers to the classes named after
    # it: an operation's `execute` returns its result, a template's `bind`
    # overloads return its bound base over its result, a fragment derives
    # from its on-type base over its data.
    if issubclass(named, GQLTemplate):
        # `bind` is rendered on each template class; no runtime base declares
        # it.
        bind = typing.cast("typing.Callable[..., object]", vars(named)["bind"])
        return [
            typing.cast("dict[str, object]", typing.get_type_hints(overload))["return"]
            # A template with one combination has a plain `bind`. The overload
            # registry is process-wide and keyed by module name, qualname and
            # line, so it also holds the overloads of same-named modules other
            # tests generated; only this module's own count.
            for overload in [
                overload
                for overload in typing.get_overloads(bind)
                if overload.__globals__ is bind.__globals__
            ]
            or [bind]
        ]
    if issubclass(named, GQLOperation):
        execute = typing.cast("object", inspect.getattr_static(named, "execute"))
        return [
            typing.cast("dict[str, object]", typing.get_type_hints(execute))["return"]
        ]
    return list(types.get_original_bases(named))


def _mentions(annotation: object, cls: object) -> bool:
    # A parameterized pydantic model is a subclass of its generic model
    # rather than an alias with an origin.
    origin = typing.get_origin(annotation)
    args = typing.cast("tuple[object, ...]", typing.get_args(annotation))
    return (
        annotation is cls
        or origin is cls
        or (
            isinstance(annotation, type)
            and isinstance(cls, type)
            and issubclass(annotation, pydantic.BaseModel)
            and issubclass(annotation, cls)
        )
        or any(_mentions(arg, cls) for arg in args)
    )


@pytest.mark.parametrize("case", KIND_SUFFIXES, ids=[case.id for case in KIND_SUFFIXES])
def test_classes_named_by_the_developer_carry_their_kind(
    test_project: ProjectBuilder, case: KindSuffix
):
    test_project.prepare(
        schema=kind_suffixes.SCHEMA, queries=_queries(case.statement, *case.context)
    )
    api, queries = test_project.generate_and_import()
    named = _module_values(queries)["q0"]
    pattern = re.escape(case.class_name).replace(
        re.escape(kind_suffixes.HASH), f"({ANONYMOUS_HASH})"
    )
    match = re.fullmatch(pattern, type(named).__name__)
    assert match, type(named).__name__
    values = _module_values(api)
    assert values[type(named).__name__] is type(named)
    hash_value = match.group(1) if match.groups() else ""
    annotations = _signature_annotations(type(named))
    for related in case.related:
        cls = values[related.replace(kind_suffixes.HASH, hash_value)]
        assert any(_mentions(annotation, cls) for annotation in annotations), related
    if case.args is not None:
        # `with_args` is rendered on each factory class; no runtime base
        # declares it.
        with_args = typing.cast(
            "typing.Callable[..., object]", vars(type(named))["with_args"]
        )
        assert type(with_args(named, **case.args)).__name__ == case.applied


@pytest.mark.parametrize(
    "case", PINNED_NAME_CLAIMS, ids=[case.id for case in PINNED_NAME_CLAIMS]
)
def test_a_shape_claiming_a_pinned_name_fails_generation(
    test_project: ProjectBuilder, case: PinnedNameClaim
):
    test_project.prepare(schema=case.schema, queries=_queries(*case.statements))
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    errors = exc_info.value.errors
    assert len(errors) == case.claims, errors
    for error in errors:
        assert error.startswith(case.prefix), error
        assert error.endswith(pinned_name_claims.HINT), error
    test_project.prepare(
        schema=case.resolved_schema, queries=_queries(*case.resolved_statements)
    )
    api, _queries_module = test_project.generate_and_import()
    values = _module_values(api)
    for name, artifact in case.resolved:
        match artifact:
            case "model":
                assert name in _models(api), name
            case "alias":
                assert isinstance(values[name], typing.TypeAliasType), name
