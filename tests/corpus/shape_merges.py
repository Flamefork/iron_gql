"""Selections whose union or interface aliases are one shape (ADR 0003).

An alias is a shape like a model: two queries selecting the same variants
under different paths -- named or anonymous, in any order of variants and
fields -- get one alias, and parents that differed only in which alias they
referred to get one class. A selection differing in one variant is the
control against merging too much. Two templates' slots never merge, however
alike their static selections: a slot's models are pinned to their path, and
the alias over them names them.

Every case holds two query variables, `a` and `b`, and is generated in both
orders of declaration; its id spells every axis. The groups are written from
the axes here, not read from the generator's shape key.
"""

from dataclasses import dataclass

from tests.corpus.name_collisions import Payload

SCHEMA = """
type Query {
    post: Post
    posts: [Post!]!
}

interface Node {
    id: ID!
}

type Post implements Node {
    id: ID!
    media: Media
    medias: [Media!]!
    related: Node
    author: User
}

type User implements Node {
    id: ID!
    name: String!
    email: String!
    avatar: Media
}

union Media = Photo | Video

type Photo {
    url: String!
    width: Int!
}

type Video {
    url: String!
    duration: Int!
}
"""


@dataclass(frozen=True, kw_only=True)
class At:
    # A query variable of the case and the field path from its result. For a
    # parent, the path reaches the model; for an alias, its last field is the
    # one whose annotation is the alias.
    query: str
    path: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ShapeMerge:
    id: str
    # The source of each query variable; `order` is which comes first.
    statements: dict[str, str]
    order: str
    # Each group is one object; two groups are two objects.
    aliases: tuple[tuple[At, ...], ...]
    parents: tuple[tuple[At, ...], ...]
    payloads: tuple[Payload, ...]

    def queries(self, *variables: str) -> str:
        # The queries module declaring `variables`, all of them by default.
        chosen = sorted(variables or self.statements, reverse=self.order == "b-first")
        lines = ["from sample_app.gql.api import api_gql", ""]
        lines.extend(
            f"{variable} = api_gql({self.statements[variable]!r})"
            for variable in chosen
        )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, kw_only=True)
class _Abstract:
    name: str
    field: str
    # Selections of `field`: the same one twice, its variants and fields
    # permuted, and one with a single variant selecting something else.
    same: str
    permuted: str
    differs: str
    # The data of `field` that only the base selection accepts, and the data
    # only the differing one accepts.
    base_only: dict[str, object]
    differs_only: dict[str, object]
    # A type the abstract type covers, and one it does not.
    member: str
    foreign: str


ABSTRACTS: tuple[_Abstract, ...] = (
    _Abstract(
        name="union",
        field="media",
        same="__typename ... on Photo { url width } ... on Video { url duration }",
        permuted="__typename ... on Video { duration url } ... on Photo { width url }",
        differs="__typename ... on Photo { url width } ... on Video { url }",
        base_only={"__typename": "Video", "url": "v", "duration": 3},
        differs_only={"__typename": "Video", "url": "v"},
        member="Photo",
        foreign="Post",
    ),
    _Abstract(
        name="interface",
        field="related",
        same="__typename id ... on User { name }",
        permuted="... on User { name } id __typename",
        differs="__typename id ... on User { email }",
        base_only={"__typename": "User", "id": "u", "name": "n"},
        differs_only={"__typename": "User", "id": "u", "email": "e"},
        member="User",
        foreign="Photo",
    ),
)

# The two roots the queries reach their parents by: one nullable, one a list,
# so the parents' paths differ while the parents' shapes are the same.
ROOTS = {"a": "post", "b": "posts"}

OPERATIONS = {
    "named-named": ("query A ", "query B "),
    "named-anonymous": ("query A ", ""),
    "anonymous-anonymous": ("", ""),
}

ORDERS = ("a-first", "b-first")


def _data(query: str, item: dict[str, object]) -> dict[str, object]:
    root = ROOTS[query]
    return {root: [item] if root == "posts" else item}


def _selection_case(
    abstract: _Abstract, operations: str, selection: str, order: str
) -> ShapeMerge:
    header_a, header_b = OPERATIONS[operations]
    selection_b = {
        "same": abstract.same,
        "permuted": abstract.permuted,
        "differs": abstract.differs,
    }[selection]
    field = abstract.field
    statements = {
        "a": f"{header_a}{{ post {{ id {field} {{ {abstract.same} }} }} }}",
        "b": f"{header_b}{{ posts {{ id {field} {{ {selection_b} }} }} }}",
    }
    alias = {query: At(query=query, path=(ROOTS[query], field)) for query in ROOTS}
    parent = {query: At(query=query, path=(ROOTS[query],)) for query in ROOTS}
    merged = selection != "differs"
    base_only: dict[str, object] = {"id": "1", field: abstract.base_only}
    differs_only: dict[str, object] = {"id": "1", field: abstract.differs_only}
    return ShapeMerge(
        id=f"abstract={abstract.name}/operations={operations}/selection={selection}/order={order}",
        statements=statements,
        order=order,
        aliases=((alias["a"], alias["b"]),)
        if merged
        else ((alias["a"],), (alias["b"],)),
        parents=(
            ((parent["a"], parent["b"]),)
            if merged
            else ((parent["a"],), (parent["b"],))
        ),
        payloads=(
            Payload(query="a", data=_data("a", base_only), accepted=True),
            Payload(query="a", data=_data("a", differs_only), accepted=False),
            Payload(query="b", data=_data("b", differs_only), accepted=not merged),
            # Whether the differing selection rejects the base data depends
            # on the variant: an extra field passes, a missing one does not.
            *(
                (Payload(query="b", data=_data("b", base_only), accepted=True),)
                if merged
                else ()
            ),
        ),
    )


