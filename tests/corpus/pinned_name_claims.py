"""A shape claiming a pinned name fails generation (ADR 0004).

A class the developer named -- an operation, a template, a fragment, the
classes named after them -- and every model and alias of a slot's or a
fragment's subtree is pinned: its name never moves. A shape of a model or an
alias that merely skipped such a name would hold another one and take this
one the day its owner is renamed, so any name the shape claims, chosen or not,
fails generation in either order of declaration.

The owner matrix crosses each pinned name with a claimant spelled after it --
a GraphQL object type (a model) or a union (an alias), claimed by its bare
name -- and both orders. The kind of operation is an axis of the owners an
operation or a template names; a fragment's names carry no kind, and a slot's
subtree is named along its template, whose kinds the template owners cover.
A second block runs the paths a model claims by -- bare, short, detailed,
digest -- against one owner each; aliases claim only the bare and the digest
name, so the short and detailed paths have no alias claimant.

Each case is also generated with the claimant's type renamed
(`resolved_statements`, `resolved_schema`): the coincidence gone, the module
generates and imports.
"""

import re
from dataclasses import dataclass
from typing import Literal

type Artifact = Literal["model", "alias"]

HINT = "rename the operation, template or fragment so the two cannot meet"

BASE_SCHEMA = """
type Query {{
    post: Post
    feed: [Post!]!
{query_fields}
}}

type Mutation {{
    touch: Post
}}

type Subscription {{
    posted: Post
}}

enum Fragment {{
    A
    B
}}

type Post {{
    id: ID!
    media: Media
    author: User
    query: User
    kind: Fragment
}}

type User {{
    id: ID!
    name: String
    avatar: Photo
}}

union Media = Photo | Video

type Photo {{
    url: String!
    thumb(width: Int!): String
}}

type Video {{
    url: String!
}}
{types}
"""

# Where a fragment can be bound: a slot of each type the fragments below are
# on. Its own names claim nothing the cases spell.
HOLDER = (
    "query Holder { post @slot { __typename } "
    "feed { media @slot { __typename } author @slot { __typename } } }"
)

RENAMED = "Renamed"


@dataclass(frozen=True, kw_only=True)
class PinnedNameClaim:
    id: str
    schema: str
    statements: tuple[str, ...]
    # The GraphQL type whose shape claims the name.
    claimant_type: str
    # Every error starts so; the shapes claiming the name are `claims`.
    prefix: str
    claims: int = 1
    # Names the resolved module exports and what each is: what the claimant
    # took once the coincidence was gone, or the pinned artifact itself.
    resolved: tuple[tuple[str, Artifact], ...] = ()

    def _rename(self, text: str) -> str:
        return re.sub(rf"\b{self.claimant_type}\b", self.claimant_type + RENAMED, text)

    @property
    def resolved_schema(self) -> str:
        return self._rename(self.schema)

    @property
    def resolved_statements(self) -> tuple[str, ...]:
        return tuple(self._rename(statement) for statement in self.statements)


@dataclass(frozen=True, kw_only=True)
class _Owner:
    id: str
    # The first statement is the one the owner's location points at.
    statements: tuple[str, ...]
    name: str
    # What the error calls the owner; `{at}` is its location.
    description: str
    resolved: tuple[tuple[str, Artifact], ...] = ()


@dataclass(frozen=True, kw_only=True)
class _Kind:
    keyword: str
    root: str
    operation: str
    operation_result: str
    template: str
    template_result: str
    template_bound: str


