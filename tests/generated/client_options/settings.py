from typing import ClassVar

from iron_gql.runtime import AsyncGQLClientOptions


class CONFIG:
    url: ClassVar[str] = "http://testserver/graphql/"
    gql: ClassVar[AsyncGQLClientOptions] = {
        "headers": {"X-Tenant": "t-1"},
        "connect_timeout": 2.5,
        "timeout": 30,
        "stream_idle_timeout": 60,
        "httpx_options": {"trust_env": False},
    }
