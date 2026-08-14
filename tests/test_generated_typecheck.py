import textwrap
from pathlib import Path

from tests.conftest import BasedPyrightReport
from tests.conftest import Diagnostic
from tests.conftest import basedpyright_report
from tests.conftest import readme_fenced_blocks
from tests.conftest import write_text

GENERATED = Path(__file__).parent / "generated"

CHECK_SOURCES = {
    "check_overlapping_tuples.py": """
        from tests.generated.bindings_shapes import queries

        with_link = queries.get_attachment.bind(
            attachment=(queries.image_parts, queries.link_parts)
        )
        reveal_type(with_link)
        with_other = queries.get_attachment.bind(
            attachment=(queries.image_parts, queries.other_parts)
        )
        reveal_type(with_other)
    """,
    "check_bind_spellings.py": """
        from tests.generated.bindings_shapes import queries

        omitted = queries.get_attachment.bind(attachment=queries.image_parts)
        reveal_type(omitted)

        one_fragment = queries.get_attachment.bind(
            attachment=queries.image_parts, preview=queries.image_parts
        )
        reveal_type(one_fragment)

        several = queries.get_attachment.bind(
            preview=(queries.other_parts, queries.link_parts)
        )
        reveal_type(several)

        explicit_empty = queries.get_attachment.bind(
            attachment=queries.image_parts, preview=[]
        )
        reveal_type(explicit_empty)

        bare_call = queries.get_attachment.bind()
        reveal_type(bare_call)

        pair_tuple = queries.get_attachment.bind(
            attachment=(queries.image_parts, queries.link_parts)
        )
        reveal_type(pair_tuple)
    """,
    "check_one_element_list.py": """
        from tests.generated.enumeration import queries

        bare = queries.get_attachment.bind(attachment=queries.image_parts)
        reveal_type(bare)
        listed = queries.get_attachment.bind(attachment=(queries.image_parts,))
    """,
    "check_catch_all.py": """
        from tests.generated.bindings_shapes.gql.api import api_gql


        def dynamic(text: str) -> None:
            reveal_type(api_gql(text))
    """,
    "check_unwritten_bind.py": """
        from tests.generated.bindings_shapes import queries

        unwritten = queries.get_attachment.bind(
            attachment=queries.image_parts,
            preview=(queries.other_parts, queries.link_parts),
        )
        reveal_type(unwritten)
    """,
    "check_tuple_scope_leak.py": """
        from tests.generated.bindings_tuple_scope import queries

        leaked = queries.get_page_attachment.bind(
            attachment=(queries.image_url, queries.link_url)
        )
    """,
    "check_tuple_scope_own.py": """
        from tests.generated.bindings_tuple_scope import queries

        reveal_type(
            queries.get_post_attachment.bind(
                attachment=(queries.image_url, queries.link_url)
            )
        )
    """,
    "check_sub_tuple.py": """
        from tests.generated.bindings_tuple_scope import queries

        narrowed = queries.get_post_attachment.bind(
            attachment=(queries.image_url,)
        )
        listed = queries.get_post_attachment.bind(
            attachment=[queries.image_url, queries.link_url]
        )
    """,
    "check_tuple_order.py": """
        from tests.generated.bindings_tuple_scope import queries

        swapped = queries.get_post_attachment.bind(
            attachment=(queries.link_url, queries.image_url)
        )
        reveal_type(swapped)
    """,
    "check_tuple_repeat.py": """
        from tests.generated.bindings_tuple_scope import queries

        repeated = queries.get_post_attachment.bind(
            attachment=(queries.image_url, queries.image_url)
        )
    """,
    "check_slots.py": """
        from tests.generated.slots_multi import queries


        async def main() -> None:
            bad = queries.list_posts.bind(
                attachment=queries.album_title,
                preview=queries.album_cover,
                owner=queries.album_summary,
            )
            result = await queries.list_posts_typed.execute()
            post = result.posts[0]
            title = queries.album_title.read(post.attachment)
            reveal_type(title)
            reveal_type(queries.album_summary)
    """,
    "check_fragment_definitions.py": """
        from tests.generated.bindings_overlap.gql import api as plain_api
        from tests.generated.fragment_factory_typing.gql import api as factory_api

        _plain = plain_api.ImageCaption()
        _factory = factory_api.SizedImage()
    """,
    "check_defaulted_fragment_variables.py": """
        from tests.generated.bindings_fragment_var_nullability import queries


        queries.image_parts.with_args(width=1)
        queries.image_parts.with_args(width=1, height=20)
        queries.image_parts.with_args(width=1, pad=None)
        queries.image_parts.with_args(width=1, slots=20)
        queries.image_parts.with_args(width=1, height=None)
    """,
    "check_on_type_constructor.py": """
        from typing import Any

        import pydantic

        from tests.generated.fragment_factory_typing.gql import api


        _forged = api.OnImageAttachment[api.SizedImageData, Any](
            fragment_name="SizedImage",
            definition_type=api.SizedImage,
            adapter=pydantic.TypeAdapter(api.SizedImageData),
        )
    """,
    "check_private_constructors.py": """
        from tests.generated.fragment_factory_typing.gql import api


        _direct_applied = api._SizedImageApplied(width=1)
        _empty_bound = api.GetAttachmentBound[api.GetAttachmentResult]()
    """,
    "check_generic_bind.py": """
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
    "check_fragment_factories.py": """
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
    "check_slot_phantom.py": """
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
}


def _write_check_files(check_dir: Path) -> dict[str, Path]:
    (generic_block,) = [
        block for block in readme_fenced_blocks() if "GetPostAttachmentBound[" in block
    ]
    (parameter_block,) = [
        block for block in readme_fenced_blocks() if "OnImageAttachment[" in block
    ]
    readme_sources = {
        "check_readme_generic.py": "\n".join([
            "import pydantic",
            "",
            "from tests.generated.readme_fragment_slots.gql.api import (",
            "    GetPostAttachmentBound,",
            ")",
            "from tests.generated.readme_fragment_slots.queries import (",
            "    get_post_attachment_image,",
            "    image_url,",
            ")",
            "",
            "",
            "async def readme_usage() -> None:",
            textwrap.indent(generic_block, "    "),
            "    _ = (result, image)",
            "",
        ]),
        "check_readme_parameter_helper.py": f"""\
import pydantic

from tests.generated.readme_fragment_slots.gql.api import OnImageAttachment
from tests.generated.readme_fragment_slots.queries import (
    get_post_attachment,
    image_caption,
)


{parameter_block}


async def readme_usage() -> None:
    caption = await attachment_of("p-1", image_caption)
    reveal_type(caption)
""",
    }
    paths: dict[str, Path] = {}
    for name, source in (CHECK_SOURCES | readme_sources).items():
        check_file = check_dir / name
        write_text(check_file, source)
        paths[name] = check_file
    return paths


def _diagnostics(report: BasedPyrightReport, check_file: Path) -> list[Diagnostic]:
    resolved = check_file.resolve()
    return [
        diagnostic
        for diagnostic in report.general_diagnostics
        if diagnostic.file == resolved
    ]


def _errors(report: BasedPyrightReport, check_file: Path) -> list[Diagnostic]:
    return [
        diagnostic
        for diagnostic in _diagnostics(report, check_file)
        if diagnostic.severity == "error"
    ]


def test_generated_and_scratch_typing_contracts(tmp_path: Path):
    check_paths = _write_check_files(tmp_path)
    report = basedpyright_report(GENERATED, tmp_path)

    errors = [
        diagnostic
        for diagnostic in report.general_diagnostics
        if diagnostic.severity == "error"
        and diagnostic.file.is_relative_to(GENERATED.resolve())
    ]
    assert errors == [], "\n".join(
        f"{diagnostic.file}:{diagnostic.range.start.line + 1}: {diagnostic.message}"
        for diagnostic in errors
    )
    assert report.summary.files_analyzed >= (
        2 * len(list(GENERATED.glob("*/gql/api.py"))) + len(check_paths)
    )

    diagnostics = _diagnostics(report, check_paths["check_overlapping_tuples.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    assert errors == [], f"неожиданные ошибки: {errors}"
    infos = [item for item in diagnostics if item.severity == "information"]
    bound_type = "GetAttachmentBound[GetAttachmentResult["
    assert [info.message for info in infos] == [
        f'Type of "with_link" is "{bound_type}ImageParts | LinkParts, Never]]"',
        f'Type of "with_other" is "{bound_type}ImageParts | OtherParts, Never]]"',
    ]

    diagnostics = _diagnostics(report, check_paths["check_bind_spellings.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    assert errors == [], f"неожиданные ошибки: {errors}"
    infos = [item for item in diagnostics if item.severity == "information"]
    image_type = "OnImageAttachment[ImagePartsData, ImageParts] | ImageParts"
    assert [info.message for info in infos] == [
        f'Type of "omitted" is "{bound_type}{image_type}, Never]]"',
        f'Type of "one_fragment" is "{bound_type}{image_type}, {image_type}]]"',
        f'Type of "several" is "{bound_type}Never, OtherParts | LinkParts]]"',
        f'Type of "explicit_empty" is "{bound_type}{image_type}, Never]]"',
        f'Type of "bare_call" is "{bound_type}Never, Never]]"',
        f'Type of "pair_tuple" is "{bound_type}ImageParts | LinkParts, Never]]"',
    ]

    diagnostics = _diagnostics(report, check_paths["check_one_element_list.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    errors = [item for item in errors if item.rule == "reportCallIssue"]
    assert len(errors) == 1, f"ожидалась одна ошибка вызова: {diagnostics}"
    assert errors[0].range.start.line == 4, errors
    [info] = [item for item in diagnostics if item.severity == "information"]
    assert info.message == (
        'Type of "bare" is "GetAttachmentBound[GetAttachmentResult['
        'OnImageAttachment[ImagePartsData, ImageParts] | ImageParts]]"'
    )

    diagnostics = _diagnostics(report, check_paths["check_catch_all.py"])
    assert [item for item in diagnostics if item.severity == "error"] == []
    [info] = [item for item in diagnostics if item.severity == "information"]
    assert info.message == (
        'Type of "api_gql(text)" is "GQLOperation | GQLFragment[BaseModel, Any] '
        '| GQLTemplate"'
    )

    diagnostics = _diagnostics(report, check_paths["check_unwritten_bind.py"])
    assert [item for item in diagnostics if item.severity == "error"] == []
    [info] = [item for item in diagnostics if item.severity == "information"]
    assert info.message == (
        'Type of "unwritten" is "GetAttachmentBound[GetAttachmentResult['
        "OnImageAttachment[ImagePartsData, ImageParts] | ImageParts, "
        'OtherParts | LinkParts]]"'
    )

    assert _errors(report, check_paths["check_tuple_scope_leak.py"]) != [], (
        "чужая tuple-форма протекла в GetPageAttachment"
    )
    assert _errors(report, check_paths["check_tuple_scope_own.py"]) == []

    errors = _errors(report, check_paths["check_sub_tuple.py"])
    assert {
        error.range.start.line for error in errors if error.rule == "reportCallIssue"
    } == {2, 5}, f"отклонены не те вызовы: {errors}"
    assert _errors(report, check_paths["check_tuple_order.py"]) == []
    assert _errors(report, check_paths["check_tuple_repeat.py"]) == []

    diagnostics = _diagnostics(report, check_paths["check_slots.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    errors = [item for item in errors if item.rule == "reportArgumentType"]
    assert len(errors) == 1, f"ожидалась одна ошибка типа аргумента: {diagnostics}"
    assert errors[0].range.start.line == 7, errors[0]
    assert "AlbumSummary" in errors[0].message, errors[0].message
    assert "OnOwner" in errors[0].message, errors[0].message
    infos = [item for item in diagnostics if item.severity == "information"]
    assert len(infos) == 2, f"ожидались два reveal_type: {diagnostics}"
    assert infos[0].message == 'Type of "title" is "AlbumTitleData | None"'
    assert infos[1].message == ('Type of "queries.album_summary" is "AlbumSummary"')

    assert _errors(report, check_paths["check_readme_generic.py"]) == []
    diagnostics = _diagnostics(report, check_paths["check_readme_parameter_helper.py"])
    assert [item for item in diagnostics if item.severity == "error"] == []
    infos = [item.message for item in diagnostics if item.severity == "information"]
    assert infos == ['Type of "caption" is "ImageCaptionData | None"'], infos

    diagnostics = _diagnostics(report, check_paths["check_slot_phantom.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    assert len(errors) == 4, f"ожидались четыре ошибки: {diagnostics}"
    assert [item.range.start.line for item in errors] == [30, 31, 34, 45], errors
    infos = [item.message for item in diagnostics if item.severity == "information"]
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

    diagnostics = _diagnostics(report, check_paths["check_generic_bind.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    assert len(errors) == 2, f"ожидались две ошибки: {diagnostics}"
    assert [item.range.start.line for item in errors] == [18, 41], errors
    infos = [item.message for item in diagnostics if item.severity == "information"]
    assert infos == [
        'Type of "own" is "ImagePartsData | None"',
        'Type of "mine" is "ImagePartsData | None"',
        'Type of "brick" is "BasePartsData | None"',
    ]

    assert _errors(report, check_paths["check_fragment_definitions.py"]) == []
    diagnostics = _diagnostics(report, check_paths["check_fragment_factories.py"])
    errors = [item for item in diagnostics if item.severity == "error"]
    assert sorted({item.range.start.line for item in errors}) == [4, 5, 6, 7, 10, 11]
    assert any(
        "SizedImage" in item.message for item in errors if item.range.start.line == 4
    )
    assert any("width" in item.message for item in errors if item.range.start.line == 5)
    assert any("width" in item.message for item in errors if item.range.start.line == 6)
    assert any("id" in item.message for item in errors if item.range.start.line == 7)
    assert any(
        "_set_fragment_args" in item.message
        for item in errors
        if item.range.start.line == 11
    )
    infos = [item.message for item in diagnostics if item.severity == "information"]
    assert infos == [
        'Type of "ok" is "SizedImageData | None"',
        'Type of "definition_read" is "SizedImageData | None"',
        'Type of "other_read" is "SizedImageData | None"',
        'Type of "transitive" is "SizedImageData | None"',
    ]

    errors = _errors(report, check_paths["check_defaulted_fragment_variables.py"])
    assert len(errors) == 1, errors
    assert errors[0].range.start.line == 7, errors
    assert "None" in errors[0].message
    assert "int" in errors[0].message

    errors = _errors(report, check_paths["check_on_type_constructor.py"])
    assert len(errors) == 1, errors
    assert errors[0].range.start.line == 7, errors
    assert errors[0].rule == "reportAbstractUsage"

    errors = _errors(report, check_paths["check_private_constructors.py"])
    assert sorted({error.range.start.line for error in errors}) == [3, 4]
    assert errors[0].rule == "reportPrivateUsage"
