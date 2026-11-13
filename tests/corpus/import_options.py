"""Values of the generator options that name an import, malformed ones by option
and how the value fails, accepted ones by option and the form they take.

`base_url_import`, `to_camel_fn_full_name`, `model_base_import` and each value
of `scalars` name an import as `module:name`, where the module and the name are
dotted paths of Python identifiers that are not keywords. An id names the
option and the failure: the `:` (none, or more than one), or the part that
fails (`module` or `name`) and how -- empty, an empty segment, a hyphen, a
leading digit, a keyword, whitespace. The diagnosis names the option as the
caller wrote it; a scalar's is `scalars['<scalar>']`. An accepted form's id
names the option and what makes the form worth a case; the test project has a
`sample_app.settings` with `GRAPHQL_URL` and a `CONFIG.url`, and a
`sample_app.type` module (`type` is a soft keyword) with `to_camel`.
"""

from dataclasses import dataclass

from tests.corpus.unusable_names import RenderOptions


@dataclass(frozen=True, kw_only=True)
class MalformedImportOption:
    id: str
    options: RenderOptions
    # The option as the diagnosis names it, the value it quotes, and what it
    # says is wrong with the value.
    option: str
    value: str
    problem: str


NO_COLON = "has no ':' between the module and the name"


MALFORMED_IMPORT_OPTIONS: tuple[MalformedImportOption, ...] = (
    MalformedImportOption(
        id="option=base_url_import/fails=no-colon",
        options={"base_url_import": "GRAPHQL_URL"},
        option="base_url_import",
        value="GRAPHQL_URL",
        problem=NO_COLON,
    ),
    MalformedImportOption(
        id="option=to_camel_fn_full_name/fails=no-colon",
        options={"to_camel_fn_full_name": "to_camel"},
        option="to_camel_fn_full_name",
        value="to_camel",
        problem=NO_COLON,
    ),
    MalformedImportOption(
        id="option=model_base_import/fails=no-colon",
        options={"model_base_import": "None"},
        option="model_base_import",
        value="None",
        problem=NO_COLON,
    ),
    MalformedImportOption(
        id="option=scalars/fails=no-colon",
        options={"scalars": {"Stamp": "class"}},
        option="scalars['Stamp']",
        value="class",
        problem=NO_COLON,
    ),
    MalformedImportOption(
        id="option=base_url_import/fails=empty",
        options={"base_url_import": ""},
        option="base_url_import",
        value="",
        problem=NO_COLON,
    ),
    MalformedImportOption(
        id="option=base_url_import/fails=second-colon",
        options={"base_url_import": "sample_app.settings:GRAPHQL:URL"},
        option="base_url_import",
        value="sample_app.settings:GRAPHQL:URL",
        problem="has more than one ':'",
    ),
    MalformedImportOption(
        id="option=base_url_import/fails=module-empty",
        options={"base_url_import": ":GRAPHQL_URL"},
        option="base_url_import",
        value=":GRAPHQL_URL",
        problem="names module '', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=base_url_import/fails=module-hyphen",
        options={"base_url_import": "my-settings:GRAPHQL_URL"},
        option="base_url_import",
        value="my-settings:GRAPHQL_URL",
        problem="names module 'my-settings', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=base_url_import/fails=name-keyword",
        options={"base_url_import": "sample_app.settings:class"},
        option="base_url_import",
        value="sample_app.settings:class",
        problem="names 'class', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=to_camel_fn_full_name/fails=module-hyphen",
        options={"to_camel_fn_full_name": "my-case:to_camel"},
        option="to_camel_fn_full_name",
        value="my-case:to_camel",
        problem="names module 'my-case', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=to_camel_fn_full_name/fails=name-empty",
        options={"to_camel_fn_full_name": "pydantic.alias_generators:"},
        option="to_camel_fn_full_name",
        value="pydantic.alias_generators:",
        problem="names '', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=to_camel_fn_full_name/fails=module-empty-segment",
        options={"to_camel_fn_full_name": "pydantic.:to_camel"},
        option="to_camel_fn_full_name",
        value="pydantic.:to_camel",
        problem="names module 'pydantic.', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=model_base_import/fails=module-keyword",
        options={"model_base_import": "None:BaseModel"},
        option="model_base_import",
        value="None:BaseModel",
        problem="names module 'None', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=model_base_import/fails=name-whitespace",
        options={"model_base_import": "pydantic:BaseModel "},
        option="model_base_import",
        value="pydantic:BaseModel ",
        problem="names 'BaseModel ', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=model_base_import/fails=module-whitespace",
        options={"model_base_import": " pydantic:BaseModel"},
        option="model_base_import",
        value=" pydantic:BaseModel",
        problem="names module ' pydantic', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=scalars/fails=module-leading-digit",
        options={"scalars": {"Stamp": "1time:Stamp"}},
        option="scalars['Stamp']",
        value="1time:Stamp",
        problem="names module '1time', which is not a dotted Python name",
    ),
    MalformedImportOption(
        id="option=scalars/fails=name-empty-segment",
        options={"scalars": {"Stamp": "builtins:str."}},
        option="scalars['Stamp']",
        value="builtins:str.",
        problem="names 'str.', which is not a dotted Python name",
    ),
)


@dataclass(frozen=True, kw_only=True)
class AcceptedImportOption:
    id: str
    options: RenderOptions


ACCEPTED_IMPORT_OPTIONS: tuple[AcceptedImportOption, ...] = (
    AcceptedImportOption(
        id="option=base_url_import/form=dotted-name",
        options={"base_url_import": "sample_app.settings:CONFIG.url"},
    ),
    AcceptedImportOption(
        id="option=to_camel_fn_full_name/form=soft-keyword-module",
        options={"to_camel_fn_full_name": "sample_app.type:to_camel"},
    ),
    AcceptedImportOption(
        id="option=model_base_import/form=dotted-module",
        options={"model_base_import": "pydantic.main:BaseModel"},
    ),
    AcceptedImportOption(
        id="option=scalars/form=builtins",
        options={"scalars": {"ID": "builtins:str"}},
    ),
)
