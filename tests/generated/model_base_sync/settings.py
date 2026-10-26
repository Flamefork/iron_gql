import pydantic

GRAPHQL_URL = "http://testserver/graphql/"


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        hide_input_in_errors=True,
        alias_generator=str.upper,
        extra="forbid",
        serialize_by_alias=False,
        populate_by_name=False,
    )
