"""Generator options that name one module twice.

A scalar, the alias function and the base each name a `module:attr`, and the
generated module imports pydantic and the standard modules on its own. Each
form makes two of these meet in one module; the module must still be imported
once. `sample_app.shared` is a module of the test project, written from
`SHARED_MODULE`.
"""

from dataclasses import dataclass

SHARED_MODULE = """
import pydantic
from pydantic import alias_generators

Token = str


class Model(pydantic.BaseModel):
    pass


def to_camel(name: str) -> str:
    return alias_generators.to_camel(name)
"""

_TO_CAMEL = "pydantic.alias_generators:to_camel"
_BASE = "pydantic:BaseModel"


@dataclass(frozen=True, kw_only=True)
class ImportForm:
    name: str
    scalars: dict[str, str]
    to_camel: str = _TO_CAMEL
    model_base: str = _BASE


def _custom(form: ImportForm) -> list[str]:
    # `ID` is a scalar the schema has already; every other one is declared.
    return [name for name in form.scalars if name != "ID"]


def schema(form: ImportForm) -> str:
    # Each mapped scalar is an argument of `ping`, so the generated module
    # writes a type from every module the form names.
    declared = "".join(f"scalar {name}\n" for name in _custom(form))
    arguments = ", ".join(f"{name.lower()}: {name}" for name in form.scalars)
    field = f"ping({arguments})" if arguments else "ping"
    return f"{declared}\ntype Query {{\n    {field}: String\n}}\n"


def queries(form: ImportForm) -> str:
    names = [name.lower() for name in form.scalars]
    declared = ", ".join(f"${name.lower()}: {name}" for name in form.scalars)
    passed = ", ".join(f"{name}: ${name}" for name in names)
    operation = f"Ping({declared})" if declared else "Ping"
    field = f"ping({passed})" if passed else "ping"
    return (
        "from sample_app.gql.api import api_gql\n\n"
        f'ping = api_gql("query {operation} {{ {field} }}")\n'
    )


IMPORT_FORMS: tuple[ImportForm, ...] = (
    ImportForm(
        name="scalars-share-a-module",
        scalars={"ID": "builtins:str", "Blob": "builtins:bytes"},
    ),
    ImportForm(
        name="scalar-from-pydantic",
        scalars={"Secret": "pydantic:SecretStr"},
    ),
    ImportForm(
        name="scalar-from-a-standard-module",
        scalars={"Stamp": "datetime:datetime"},
    ),
    ImportForm(
        name="alias-function-through-pydantic",
        scalars={},
        to_camel="pydantic:alias_generators.to_camel",
    ),
    ImportForm(
        name="scalar-and-alias-function-share-a-module",
        scalars={"Token": "sample_app.shared:Token"},
        to_camel="sample_app.shared:to_camel",
    ),
    ImportForm(
        name="scalar-and-base-share-a-module",
        scalars={"Token": "sample_app.shared:Token"},
        model_base="sample_app.shared:Model",
    ),
)
