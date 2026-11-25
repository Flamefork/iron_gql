from typing import ClassVar

from iron_gql.runtime import GQLClientOptions


class CONFIG:
    url: ClassVar[str] = "http://testserver/graphql/"
    gql: ClassVar[GQLClientOptions] = {
        "headers": {"X-Tenant": "t-1"},
        "connect_timeout": 2.5,
        "timeout": 30,
        "stream_idle_timeout": 60,
        "httpx_options": {"trust_env": False},
    }
