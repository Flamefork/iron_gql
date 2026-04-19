import dataclasses

from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import make_optional
from iron_gql.codegen.ir import referenced_names
from iron_gql.codegen.ir import rename_type
from iron_gql.codegen.ir import render_type_expr
from iron_gql.codegen.naming import type_tokens


def test_render_scalar():
    assert render_type_expr(ScalarRef(expr="str")) == "str"


def test_render_scalar_nullable():
    assert render_type_expr(ScalarRef(expr="int", nullable=True)) == "int | None"


def test_render_named():
    assert render_type_expr(NamedRef(name="User")) == "User"


def test_render_list():
    assert render_type_expr(ListRef(element=ScalarRef(expr="int"))) == "list[int]"


def test_render_nullable_list_of_named():
    typ = ListRef(element=NamedRef(name="User"), nullable=True)
    assert render_type_expr(typ) == "list[User] | None"


def test_render_nested_list():
    typ = ListRef(element=ListRef(element=NamedRef(name="User")))
    assert render_type_expr(typ) == "list[list[User]]"


def test_make_optional_wraps_non_nullable():
    assert render_type_expr(make_optional(ScalarRef(expr="int"))) == "int | None"


def test_make_optional_is_idempotent():
    once = make_optional(ScalarRef(expr="int"))
    assert make_optional(once) is once


def test_referenced_names_scalar():
    assert list(referenced_names(ScalarRef(expr="str"))) == []


def test_referenced_names_named():
    assert list(referenced_names(NamedRef(name="User"))) == ["User"]


def test_referenced_names_through_list():
    typ = ListRef(element=ListRef(element=NamedRef(name="User")))
    assert list(referenced_names(typ)) == ["User"]


def test_type_tokens_scalar_no_hint():
    assert list(type_tokens(ScalarRef(expr="int"))) == []


def test_type_tokens_scalar_with_hint():
    assert list(type_tokens(ScalarRef(expr="object", name_hint="Object"))) == ["Object"]


def test_type_tokens_named_pascal():
    assert list(type_tokens(NamedRef(name="user_profile"))) == ["UserProfile"]


def test_type_tokens_list_of_named():
    typ = ListRef(element=ListRef(element=NamedRef(name="User")))
    assert list(type_tokens(typ)) == ["List", "List", "User"]


def test_rename_type_named_replaces_name():
    renamed = rename_type(NamedRef(name="User"), {"User": "UserV2"})
    assert renamed == NamedRef(name="UserV2")


def test_rename_type_named_not_in_map_unchanged():
    typ = NamedRef(name="User")
    assert rename_type(typ, {"Other": "Foo"}) is typ


def test_rename_type_preserves_nullable():
    renamed = rename_type(NamedRef(name="User", nullable=True), {"User": "UserV2"})
    assert renamed == NamedRef(name="UserV2", nullable=True)


def test_rename_type_descends_into_list():
    typ = ListRef(element=ListRef(element=NamedRef(name="User")))
    expected = ListRef(element=ListRef(element=NamedRef(name="UserV2")))
    assert rename_type(typ, {"User": "UserV2"}) == expected


def test_rename_type_leaves_scalar_alone():
    typ = ScalarRef(expr="SuperUser", name_hint="SuperUser")
    assert rename_type(typ, {"SuperUser": "NewUser"}) is typ


def test_dataclasses_replace_toggles_nullable():
    typ = NamedRef(name="User")
    assert dataclasses.replace(typ, nullable=True) == NamedRef(
        name="User", nullable=True
    )
