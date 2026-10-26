"""Pydantic's secret types, crossed with the depths of request variables.

A secret masks itself in `repr` and in every pydantic dump, `mode="json"`
included, so a secret the request walk leaves to pydantic reaches the server as
`**********`. The wire value is what the server must receive instead: the
secret's own value, serialized the way the request serializes a value of that
type. Each depth reaches the request walk by its own route: the variable
itself, a field of an input model (dumped first, then walked), a list element,
a value under a mapping key.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pydantic

# `Secret[T]` is generic; a scalar maps to a `module:attr`, so the parameterized
# type needs a name of its own -- which is how a consumer would map it too.
SecretInt = pydantic.Secret[int]


@dataclass(frozen=True, kw_only=True)
class SecretKind:
    # The schema's scalar and the Python name of every variable and input
    # field of that scalar; one lowercase word, so the two spellings agree.
    scalar: str
    name: str
    # The scalar's mapping, as `render_gql_module(scalars=…)` takes it.
    python: str
    value: object
    wire: object
    # A second secret of the same type and another value: masked, the two
    # look alike.
    other: object


KINDS: tuple[SecretKind, ...] = (
    SecretKind(
        scalar="SecretString",
        name="string",
        python="pydantic:SecretStr",
        value=pydantic.SecretStr("str-s3cret"),
        wire="str-s3cret",
        other=pydantic.SecretStr("str-other"),
    ),
    SecretKind(
        scalar="SecretBlob",
        name="blob",
        python="pydantic:SecretBytes",
        value=pydantic.SecretBytes(b"bytes-s3cret"),
        wire="bytes-s3cret",
        other=pydantic.SecretBytes(b"bytes-other"),
    ),
    SecretKind(
        scalar="SecretNumber",
        name="number",
        python="tests.corpus.secrets:SecretInt",
        value=SecretInt(424242),
        wire=424242,
        other=SecretInt(171717),
    ),
)


def _fields(template: str) -> str:
    return ", ".join(template.format(kind=kind) for kind in KINDS)


# Two scalars from `pydantic`, which the module imports itself, and two from
# `builtins` (`ID` and `JSON`): the generated module imports each once.
SCALARS: dict[str, str] = {
    "ID": "builtins:str",
    **{kind.scalar: kind.python for kind in KINDS},
    "JSON": "builtins:object",
}


def schema() -> str:
    return f"""
    {"\n".join(f"scalar {kind.scalar}" for kind in KINDS)}
    scalar JSON

    input Vault {{
        {"\n".join(f"{kind.name}: {kind.scalar}" for kind in KINDS)}
    }}

    type Query {{
        direct({_fields("{kind.name}: {kind.scalar}")}): Boolean!
        inInput(vault: Vault!): Boolean!
        inList({_fields("{kind.name}s: [{kind.scalar}!]")}): Boolean!
        inMapping(bag: JSON!): Boolean!
    }}
    """


def queries(package: str) -> str:
    return f'''
    from tests.generated.{package}.gql.api import api_gql

    direct = api_gql(
        """
        query Direct({_fields("${kind.name}: {kind.scalar}")}) {{
            direct({_fields("{kind.name}: ${kind.name}")})
        }}
        """
    )

    in_input = api_gql(
        """
        query InInput($vault: Vault!) {{
            inInput(vault: $vault)
        }}
        """
    )

    in_list = api_gql(
        """
        query InList({_fields("${kind.name}s: [{kind.scalar}!]")}) {{
            inList({_fields("{kind.name}s: ${kind.name}s")})
        }}
        """
    )

    in_mapping = api_gql(
        """
        query InMapping($bag: JSON!) {{
            inMapping(bag: $bag)
        }}
        """
    )
    '''


# Builds the generated input model `Vault` from its fields; the corpus does
# not import the generated module, so the test hands the class in.
type VaultFactory = Callable[[dict[str, object]], object]


@dataclass(frozen=True, kw_only=True)
class Depth:
    # Where in the variables the secret sits. Each depth is one operation of
    # `queries()`, named as the module binds it and as the response answers.
    name: str
    operation: str
    field: str
    # The keyword arguments of `execute` that carry the secret at this depth,
    # and the variables the server must receive for them.
    call: Callable[[SecretKind, VaultFactory], dict[str, object]]
    wire: Callable[[SecretKind], dict[str, object]]


def _others_null(template: str) -> dict[str, object]:
    # An operation that takes every kind takes each as a required keyword; the
    # kinds a case is not about are passed as null.
    return {template.format(kind.name): None for kind in KINDS}


DEPTHS: tuple[Depth, ...] = (
    Depth(
        name="variable",
        operation="direct",
        field="direct",
        call=lambda kind, _vault: _others_null("{}") | {kind.name: kind.value},
        wire=lambda kind: {kind.name: kind.wire},
    ),
    Depth(
        name="input-field",
        operation="in_input",
        field="inInput",
        call=lambda kind, vault: {"vault": vault({kind.name: kind.value})},
        wire=lambda kind: {"vault": {kind.name: kind.wire}},
    ),
    Depth(
        name="list-element",
        operation="in_list",
        field="inList",
        call=lambda kind, _vault: _others_null("{}s") | {f"{kind.name}s": [kind.value]},
        wire=lambda kind: {f"{kind.name}s": [kind.wire]},
    ),
    Depth(
        name="mapping-value",
        operation="in_mapping",
        field="inMapping",
        call=lambda kind, _vault: {"bag": {"key": kind.value}},
        wire=lambda kind: {"bag": {"key": kind.wire}},
    ),
)