KINDS: tuple[_Kind, ...] = (
    _Kind(
        keyword="query",
        root="post",
        operation="OpQuery",
        operation_result="OpQueryResult",
        template="TmplQuery",
        template_result="TmplQueryResult",
        template_bound="TmplQueryBound",
    ),
    _Kind(
        keyword="mutation",
        root="touch",
        operation="OpMutation",
        operation_result="OpMutationResult",
        template="TmplMutation",
        template_result="TmplMutationResult",
        template_bound="TmplMutationBound",
    ),
    _Kind(
        keyword="subscription",
        root="posted",
        operation="OpSubscription",
        operation_result="OpSubscriptionResult",
        template="TmplSubscription",
        template_result="TmplSubscriptionResult",
        template_bound="TmplSubscriptionBound",
    ),
)


def _kind_owners(kind: _Kind) -> tuple[_Owner, ...]:
    operation = f"{kind.keyword} Op {{ {kind.root} {{ id }} }}"
    template = (
        f"{kind.keyword} Tmpl {{ {kind.root} {{ id media @slot {{ __typename }} }} }}"
    )
    return (
        _Owner(
            id=f"owner=operation/kind={kind.keyword}",
            statements=(operation,),
            name=kind.operation,
            description=f"operation '{kind.operation}' at {{at}}",
        ),
        _Owner(
            id=f"owner=operation-result/kind={kind.keyword}",
            statements=(operation,),
            name=kind.operation_result,
            description=f"the result class of operation '{kind.operation}' at {{at}}",
        ),
        _Owner(
            id=f"owner=template/kind={kind.keyword}",
            statements=(template,),
            name=kind.template,
            description=f"template '{kind.template}' at {{at}}",
        ),
        _Owner(
            id=f"owner=template-result/kind={kind.keyword}",
            statements=(template,),
            name=kind.template_result,
            description=f"the result class of template '{kind.template}' at {{at}}",
        ),
        _Owner(
            id=f"owner=template-bound/kind={kind.keyword}",
            statements=(template,),
            name=kind.template_bound,
            description=f"the bound base of template '{kind.template}' at {{at}}",
        ),
    )


SLOT_TEMPLATE = (
    "query Tmpl { post @slot { __typename author { avatar { url } } "
    "media { __typename ... on Photo { url } } } }"
)
UNION_SLOT_TEMPLATE = "query Tmpl { post { id media @slot { __typename } } }"

OWNERS: tuple[_Owner, ...] = (
    *(owner for kind in KINDS for owner in _kind_owners(kind)),
    _Owner(
        id="owner=fragment",
        statements=("fragment Parts on Photo { url }", HOLDER),
        name="PartsFragment",
        description="fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=factory-applied",
        statements=("fragment Parts on Photo { thumb(width: $width) }", HOLDER),
        name="_PartsFragmentApplied",
        description="the applied class of fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=on-type-base",
        statements=("fragment Parts on Photo { url }", HOLDER),
        name="OnPhoto",
        description="the on-type base of 'Photo'",
    ),
    _Owner(
        id="owner=fragment-root-model",
        statements=("fragment Parts on User { avatar { url } }", HOLDER),
        name="PartsData",
        description="the model of fragment 'Parts' at {at}",
        resolved=(("PartsData", "model"),),
    ),
    _Owner(
        # Pinned with its subtree rather than named after its union.
        id="owner=fragment-root-alias",
        statements=(
            "fragment Parts on Media { __typename ... on Photo { url } }",
            HOLDER,
        ),
        name="PartsData",
        description="the model of fragment 'Parts' at {at}",
        resolved=(("PartsData", "alias"),),
    ),
    _Owner(
        id="owner=fragment-descendant",
        statements=("fragment Parts on User { avatar { url } }", HOLDER),
        name="PartsDataAvatar",
        description="a model inside fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=fragment-descendant-alias",
        statements=(
            "fragment Parts on Post { media { __typename ... on Photo { url } } }",
            HOLDER,
        ),
        name="PartsDataMedia",
        description="a model inside fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=fragment-descendant-through-alias",
        statements=(
            "fragment Parts on Post { media { __typename ... on Photo { url } } }",
            HOLDER,
        ),
        name="PartsDataMediaPhoto",
        description="a model inside fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=fragment-descendant-two-levels",
        statements=("fragment Parts on Post { author { avatar { url } } }", HOLDER),
        name="PartsDataAuthorAvatar",
        description="a model inside fragment 'Parts' at {at}",
    ),
    _Owner(
        id="owner=slot-root",
        statements=(SLOT_TEMPLATE,),
        name="TmplQueryResultPostSlot",
        description="the model of slot 'post'",
    ),
    _Owner(
        id="owner=slot-root-of-union",
        statements=(UNION_SLOT_TEMPLATE,),
        name="TmplQueryResultPostMediaSlotPhoto",
        description="the model of slot 'media'",
    ),
    _Owner(
        id="owner=slot-descendant",
        statements=(SLOT_TEMPLATE,),
        name="TmplQueryResultPostSlotAuthor",
        description="a model inside slot 'post'",
    ),
    _Owner(
        id="owner=slot-descendant-alias",
        statements=(SLOT_TEMPLATE,),
        name="TmplQueryResultPostSlotMedia",
        description="a model inside slot 'post'",
    ),
    _Owner(
        id="owner=slot-descendant-through-alias",
        statements=(SLOT_TEMPLATE,),
        name="TmplQueryResultPostSlotMediaPhoto",
        description="a model inside slot 'post'",
    ),
    _Owner(
        id="owner=slot-descendant-two-levels",
        statements=(SLOT_TEMPLATE,),
        name="TmplQueryResultPostSlotAuthorAvatar",
        description="a model inside slot 'post'",
    ),
)

