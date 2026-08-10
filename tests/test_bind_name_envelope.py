"""Slot names that collide with what the generator writes around them.

Every other fixture names its slots after its domain -- `attachment`,
`preview`, `board` -- so the whole family of names that are legal GraphQL and
also spell something the renderer emits was covered by two hand-written cases
(`slots`, rejected; `msg`, accepted). `cls` was in neither, and a slot called
`cls` generated a module that ran correctly and failed to type-check.

So the names are a corpus, crossed with the two shapes a template can have --
one discovered binding or several: a package, committed like every other,
whose slots are named after the renderer's own vocabulary. It is type-checked by
`test_generated_typecheck` with the rest of them, and every generated package
-- this one included -- answers `assert_method_namespaces_are_closed`, which
is what makes the *next* such name a failure rather than a corpus entry
somebody has to think of.
"""

from pathlib import Path

import pytest

from tests.conftest import basedpyright_report
from tests.conftest import generated_package
from tests.conftest import write_text

SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""

# The renderer's vocabulary, as far as a GraphQL alias can spell it: the two
# locals `bind()`'s body has held (`cls`, `msg`), the pieces of the dispatch it
# writes (`key`, `dispatch`, `fragments`), the method's own name, and the
# lower-case spelling of every module and type its signatures name. `slots` is
# absent on purpose -- it is the one name a claim reserves, and
# `test_bindings_generation` pins its rejection.
COLLIDING_SLOTS = (
    "cls",
    "msg",
    "key",
    "bind",
    "fragments",
    "dispatch",
    "pydantic",
    "runtime",
    "sequence",
)

_SLOT_LINES = "\n".join(
    f"                {name}: attachment @slot {{ __typename }}"
    for name in COLLIDING_SLOTS
)

generated_package(
    "bind_name_envelope",
    schema=SCHEMA,
    queries=f'''
    from tests.generated.bind_name_envelope.gql.api import api_gql

    image_parts = api_gql(
        """
        fragment ImageParts on ImageAttachment {{
            url
        }}
        """
    )

    link_parts = api_gql(
        """
        fragment LinkParts on LinkAttachment {{
            href
        }}
        """
    )

    # Several bindings: one `@overload` each over the shared implementation.
    overloaded = api_gql(
        """
        query Overloaded($id: ID!) {{
            post(id: $id) {{
                id
{_SLOT_LINES}
            }}
        }}
        """
    )

    # A single binding, where the renderer has to write the second signature
    # itself -- the same names, the other end of the axis.
    inline = api_gql(
        """
        query Inline($id: ID!) {{
            post(id: $id) {{
                id
                cls: attachment @slot {{ __typename }}
            }}
        }}
        """
    )

    overloaded_cls = overloaded.bind(cls=image_parts)
    overloaded_pair = overloaded.bind(pydantic=link_parts, runtime=image_parts)
    inline_cls = inline.bind(cls=image_parts)
    ''',
)

from tests.generated.bind_name_envelope import queries
from tests.generated.bind_name_envelope.gql import api


@pytest.mark.parametrize("template", ["overloaded", "inline"])
@pytest.mark.parametrize("call", [{}, {"cls": []}])
def test_a_combination_no_binding_covers_raises_whatever_the_template_holds(
    template: str, call: dict[str, list[object]]
):
    # README.md: omitting a slot and passing it `[]` mean the same thing, and
    # a combination the generator never saw raises `LookupError` where the
    # call runs. Neither promise may depend on how many bindings a template
    # happens to have -- and both did: with exactly one binding filling a
    # slot, that slot became a required parameter, so the omitted spelling
    # raised `TypeError` from the interpreter before any dispatch ran while
    # the `[]` spelling raised the documented `LookupError`.
    #
    # `bind` is reached as `object` because the point is what happens at
    # runtime to a call the signatures describe as unmatched: statically both
    # spellings resolve to the `-> Never` overload.
    owner = getattr(api, template.capitalize())()  # pyright: ignore[reportAny]
    with pytest.raises(LookupError, match="regenerate"):
        owner.bind(**call)  # pyright: ignore[reportAny]


def test_an_unmatched_call_is_typed_as_never_whatever_the_template_holds(
    tmp_path: Path,
):
    # The static half of the same promise. `Never` is what a call that always
    # raises returns, and it is written by the renderer for exactly the
    # combinations no binding covers -- so a caller sees the problem at the
    # call rather than at the `LookupError` a run later.
    # One call per function: a `Never` expression makes everything after it
    # unreachable, which is the honest reading and would otherwise bury the
    # next `reveal_type` under a diagnostic of its own.
    check_file = tmp_path / "check_unmatched.py"
    write_text(
        check_file,
        """
            from tests.generated.bind_name_envelope.gql.api import Inline, Overloaded

            def omitted_on_a_lone_binding() -> None:
                reveal_type(Inline().bind())

            def empty_on_a_lone_binding() -> None:
                reveal_type(Inline().bind(cls=[]))

            def omitted_with_several_bindings() -> None:
                reveal_type(Overloaded().bind())

            def empty_with_several_bindings() -> None:
                reveal_type(Overloaded().bind(cls=[]))
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == [], f"expected no type errors, got: {errors}"
    infos = [d for d in diagnostics if d.severity == "information"]
    assert [info.message for info in infos] == [
        'Type of "Inline().bind()" is "Never"',
        'Type of "Inline().bind(cls=[])" is "Never"',
        'Type of "Overloaded().bind()" is "Never"',
        'Type of "Overloaded().bind(cls=[])" is "Never"',
    ]


def test_each_form_dispatches_on_a_colliding_slot_name():
    # The runtime half: the module type-checks (`test_generated_typecheck`)
    # *and* the calls still reach the class their combination generated. A
    # parameter shadowed by a local answered correctly here while the module
    # failed to type-check, so neither half stands in for the other.
    assert type(queries.overloaded_cls).__name__ == "OverloadedWithClsImageParts"
    assert type(queries.overloaded_pair).__name__ == (
        "OverloadedWithPydanticLinkPartsWithRuntimeImageParts"
    )
    assert type(queries.inline_cls).__name__ == "InlineWithClsImageParts"
