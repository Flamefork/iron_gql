"""Пакетный контракт вызова `bind()`.

`slots.combination_key` задаёт логическую идентичность комбинации, а
`slots.binding_key` — nominal identity bindable fragment classes внутри template.
Инвариант наблюдаем в одной точке: generated `bind()` обязан выбрать правильную
entry.

Каждый вызов из `queries.py` должен выбрать entry своей комбинации. Опечатка в
slot, пустое значение под ошибочным именем и повтор fragment должны отвергаться;
перестановка keywords или fragments должна сохранять комбинацию.
"""

from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import cast

import pydantic
import pytest

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.slots import GQLFragment
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package

SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
    preview: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""

generated_package(
    "bind_contract",
    schema=SCHEMA,
    queries='''
    from tests.generated.bind_contract.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
                preview @slot { __typename }
            }
        }
        """
    )

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {
            url
        }
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {
            href
        }
        """
    )

    nothing = get_attachment.bind()
    one = get_attachment.bind(attachment=image_parts)
    two = get_attachment.bind(attachment=(image_parts, link_parts))
    both_slots = get_attachment.bind(attachment=image_parts, preview=link_parts)
    ''',
)

from tests.generated.bind_contract import queries
from tests.generated.bind_contract.gql.api import GetAttachment
from tests.generated.bind_contract.gql.api import ImageParts
from tests.generated.bind_contract.gql.api import LinkParts

IMAGE_PARTS = ImageParts()
LINK_PARTS = LinkParts()

# Every call `queries.py` writes, as (the value it produced, the keywords it
# wrote). The keywords are what the mutations below are derived from, so the
# corpus and the source stay one thing.
type Passed = dict[str, GQLFragment[pydantic.BaseModel, Any] | Sequence[Any]]

WRITTEN: list[tuple[str, object, Passed]] = [
    ("nothing", queries.nothing, {}),
    ("one", queries.one, {"attachment": IMAGE_PARTS}),
    ("two", queries.two, {"attachment": [IMAGE_PARTS, LINK_PARTS]}),
    (
        "both",
        queries.both_slots,
        {"attachment": IMAGE_PARTS, "preview": LINK_PARTS},
    ),
]


def _bind(passed: Passed) -> object:
    # The generated `bind()` reached the way a caller reaches it. Typed as
    # `object` because the point of each assertion below is *which* class comes
    # back, never what it can do.
    bind = cast("Callable[..., object]", GetAttachment().bind)
    return bind(**passed)


@pytest.mark.parametrize(
    ("written", "passed"),
    [(written, passed) for _, written, passed in WRITTEN],
    ids=[name for name, _, _ in WRITTEN],
)
def test_every_written_call_resolves_to_its_bound_type(written: object, passed: Passed):
    assert type(_bind(passed)) is type(written)


def _misspellings(passed: Passed) -> list[Passed]:
    # A slot renamed to something the template does not have, and -- the case
    # the key's normalisation used to swallow -- that same misspelling passed
    # nothing at all.
    mutated: list[Passed] = [{**passed, "typo": []}]
    mutated.extend(
        {**{k: v for k, v in passed.items() if k != slot}, "typo": passed[slot]}
        for slot in passed
    )
    return mutated


MISSPELLED: list[tuple[str, Passed]] = [
    (f"{name}-{index}", mutated)
    for name, _, passed in WRITTEN
    for index, mutated in enumerate(_misspellings(passed))
]


@pytest.mark.parametrize(
    "passed",
    [passed for _, passed in MISSPELLED],
    ids=[name for name, _ in MISSPELLED],
)
def test_a_keyword_naming_no_slot_is_refused(passed: Passed):
    # TypeError from the interpreter, which knows the parameter names, rather
    # than a lookup that has already normalised the misspelling away.
    with pytest.raises(TypeError, match="typo"):
        _ = _bind(passed)


@pytest.mark.parametrize(
    "passed",
    [
        {"attachment": [IMAGE_PARTS, IMAGE_PARTS]},
        {"attachment": [IMAGE_PARTS], "preview": [LINK_PARTS, LINK_PARTS]},
    ],
    ids=["one-slot", "other-slot"],
)
def test_a_repeated_fragment_is_refused(passed: Passed):
    # A slot spreads each of its fragments once, so this asks for a
    # combination that cannot exist -- and the binding key, which sorts and
    # keeps both classes, must not quietly answer with the one-of-each binding.
    with pytest.raises(LookupError, match="unknown bind combination"):
        _ = _bind(passed)


# (how it is written, which written call it must come back as). One
# combination has several spellings -- a bare fragment or a one-element list,
# the keywords in either order, a slot's fragments in either order -- and the
# key is the only thing that decides, so it sorts.
SPELLINGS: list[tuple[str, Passed, str]] = [
    ("list-of-one", {"attachment": [IMAGE_PARTS]}, "one"),
    (
        "keywords-reversed",
        {"preview": LINK_PARTS, "attachment": IMAGE_PARTS},
        "both",
    ),
    (
        "list-of-one-in-a-pair",
        {"attachment": IMAGE_PARTS, "preview": [LINK_PARTS]},
        "both",
    ),
    ("fragments-reversed", {"attachment": [LINK_PARTS, IMAGE_PARTS]}, "two"),
]


@pytest.mark.parametrize(
    ("passed", "expected"),
    [(passed, expected) for _, passed, expected in SPELLINGS],
    ids=[name for name, _, _ in SPELLINGS],
)
def test_spellings_of_one_combination_answer_alike(passed: Passed, expected: str):
    written = next(value for name, value, _ in WRITTEN if name == expected)
    assert type(_bind(passed)) is type(written)


def test_an_unfilled_slot_and_an_explicit_empty_list_agree():
    # The normalisation the key does perform, and the one thing it is for.
    assert type(_bind({"attachment": []})) is type(queries.nothing)
    assert type(_bind({"attachment": [], "preview": []})) is type(queries.nothing)


def _misspelled_source(*, extra: str) -> str:
    return f'''
    from sample_app.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {{
            post(id: $id) {{
                id
                attachment @slot {{ __typename }}
            }}
        }}
        """
    )
{extra}
    bound = get_attachment.bind(typo=[])
    '''


@pytest.mark.parametrize(
    "extra",
    ["", "    other = get_attachment.bind()\n"],
    ids=["alone", "beside-an-empty-bind"],
)
def test_a_misspelled_slot_is_diagnosed_whatever_else_the_tree_holds(
    test_project: ProjectBuilder, extra: str
):
    # The metamorphic half: a call's diagnosis is the call's own business. It
    # was not -- the scan merged binds on the normalised key before any slot
    # name had been checked, so `bind(typo=[])` collapsed onto a plain
    # `bind()` written anywhere in the tree and generation went through in
    # silence. Whether an unrelated line exists in another statement must not
    # decide whether this line is an error.
    test_project.prepare(schema=SCHEMA, queries=_misspelled_source(extra=extra))
    with pytest.raises(GraphQLGenerationError, match="unknown slot 'typo'"):
        _ = test_project.generate()
