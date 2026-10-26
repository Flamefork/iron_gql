"""Consumer bases that set a `model_config` key the library sets itself.

Each sets the key to a value that shows when it gets through: the wire would
not be read, an input would not be built by its field names, a result would not
dump by alias, an unknown key would pass. The library's value has to win for
every one; a key pydantic stores under another name -- `populate_by_name` as
`validate_by_name` -- is a base of its own.
"""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class BaseForm:
    name: str
    # The keyword arguments of the base's `pydantic.ConfigDict`, as source.
    config: str


LIBRARY_KEY_BASES: tuple[BaseForm, ...] = (
    BaseForm(name="alias-upper", config="alias_generator=str.upper"),
    BaseForm(name="extra-allow", config='extra="allow"'),
    BaseForm(name="dump-by-field-name", config="serialize_by_alias=False"),
    BaseForm(name="no-populate-by-name", config="populate_by_name=False"),
    BaseForm(name="no-validate-by-name", config="validate_by_name=False"),
    BaseForm(
        name="no-validate-by-alias",
        config="validate_by_name=True, validate_by_alias=False",
    ),
)