def _represented_case(placement: str, order: str) -> ShapeMerge:
    # The alias is one shape wherever it sits: under a list instead of a
    # nullable field, or a level deeper. Its parents differ -- another field,
    # another type -- so they stay two classes. One representative each,
    # with the union selected the same way by two named queries.
    selection = ABSTRACTS[0].same
    reach = {
        "wrapper=list": (
            ("posts", "medias"),
            f"posts {{ id medias {{ {selection} }} }}",
        ),
        "depth=2": (
            ("post", "author", "avatar"),
            f"post {{ author {{ id avatar {{ {selection} }} }} }}",
        ),
    }
    path_b, query_b = reach[placement]
    statements = {
        "a": f"query A {{ post {{ id media {{ {selection} }} }} }}",
        "b": f"query B {{ {query_b} }}",
    }
    video = {"__typename": "Video", "url": "v", "duration": 3}
    data_b: dict[str, object] = (
        {"posts": [{"id": "1", "medias": [video]}]}
        if placement == "wrapper=list"
        else {"post": {"author": {"id": "u", "avatar": video}}}
    )
    return ShapeMerge(
        id=f"{placement}/order={order}",
        statements=statements,
        order=order,
        aliases=((At(query="a", path=("post", "media")), At(query="b", path=path_b)),),
        parents=((At(query="a", path=("post",)),), (At(query="b", path=path_b[:-1]),)),
        payloads=(
            Payload(
                query="a", data={"post": {"id": "1", "media": video}}, accepted=True
            ),
            Payload(query="b", data=data_b, accepted=True),
        ),
    )


def _slot_case(abstract: _Abstract, operations: str, order: str) -> ShapeMerge:
    # The same static selection over two templates' slots stays apart. A slot
    # over a union is one pinned model per variant and an alias over them; a
    # slot over an interface is a single pinned model, with no alias to tell
    # apart -- only its parents.
    header_a, header_b = OPERATIONS[operations]
    field = abstract.field
    statements = {
        "a": f"{header_a}{{ post {{ id {field} @slot {{ __typename }} }} }}",
        "b": f"{header_b}{{ posts {{ id {field} @slot {{ __typename }} }} }}",
    }
    aliases = (
        (
            (At(query="a", path=("post", field)),),
            (At(query="b", path=("posts", field)),),
        )
        if abstract.name == "union"
        else ()
    )
    # These payloads tell a valid response from an invalid one; the two slots'
    # static selections are the same, so no payload tells the slots apart --
    # the identity groups do. A slot's node validates only against the
    # context its template's `bind()` builds.
    node: dict[str, object] = {"__typename": abstract.member}
    responses: tuple[tuple[dict[str, object], bool], ...] = (
        ({"id": "1", field: node}, True),
        ({"id": "1", field: {}}, False),
        ({"id": "1", field: {"__typename": abstract.foreign}}, False),
    )
    return ShapeMerge(
        id=f"slots/abstract={abstract.name}/operations={operations}/order={order}",
        statements=statements,
        order=order,
        aliases=aliases,
        parents=((At(query="a", path=("post",)),), (At(query="b", path=("posts",)),)),
        payloads=tuple(
            Payload(query=query, data=_data(query, item), accepted=accepted)
            for query in ROOTS
            for item, accepted in responses
        ),
    )


SHAPE_MERGES: tuple[ShapeMerge, ...] = (
    *(
        _selection_case(abstract, operations, selection, order)
        for abstract in ABSTRACTS
        for operations in OPERATIONS
        for selection in ("same", "permuted", "differs")
        for order in ORDERS
    ),
    *(
        _represented_case(placement, order)
        for placement in ("wrapper=list", "depth=2")
        for order in ORDERS
    ),
    *(
        _slot_case(abstract, "named-named", order)
        for abstract in ABSTRACTS
        for order in ORDERS
    ),
    # One representative with an anonymous template: its hash stays out of
    # the parents' names.
    *(_slot_case(ABSTRACTS[1], "named-anonymous", order) for order in ORDERS),
)