ORDERS = ("owner-first", "claimant-first")

# The first statement's line in the queries module: an import and a blank
# line come before it.
FIRST_LINE = 3


def _case(
    *,
    axes: str,
    owner: _Owner,
    claimant: tuple[str, ...],
    claimant_type: str,
    order: str,
    query_fields: str = "",
    types: str = "",
    own_name: str = "",
    claims: int = 1,
    resolved: tuple[tuple[str, Artifact], ...] = (),
) -> PinnedNameClaim:
    statements = (
        (*owner.statements, *claimant)
        if order == "owner-first"
        else (*claimant, *owner.statements)
    )
    at = f"sample_app/queries.py:{FIRST_LINE + statements.index(owner.statements[0])}"
    return PinnedNameClaim(
        id=f"{axes}/{owner.id}/order={order}",
        schema=BASE_SCHEMA.format(query_fields=query_fields, types=types),
        statements=statements,
        claimant_type=claimant_type,
        prefix=(
            f"Name '{owner.name}' of {owner.description.format(at=at)} is also "
            f"claimed by a selection of GraphQL type '{claimant_type}' "
            f"(its own name '{own_name}"
        ),
        claims=claims,
        resolved=(*owner.resolved, *resolved),
    )


# A claimant spelled after a name, by the bare name of its GraphQL type: its
# type's definition and the selection of it through `Query.claim`.
CLAIMANTS = {
    "model": ("type {name} {{\n    id: ID!\n}}", "{{ claim {{ id }} }}"),
    "alias": (
        "union {name} = Photo | Video",
        "{{ claim {{ __typename ... on Photo {{ url }} }} }}",
    ),
}


def _owner_case(owner: _Owner, claimant: str, order: str) -> PinnedNameClaim:
    types, selection = CLAIMANTS[claimant]
    return _case(
        axes=f"path=bare/usable=yes/claimant={claimant}",
        owner=owner,
        claimant=(selection.format(),),
        claimant_type=owner.name,
        order=order,
        query_fields=f"    claim: {owner.name}",
        types=types.format(name=owner.name),
    )


SHORT_OWNER = _Owner(
    id="owner=operation/kind=query",
    statements=("query PostWith { feed { id } }",),
    name="PostWithQuery",
    description="operation 'PostWithQuery' at {at}",
)

