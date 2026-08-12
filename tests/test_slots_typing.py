from pathlib import Path
from typing import Any

from tests.conftest import basedpyright_errors
from tests.conftest import basedpyright_report
from tests.conftest import generated_package
from tests.conftest import write_text

# Отдельный fixture для typing-контрактов factory: один required fragment
# variable (`SizedImage`) и один plain fragment (`Wrapper`), который root-spread
# его. Поэтому `Wrapper` тоже становится factory, а brick можно прочитать
# транзитивно через independently created definition этой factory.
FRAGMENT_FACTORY_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    caption: String!
    thumbnail(width: Int!): String!
}

type LinkAttachment {
    href: String!
}
"""

FRAGMENT_FACTORY_QUERIES = '''
from tests.generated.fragment_factory_typing.gql.api import api_gql

get_attachment = api_gql("""
    query GetAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")

sized_image = api_gql("""
    fragment SizedImage on ImageAttachment {
        thumbnail(width: $width)
    }
""")

image_caption = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")

wrapper = api_gql("""
    fragment Wrapper on ImageAttachment {
        caption
        ...SizedImage
    }
""")

tuple_bound = get_attachment.bind(
    attachment=(image_caption, sized_image.with_args(width=64))
)
'''

generated_package(
    "fragment_factory_typing",
    schema=FRAGMENT_FACTORY_SCHEMA,
    queries=FRAGMENT_FACTORY_QUERIES,
)

from tests.generated.fragment_factory_typing import queries as factory_queries
from tests.generated.fragment_factory_typing.gql import api as factory_api


def test_independent_factory_definition_reads_transitive_projection():
    wrapped = factory_queries.wrapper.with_args(width=32)
    bound = factory_queries.get_attachment.bind(attachment=wrapped)
    result = factory_api.GetAttachmentResult[Any].model_validate(
        {
            "post": {
                "id": "p-1",
                "attachment": {
                    "__typename": "ImageAttachment",
                    "caption": "cover",
                    "thumbnail": "thumb-32",
                },
            }
        },
        context=bound.slot_readers,
    )
    assert result.post is not None
    independent = factory_api.SizedImage()
    assert independent is not factory_queries.sized_image
    image = independent.read(result.post.attachment)
    assert image is not None
    assert image.thumbnail == "thumb-32"


def test_factory_definition_and_other_application_read_tuple_projection():
    result = factory_api.GetAttachmentResult[Any].model_validate(
        {
            "post": {
                "id": "p-1",
                "attachment": {
                    "__typename": "ImageAttachment",
                    "caption": "cover",
                    "thumbnail": "thumb-64",
                },
            }
        },
        context=factory_queries.tuple_bound.slot_readers,
    )
    assert result.post is not None
    definition = factory_api.SizedImage()
    other_application = definition.with_args(width=128)
    from_definition = definition.read(result.post.attachment)
    from_other_application = other_application.read(result.post.attachment)
    assert from_definition is not None
    assert from_other_application is not None
    assert from_definition.thumbnail == "thumb-64"
    assert from_other_application.thumbnail == "thumb-64"


def test_phantom_typing_of_slot_reads(tmp_path: Path):
    # The phantom mirrors runtime readability one to one: a fragment reads
    # exactly the nodes of bindings that offered it; an unfilled slot (phantom
    # Never) is statically unreadable. `slots_multi` carries most of the
    # shapes -- `list_posts_typed` offers AlbumTitle to `attachment` and
    # AlbumCover to `preview` (so AlbumCover is a readable definition of that binding,
    # just not of that slot) while never offering LinkHref at all,
    # `list_posts_bare` fills nothing, and `list_posts_dual` offers two
    # fragments to one slot. `bindings_closure_shape` carries the last one:
    # ThumbAlt is spread inside a *field* of a bound fragment, so its data
    # never lands on the slot's own payload and it stays out of the offered
    # set (the phantom there is `ImageParts | LinkParts | NodeId`) -- the
    # README's nested-field paragraph, statically.
    #
    # The reading contract's other half needs concrete definitions known in one
    # scope, so it draws on three more packages: `bindings_overlap` accepts a
    # loop over a literal tuple of two concrete definitions bound to the same
    # slot (the loop variable types as their union, and each still reads its
    # own slice) but rejects a definition already erased to the base
    # `GQLFragment[BaseModel, Any]` -- the README's type-erased-path paragraph,
    # statically; `bindings_composition` accepts a read of `BaseParts`, never
    # itself passed to `bind()` but reachable through `ImageParts`'s spread of
    # it, through its own definition (an indirect, root-spread-reached read);
    # `bindings_two_templates` accepts the same fragment definition reading the
    # results of the two different templates it was bound into (a
    # cross-template read). The scratch file lives under tmp_path, outside
    # the repo tree, so `just lint`'s whole-project basedpyright run never
    # picks it up.
    check_file = tmp_path / "check_slot_phantom.py"
    write_text(
        check_file,
        """
            from typing import Any

            import pydantic

            from iron_gql import slots
            from tests.generated.bindings_closure_shape import queries as closure_gql
            from tests.generated.bindings_composition import queries as composition_gql
            from tests.generated.bindings_overlap import queries as overlap_gql
            from tests.generated.bindings_overlap.gql.api import (
                GetAttachmentResult,
                ImageCaption,
                ImageSize,
            )
            from tests.generated.bindings_two_templates import queries as two_tpl_gql
            from tests.generated.slots_multi import queries


            def use_erased(
                fragment: slots.GQLFragment[pydantic.BaseModel, Any],
                result: GetAttachmentResult[ImageCaption | ImageSize],
            ) -> None:
                assert result.post is not None
                _err_type_erased = fragment.read(result.post.attachment)


            async def main() -> None:
                result = await queries.list_posts_typed.execute()
                post = result.posts[0]
                ok = queries.album_title.read(post.attachment)
                reveal_type(ok)
                _err_other_slot = queries.album_cover.read(post.attachment)
                _err_never_bound = queries.link_href.read(post.attachment)

                bare = await queries.list_posts_bare.execute()
                _err_unfilled = queries.album_title.read(bare.posts[0].attachment)

                dual = await queries.list_posts_dual.execute()
                dual_post = dual.posts[0]
                ok_a = queries.link_href.read(dual_post.attachment)
                ok_b = queries.album_cover.read(dual_post.attachment)
                reveal_type(ok_a)
                reveal_type(ok_b)

                nested = await closure_gql.bound.execute(id="1")
                assert nested.post is not None
                _err_nested = closure_gql.thumb_alt.read(nested.post.attachment)

                overlap_result = await overlap_gql.both.execute(id="p-1")
                assert overlap_result.post is not None
                overlap_node = overlap_result.post.attachment
                for frag in (overlap_gql.image_caption, overlap_gql.image_size):
                    overlap_read = frag.read(overlap_node)
                    reveal_type(overlap_read)

                use_erased(overlap_gql.image_caption, overlap_result)

                composed = await composition_gql.bound.execute(id="1")
                assert composed.post is not None
                indirect = composition_gql.base_parts.read(composed.post.attachment)
                reveal_type(indirect)

                attachment_result = await two_tpl_gql.bound_attachment.execute(
                    id="1"
                )
                highlight_result = await two_tpl_gql.bound_highlight.execute(
                    id="1"
                )
                assert attachment_result.post is not None
                assert highlight_result.post is not None
                cross_a = two_tpl_gql.image_parts.read(
                    attachment_result.post.attachment
                )
                cross_b = two_tpl_gql.image_parts.read(
                    highlight_result.post.highlight
                )
                reveal_type(cross_a)
                reveal_type(cross_b)

                shared = read_any_binding(
                    overlap_result, overlap_gql.image_caption
                )
                reveal_type(shared)


            def read_any_binding[TData: pydantic.BaseModel](
                result: GetAttachmentResult[Any],
                fragment: slots.GQLFragment[TData, Any],
            ) -> TData | None:
                assert result.post is not None
                return fragment.read(result.post.attachment)
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics

    # Four rejections, each on its own line, inside `main`: a fragment reading
    # another slot of its own binding, one the
    # binding never offered anywhere, a read of a slot left unfilled, and one
    # reached only through a nested field of a bound fragment.
    errors = [d for d in diagnostics if d.severity == "error"]
    assert len(errors) == 4, f"expected exactly four errors, got: {diagnostics}"
    assert [d.range.start.line for d in errors] == [30, 31, 34, 45], errors

    # And the reads that are offered keep their own fragment's model -- the
    # phantom types the node, never the value `read` hands back. This also
    # covers overlap (two definitions, one loop, one read each), an indirect
    # spread-only definition, the same definition read across two templates, and the
    # shared-helper shape: a binding's result passed to a helper that spells
    # the phantom `Any`, where the ordinary `read` keeps the definition's own
    # model -- the erasure is the annotation, not a second method.
    infos = [d.message for d in diagnostics if d.severity == "information"]
    assert infos == [
        'Type of "ok" is "AlbumTitleData | None"',
        'Type of "ok_a" is "LinkHrefData | None"',
        'Type of "ok_b" is "AlbumCoverData | None"',
        'Type of "overlap_read" is "ImageCaptionData | ImageSizeData | None"',
        'Type of "indirect" is "BasePartsData | None"',
        'Type of "cross_a" is "ImagePartsData | None"',
        'Type of "cross_b" is "ImagePartsData | None"',
        'Type of "shared" is "ImageCaptionData | None"',
    ]


def test_typing_of_the_generic_bind_path(tmp_path: Path):
    # `bind()` принимает on-type base, поэтому template остаётся приватным для
    # своего модуля, а fragment приходит параметром. Проверяются четыре случая
    # на `bindings_composition`: `ImageParts` spread-ит `BaseParts`, а
    # `ForeignParts` — другой fragment на том же `ImageAttachment` со структурно
    # одинаковым model, поэтому различить их может только nominal identity:
    #
    # - reader helper (`read_details`) bind-ит и читает внутри себя, возвращает
    #   model вызывающей стороны и запрещает read другим fragment того же type;
    # - конкретный call site через generic `bind` получает в `own` model
    #   вызывающей стороны, а не `Any` и не union;
    # - forwarding helper (`forward`) переносит class и closure, поэтому
    #   transitive brick читается через его result;
    # - foreign fragment того же type со структурно одинаковым model не может
    #   прочитать этот result.
    #
    # Случай fragment, чей on-type не принимает ни один slot, закреплён в
    # `tests/test_fragment_handles.py`. Scratch file лежит в `tmp_path` вне
    # дерева репозитория и не попадает в общий запуск BasedPyright.
    check_file = tmp_path / "check_generic_bind.py"
    write_text(
        check_file,
        """
            import pydantic

            from tests.generated.bindings_composition import queries
            from tests.generated.bindings_composition.gql.api import (
                GetAttachmentResult,
            )
            from tests.generated.bindings_composition.gql.api import (
                OnImageAttachment,
            )


            async def read_details[TModel: pydantic.BaseModel, TReads](
                details: OnImageAttachment[TModel, TReads],
            ) -> TModel | None:
                bound = queries.get_attachment.bind(attachment=details)
                result = await bound.execute(id="1")
                assert result.post is not None
                _err_other_fragment = queries.base_parts.read(
                    result.post.attachment
                )
                return details.read(result.post.attachment)


            async def forward[TModel: pydantic.BaseModel, TReads](
                details: OnImageAttachment[TModel, TReads],
            ) -> GetAttachmentResult[OnImageAttachment[TModel, TReads] | TReads]:
                bound = queries.get_attachment.bind(attachment=details)
                return await bound.execute(id="1")


            async def main() -> None:
                own = await read_details(queries.image_parts)
                reveal_type(own)

                forwarded = await forward(queries.image_parts)
                assert forwarded.post is not None
                mine = queries.image_parts.read(forwarded.post.attachment)
                reveal_type(mine)
                brick = queries.base_parts.read(forwarded.post.attachment)
                reveal_type(brick)
                _err_foreign = queries.foreign_parts.read(
                    forwarded.post.attachment
                )
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics

    # Two rejections, in file order: inside the reader helper, a read by a
    # fragment other than the one the caller handed it; and, on the result
    # that leaked out of the forwarding helper, a read by a foreign fragment
    # of the same on-type whose model has the very same fields.
    errors = [d for d in diagnostics if d.severity == "error"]
    assert len(errors) == 2, f"expected exactly two errors, got: {diagnostics}"
    assert [d.range.start.line for d in errors] == [18, 41], errors

    infos = [d.message for d in diagnostics if d.severity == "information"]
    assert infos == [
        'Type of "own" is "ImagePartsData | None"',
        'Type of "mine" is "ImagePartsData | None"',
        'Type of "brick" is "BasePartsData | None"',
    ]


def test_public_fragment_definitions_are_concrete(tmp_path: Path):
    check_file = tmp_path / "check_fragment_definitions.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_overlap.gql import api as plain_api
            from tests.generated.fragment_factory_typing.gql import api as factory_api

            _plain = plain_api.ImageCaption()
            _factory = factory_api.SizedImage()
        """,
    )
    assert basedpyright_errors(check_file) == []


def test_typing_of_fragment_factories(tmp_path: Path):
    # У factory нет on-type base, поэтому `bind()` отвергает её напрямую;
    # `with_args` без required argument или с неверным type даёт static error
    # у владельца fragment. Возвращённая application bind-ится и читает
    # собственную model. Definition той же factory и другая её application
    # читают ту же projection. Транзитивно достигнутый `SizedImage` также
    # читается через независимо созданный definition.
    check_file = tmp_path / "check_fragment_factories.py"
    write_text(
        check_file,
        """
            from tests.generated.fragment_factory_typing import queries


            async def main() -> None:
                queries.get_attachment.bind(attachment=queries.sized_image)
                queries.sized_image.with_args()
                queries.sized_image.with_args(width="oops")
                queries.sized_image.with_args(width=64, id="injected")

                applied = queries.sized_image.with_args(width=64)
                applied.fragment_args__["id"] = "injected"
                applied._set_fragment_args({"id": "injected"})
                bound = queries.get_attachment.bind(attachment=applied)
                result = await bound.execute(id="p-1")
                assert result.post is not None
                ok = applied.read(result.post.attachment)
                reveal_type(ok)
                definition_read = queries.sized_image.read(result.post.attachment)
                reveal_type(definition_read)

                other_application = queries.sized_image.with_args(width=128)
                other_read = other_application.read(result.post.attachment)
                reveal_type(other_read)

                wrapped = queries.wrapper.with_args(width=32)
                wrapped_bound = queries.get_attachment.bind(attachment=wrapped)
                wrapped_result = await wrapped_bound.execute(id="p-1")
                assert wrapped_result.post is not None
                transitive = queries.sized_image.read(wrapped_result.post.attachment)
                reveal_type(transitive)
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics

    # Factory не проходит в `bind`; точный конструктор `with_args` отвергает
    # missing, неверно типизированный и лишний argument; Mapping applied
    # fragment не допускает подмену назначения variable. Последняя ошибка —
    # Definition и любая application одной factory читают один projection.
    errors = [d for d in diagnostics if d.severity == "error"]
    error_lines = sorted({d.range.start.line for d in errors})
    assert error_lines == [4, 5, 6, 7, 10, 11], (
        f"expected lines 4, 5, 6, 7, 10, 11 rejected, got: {diagnostics}"
    )

    def messages_at(line: int) -> list[str]:
        return [d.message for d in errors if d.range.start.line == line]

    assert any("SizedImage" in m for m in messages_at(4))
    assert any("width" in m for m in messages_at(5))
    assert any("width" in m for m in messages_at(6))
    assert any("id" in m for m in messages_at(7))
    assert any("_set_fragment_args" in m for m in messages_at(11))
    infos = [d.message for d in diagnostics if d.severity == "information"]
    assert infos == [
        'Type of "ok" is "SizedImageData | None"',
        'Type of "definition_read" is "SizedImageData | None"',
        'Type of "other_read" is "SizedImageData | None"',
        'Type of "transitive" is "SizedImageData | None"',
    ]


def test_defaulted_non_null_fragment_variable_rejects_explicit_none(
    tmp_path: Path,
):
    generated_api = (
        Path(__file__).parent / "generated/bindings_fragment_var_nullability/gql/api.py"
    )
    assert basedpyright_errors(generated_api) == []

    check_file = tmp_path / "check_defaulted_fragment_variables.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_fragment_var_nullability import queries


            queries.image_parts.with_args(width=1)
            queries.image_parts.with_args(width=1, height=20)
            queries.image_parts.with_args(width=1, pad=None)
            queries.image_parts.with_args(width=1, slots=20)
            queries.image_parts.with_args(width=1, height=None)
        """,
    )

    errors = basedpyright_errors(check_file)
    assert len(errors) == 1, f"expected exactly one error, got: {errors}"
    assert errors[0].range.start.line == 7, errors
    assert "None" in errors[0].message
    assert "int" in errors[0].message


def test_on_type_base_is_not_a_public_constructor(tmp_path: Path):
    check_file = tmp_path / "check_on_type_constructor.py"
    write_text(
        check_file,
        """
            from typing import Any

            import pydantic

            from tests.generated.fragment_factory_typing.gql import api


            _forged = api.OnImageAttachment[api.SizedImageData, Any](
                fragment_name="SizedImage",
                definition_type=api.SizedImage,
                adapter=pydantic.TypeAdapter(api.SizedImageData),
            )
        """,
    )

    errors = basedpyright_errors(check_file)
    assert len(errors) == 1, errors
    assert errors[0].range.start.line == 7, errors
    assert errors[0].rule == "reportAbstractUsage"


def test_applied_constructor_is_private_and_bound_requires_state(tmp_path: Path):
    check_file = tmp_path / "check_private_constructors.py"
    write_text(
        check_file,
        """
            from tests.generated.fragment_factory_typing.gql import api


            _direct_applied = api._SizedImageApplied(width=1)
            _empty_bound = api.GetAttachmentBound[api.GetAttachmentResult]()
        """,
    )
    errors = basedpyright_errors(check_file)
    error_lines = sorted({error.range.start.line for error in errors})
    assert error_lines == [3, 4], errors
    assert errors[0].rule == "reportPrivateUsage"
