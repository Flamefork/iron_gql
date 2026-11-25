from typing import ClassVar

from iron_gql.runtime import GQLClientOptions


class CONFIG:
    url: ClassVar[str] = "http://testserver/graphql/"
    gql: ClassVar[GQLClientOptions] = {
        "headers": {"X-Tenant": "t-1"},
        "query_timeout": 30,
        "subscription_idle_timeout": 2.5,
        "httpx_options": {"trust_env": False},
    }