BARE = _Owner(
    id="owner=operation/kind=query",
    statements=("query Op { feed { id } }",),
    name="OpQuery",
    description="operation 'OpQuery' at {at}",
)

# A digest name ends in hex, so no kind suffix spells it: the on-type base of
# a type spelled after a digest name does. `OnPick_141529a4` is the first
# digest name of the alias the claimant below selects over the union `OnPick`
# -- `Photo { __typename url } | Video { __typename }`: the first 8 hex digits
# of the SHA-256 of the alias's shape key. The value was read off the module
# generated with a second shape of `OnPick` beside this one, which moves the
# alias off its bare name without touching its key. A change of the key's
# spelling moves every digest name; this case is meant to surface it.
DIGEST = "141529a4"
DIGEST_TYPE = f"Pick_{DIGEST}"
DIGEST_OWNER = _Owner(
    id="owner=on-type-base",
    statements=(
        f"fragment Parts on {DIGEST_TYPE} {{ id }}",
        "query Holder { pick @slot { __typename } }",
    ),
    name=f"OnPick_{DIGEST}",
    description=f"the on-type base of '{DIGEST_TYPE}'",
)

DETAILED_OWNER = _Owner(
    id="owner=fragment",
    statements=("fragment PostWithKind_Opt_ on Post { id }", HOLDER),
    name="PostWithKind_Opt_Fragment",
    description="fragment 'PostWithKind_Opt_' at {at}",
)


def _path_cases(order: str) -> tuple[PinnedNameClaim, ...]:
    return (
        # A type with two shapes cannot give either its bare name, and
        # still claims it.
        _case(
            axes="path=bare/usable=no/claimant=model",
            owner=BARE,
            claimant=("{ claim { id } }", "{ claims { name } }"),
            claimant_type="OpQuery",
            order=order,
            query_fields="    claim: OpQuery\n    claims: [OpQuery!]!",
            types="type OpQuery {\n    id: ID!\n    name: String\n}",
            claims=2,
        ),
        # `Post` has other shapes, so the field set `query` names its shape.
        _case(
            axes="path=short/usable=yes/claimant=model",
            owner=SHORT_OWNER,
            claimant=("{ post { query { id } } }",),
            claimant_type="Post",
            order=order,
        ),
        # Two shapes of `Post` select the field set `query`: neither can take
        # the short name, and both claim it.
        _case(
            axes="path=short/usable=no/claimant=model",
            owner=SHORT_OWNER,
            claimant=("{ post { query { id } } }", "{ feed { query { name } } }"),
            claimant_type="Post",
            order=order,
            claims=2,
        ),
        # The holder's `feed` selects another shape of `Post`, so this one
        # takes its short name; its detailed name, which it did not choose,
        # is a fragment's class.
        _case(
            axes="path=detailed/chosen=short/claimant=model",
            owner=DETAILED_OWNER,
            claimant=("{ post { kind } }",),
            claimant_type="Post",
            order=order,
            own_name="PostWithKind_Opt_Fragment",
            resolved=(("PostRenamedWithKind", "model"),),
        ),
        _case(
            axes="path=digest/usable=yes/claimant=alias",
            owner=DIGEST_OWNER,
            claimant=("{ claim { __typename ... on Photo { url } } }",),
            claimant_type="OnPick",
            order=order,
            query_fields=f"    claim: OnPick\n    pick: {DIGEST_TYPE}",
            types=(
                f"union OnPick = Photo | Video\n\n"
                f"type {DIGEST_TYPE} {{\n    id: ID!\n}}"
            ),
            own_name=f"OnPick_{DIGEST}",
        ),
    )


PINNED_NAME_CLAIMS: tuple[PinnedNameClaim, ...] = (
    *(
        _owner_case(owner, claimant, order)
        for owner in OWNERS
        for claimant in ("model", "alias")
        for order in ORDERS
    ),
    *(case for order in ORDERS for case in _path_cases(order)),
)
