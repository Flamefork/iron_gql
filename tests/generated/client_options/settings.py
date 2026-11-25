from typing import ClassVar

from iron_gql.runtime import AsyncGQLClientOptions


class CONFIG:
    url: ClassVar[str] = "http://testserver/graphql/"
    gql: ClassVar[AsyncGQLClientOptions] = {
        "headers": {"X-Tenant": "t-1"},
        "query_timeout": 30,
        "subscription_idle_timeout": 2.5,
        "httpx_options": {"trust_env": False},
    }
