import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass

import graphql

from iron_gql.codegen.ir import CollectedTemplate
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.slots import spreads_into

# The product grows exponentially in the number of slots, so a template that
# runs away is stopped rather than trimmed. The value is chosen by module
# size: 4 slots over 3 compatible fragments each is 256 texts (~150 KB), which
# is still a module a developer can open; 5 slots is 1024, which is not.
MAX_COMBINATIONS_PER_TEMPLATE = 256


class CombinationLimitError(GraphQLGenerationError):
    # Its own type so a caller can tell "this package asks for more texts than
    # a module should hold" from every other rejection of the GraphQL input,
    # which all share `GraphQLGenerationError`.
    pass


@dataclass(kw_only=True, frozen=True)
class Combination:
    # One template text to generate: the template, and per slot the fragments
    # spread into it. Every slot of the template is present -- one filled by
    # nothing carries an empty tuple -- so two combinations are equal exactly
    # when they mean the same document, whatever produced them.
    #
    # A literal bind may additionally carry a key that names no slot of the
    # template; it is kept rather than normalised away, because dropping it
    # would turn a misspelled slot into a silent no-op instead of the
    # diagnosis `bindings.expand_binding` raises for it.
    template_name: str  # CollectedTemplate.name, the GraphQL operation name
    slots: tuple[tuple[str, tuple[str, ...]], ...]  # bind() keyword -> fragment names


def compatible_fragment_names(
    *,
    schema: graphql.GraphQLSchema,
    slot_type: str,
    fragments: Mapping[str, graphql.FragmentDefinitionNode],
) -> tuple[str, ...]:
    # "This fragment can be spread into this slot", stated once and read
    # twice: here, to enumerate the combinations, and by
    # `collect._collect_templates`, to work out which on-type bases a slot's
    # `bind()` overloads accept. A second spelling of the rule would let the
    # signature and the dispatch table disagree about one pair.
    return tuple(
        sorted(
            name
            for name, definition in fragments.items()
            if spreads_into(schema, definition.type_condition.name.value, slot_type)
        )
    )


def enumerate_combinations(
    *,
    schema: graphql.GraphQLSchema,
    templates: list[CollectedTemplate],
    fragments: Mapping[str, graphql.FragmentDefinitionNode],
    literal_binds: list[Combination],
    limit: int = MAX_COMBINATIONS_PER_TEMPLATE,
) -> list[Combination]:
    # Для каждого template slot принимает пустое значение либо любой
    # spread-совместимый fragment пакета; комбинации образуют их декартово
    # произведение. Literal binds добавляют единственный отсутствующий класс —
    # slots с несколькими fragments в tuple.
    #
    # Правило едино для любого числа slots: порог по размеру создал бы два
    # режима и сохранил бы scan call sites как второй источник комбинаций.
    enumerated: list[Combination] = []
    errors: list[str] = []
    for template in templates:
        literal_for_template = [
            combination
            for combination in literal_binds
            if combination.template_name == template.name
        ]
        variants = [
            [
                (),
                *(
                    (name,)
                    for name in compatible_fragment_names(
                        schema=schema, slot_type=slot.type_name, fragments=fragments
                    )
                ),
            ]
            for slot in template.slots
        ]
        # Counted before the product is built, not after: the point of the
        # limit is to never materialise the runaway one.
        schema_total = math.prod(len(variant) for variant in variants)
        if schema_total > limit:
            errors.append(
                _limit_error(
                    template,
                    variants,
                    total=schema_total,
                    schema_total=schema_total,
                    literal_only=None,
                    limit=limit,
                )
            )
            continue
        schema_combinations = [
            Combination(
                template_name=template.name,
                slots=tuple(
                    (slot.python_name, names)
                    for slot, names in zip(template.slots, chosen, strict=True)
                ),
            )
            for chosen in itertools.product(*variants)
        ]
        final_combinations = list(
            dict.fromkeys([*schema_combinations, *literal_for_template])
        )
        total = len(final_combinations)
        if total > limit:
            errors.append(
                _limit_error(
                    template,
                    variants,
                    total=total,
                    schema_total=schema_total,
                    literal_only=total - schema_total,
                    limit=limit,
                )
            )
            continue
        enumerated.extend(schema_combinations)
    if errors:
        raise CombinationLimitError(errors)
    # Literal binds last, deduplicated against the enumeration: a bind that
    # writes a single fragment per slot spells a combination the product
    # already holds, and one text per combination is the whole point of the
    # key. `dict.fromkeys` keeps the enumeration's own order, which is
    # deterministic by construction (template order, then slot order).
    return list(dict.fromkeys([*enumerated, *literal_binds]))


def _limit_error(
    template: CollectedTemplate,
    variants: list[list[tuple[str, ...]]],
    *,
    total: int,
    schema_total: int,
    literal_only: int | None,
    limit: int,
) -> str:
    # The numbers are the message: "too many combinations" leaves a developer
    # with no way to see which slot to drop, and the per-slot counts are
    # exactly what says whether the fix is fewer slots or narrower ones.
    per_slot = ", ".join(
        # One variant per slot is "nothing", which is not a fragment.
        f"{slot.python_name}: {len(variant) - 1}"
        for slot, variant in zip(template.slots, variants, strict=True)
    )
    breakdown = (
        f"{schema_total} schema-derived"
        if literal_only is None
        else f"{schema_total} schema-derived, {literal_only} literal-only"
    )
    return (
        f"Template '{template.class_name}' at {template.location} enumerates to "
        f"{total} combinations, over the limit of {limit} "
        f"({breakdown}): it has "
        f"{len(template.slots)} slots, each ranging over nothing plus every "
        f"fragment compatible with it ({per_slot}). Split the template into "
        "smaller ones, or narrow the slots' types so fewer fragments are "
        "compatible"
    )
