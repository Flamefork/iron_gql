"""Schema names that collide with what the generator writes around them.

Every other fixture names its slots after its domain -- `attachment`,
`preview`, `board` -- so the whole family of names that are legal GraphQL and
also spell something the renderer emits was covered by two hand-written cases
(`slots`, rejected; `msg`, accepted). `cls` was in neither, and a slot called
`cls` generated a module that ran correctly and failed to type-check.

A slot is not the only place such a name reaches, either: a template's
variables are `execute()`'s keywords, and a template taking `$cast` shadowed
the very call the bound base's `execute` makes -- generating a module that
type-checked nowhere and raised `TypeError: 'str' object is not callable`
before the request left.

So the names are a corpus, crossed with the two shapes `bind()` can be
rendered in -- a set of `@overload` stubs over an erased implementation, or a
single plain signature when nothing in the package can be spread into the
template's slots at all: a package, committed like every other, whose slots
are named after the renderer's own vocabulary. It is type-checked by
`test_generated_typecheck` with the rest of them, and every generated package
-- this one included -- answers `assert_method_namespaces_are_closed`, which
is what makes the *next* such name a failure rather than a corpus entry
somebody has to think of.
"""

import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from tests.conftest import generated_package
from tests.conftest import generated_source
from tests.conftest import gql_server

SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
    author: Author
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}

type Author {
    name: String!
}
"""

# The renderer's vocabulary, as far as a GraphQL alias can spell it: a local
# `bind()`'s body has held (`cls`), the method's own name, and the lower-case
# spelling of a module its signatures name. Three names rather than the nine
# the corpus started with: the slots multiply, so nine of them enumerate to
# 3**9 combinations and trip the limit (`combinations.MAX_COMBINATIONS_PER_
# TEMPLATE`) before any of them can be checked, while three prove exactly the
# same thing. `slots` is absent on purpose -- it is the one name a claim
# reserves, and `test_bindings_generation` pins its rejection.
COLLIDING_SLOTS = ("cls", "bind", "pydantic")

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

    # Slots two fragments are compatible with: `bind()` is a set of
    # `@overload` stubs over an erased implementation.
    #
    # `$cast` is the same axis in the other parameter namespace the generator
    # writes: a template's variables become `execute()`'s keywords, and the
    # bound base's `execute` reads a name of the renderer's own to reconcile
    # the result type it promises with the one class it validates against.
    overloaded = api_gql(
        """
        query Overloaded($cast: ID!) {{
            post(id: $cast) {{
                id
{_SLOT_LINES}
            }}
        }}
        """
    )

    # A slot of a type no fragment in the package is defined on: the empty
    # call is the only one `bind()` accepts, so it is written as one plain
    # signature -- the same names, the other end of the axis.
    inline = api_gql(
        """
        query Inline($id: ID!) {{
            post(id: $id) {{
                id
                cls: author @slot {{ __typename }}
            }}
        }}
        """
    )

    overloaded_cls = overloaded.bind(cls=image_parts)
    overloaded_pair = overloaded.bind(pydantic=link_parts, bind=image_parts)
    ''',
)

from tests.generated.bind_name_envelope import queries
from tests.generated.bind_name_envelope.gql import api


@pytest.mark.parametrize("template", ["overloaded", "inline"])
@pytest.mark.parametrize("call", [{}, {"cls": []}])
def test_an_empty_bind_answers_alike_whatever_shape_the_template_has(
    template: str, call: dict[str, list[object]]
):
    # README.md: omitting a slot and passing it `[]` mean the same thing. The
    # empty combination is enumerated for every template now, so both
    # spellings answer with a bound operation rather than the `LookupError`
    # they used to -- and neither promise may depend on how `bind()` happens
    # to be rendered. Both did: with one signature the filled slots were
    # required parameters, so the omitted spelling raised `TypeError` from the
    # interpreter before any dispatch ran while the `[]` spelling reached the
    # dispatch.
    #
    # `bind` is reached as `object` because the point is what happens at
    # runtime, and the two templates' signatures differ.
    owner = getattr(api, template.capitalize())()  # pyright: ignore[reportAny]
    bound = owner.bind(**call)  # pyright: ignore[reportAny]
    assert bound.slot_readers == dict.fromkeys(  # pyright: ignore[reportAny]
        COLLIDING_SLOTS if template == "overloaded" else ["cls"], ()
    )


def test_a_template_nothing_can_be_bound_into_renders_one_plain_signature():
    # `Inline`'s slot is an `Author`, and no fragment in the package is
    # defined on it: the empty call is the only combination, so there is one
    # signature -- and one signature is not an overload set. `@overload`
    # needs two, and a second stub standing for a call that can never happen
    # is a signature standing for nothing.
    source = generated_source("bind_name_envelope")
    body = source.split("class Inline(runtime.GQLTemplate):")[1].split("\n\n\n")[0]
    assert "@overload" not in body
    assert (
        "def bind(self, *, cls: Sequence[Never] = ()) "
        "-> InlineBound[InlineResult[Never]]:"
    ) in body


def test_each_form_dispatches_on_a_colliding_slot_name():
    # The runtime half: the module type-checks (`test_generated_typecheck`)
    # *and* the calls still reach the combination their own colliding slot
    # name named -- proving a parameter shadowed by a local (the historical
    # bug: a `cls` local took the parameter of a slot called `cls`, and the
    # module still ran, silently reaching the wrong combination) answers
    # correctly, which neither half stands in for the other. Every
    # combination of one template shares a class now, so "reaches the right
    # combination" is a content check on `slot_readers` -- what the offered
    # definition's fragment name is -- not a bound class identity check.
    [cls_reader] = queries.overloaded_cls.slot_readers["cls"]
    assert cls_reader.definition.fragment_name__ == "ImageParts"

    pair_readers = {
        slot: [reader.definition.fragment_name__ for reader in readers]
        for slot, readers in queries.overloaded_pair.slot_readers.items()
        if readers
    }
    assert pair_readers == {"pydantic": ["LinkParts"], "bind": ["ImageParts"]}


def _resolve_post(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    attachment = {"__typename": "ImageAttachment", "url": f"u-{id}"}
    return {"id": id, "attachment": attachment, "author": {"name": "a"}}


async def test_a_variable_named_after_the_execute_body_still_executes(
    httpserver: HTTPServer,
):
    # The static half of this name is answered by `test_generated_typecheck`
    # and `assert_method_namespaces_are_closed`; this is the running half.
    # `$cast` is `execute()`'s own keyword, and the body it sits in calls the
    # renderer's cast -- so the call the developer writes is the only thing
    # that can prove the two no longer share a namespace.
    async with gql_server(
        httpserver, "bind_name_envelope", {"Query": {"post": _resolve_post}}
    ):
        result = await queries.overloaded.bind().execute(cast="p1")
    assert result.post is not None
    assert result.post.id == "p1"
