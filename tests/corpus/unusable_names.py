"""Module-level names that are not usable Python identifiers, by origin.

Such a name reaches the generated module unchanged from one of two origins: a
generator option (the gql function named after `module_full_name`, the names
the configured imports bind) or a GraphQL type (a model or an enum keeps the
type's name). Operation, template and fragment classes are capitalized and
suffixed on the way, so a keyword spelled there still yields an identifier and
has no case here.

An id names the origin and, for an option, which one; for a type, its kind and
how the name fails -- a hyphen or a leading digit, or a Python keyword.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

PING_SCHEMA = "type Query { ping: String }"
PING_STATEMENT = "query Ping { ping }"


class RenderOptions(TypedDict, total=False):
    module_full_name: str
    base_url_import: str
    to_camel_fn_full_name: str
    model_base_import: str
    scalars: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class UnusableName:
    id: str
    schema: str
    statement: str
    options: RenderOptions
    # The name as the diagnosis quotes it, and the origin it names.
    name: str
    origin: str


UNUSABLE_NAMES: tuple[UnusableName, ...] = (
    UnusableName(
        id="origin=option/option=module_full_name/fails=hyphen",
        schema=PING_SCHEMA,
        statement=PING_STATEMENT,
        options={"module_full_name": "sample_app.gql.my-api"},
        name="my-api_gql",
        origin="the generated gql function",
    ),
    UnusableName(
        id="origin=option/option=base_url_import/fails=keyword",
        schema=PING_SCHEMA,
        statement=PING_STATEMENT,
        options={"base_url_import": "sample_app.settings:class"},
        name="class",
        origin="from sample_app.settings import class",
    ),
    UnusableName(
        id="origin=option/option=to_camel_fn_full_name/fails=hyphen",
        schema=PING_SCHEMA,
        statement=PING_STATEMENT,
        options={"to_camel_fn_full_name": "my-case:to_camel"},
        name="my-case",
        origin="import my-case",
    ),
    UnusableName(
        id="origin=option/option=model_base_import/fails=keyword",
        schema=PING_SCHEMA,
        statement=PING_STATEMENT,
        options={"model_base_import": "None:BaseModel"},
        name="None",
        origin="import None",
    ),
    UnusableName(
        id="origin=option/option=scalars/fails=leading-digit",
        schema="scalar Stamp\ntype Query { ping: Stamp }",
        statement=PING_STATEMENT,
        options={"scalars": {"Stamp": "1time:Stamp"}},
        name="1time",
        origin="import 1time",
    ),
    UnusableName(
        id="origin=type/kind=object/fails=keyword",
        schema="type class { value: String }\ntype Query { item: class }",
        statement="query Get { item { value } }",
        options={},
        name="class",
        origin="model 'class'",
    ),
    UnusableName(
        id="origin=type/kind=input/fails=keyword",
        schema=(
            "input import { value: String }\ntype Query { ping(arg: import): String }"
        ),
        statement="query Get($arg: import) { ping(arg: $arg) }",
        options={},
        name="import",
        origin="model 'import'",
    ),
    UnusableName(
        id="origin=type/kind=union/fails=keyword",
        schema=(
            "type Item { value: String }\nunion async = Item\n"
            "type Query { item: async }"
        ),
        statement="query Get { item { __typename ... on Item { value } } }",
        options={},
        name="async",
        origin="model 'async'",
    ),
    UnusableName(
        id="origin=type/kind=interface/fails=keyword",
        schema=(
            "interface None { value: String }\n"
            "type Item implements None { value: String }\n"
            "type Query { item: None }"
        ),
        statement="query Get { item { __typename value } }",
        options={},
        name="None",
        origin="model 'None'",
    ),
    UnusableName(
        id="origin=type/kind=enum/fails=keyword",
        schema="enum True { YES }\ntype Query { ping(arg: True): String }",
        statement="query Get($arg: True) { ping(arg: $arg) }",
        options={},
        name="True",
        origin="enum 'True'",
    ),
)
