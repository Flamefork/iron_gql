from pathlib import Path

from tests.conftest import basedpyright_errors
from tests.conftest import basedpyright_report
from tests.conftest import write_text


def test_phantom_typing_of_slot_reads(tmp_path: Path):
    # The phantom mirrors runtime readability one to one: a fragment reads
    # exactly the nodes of bindings that offered it; an unfilled slot (phantom
    # Never) is statically unreadable. `slots_multi` carries most of the
    # shapes -- `list_posts_typed` offers AlbumTitle to `attachment` and
    # AlbumCover to `preview` (so AlbumCover is a real handle of that binding,
    # just not of that slot) while never offering LinkHref at all,
    # `list_posts_bare` fills nothing, and `list_posts_dual` offers two
    # fragments to one slot. `bindings_closure_shape` carries the last one:
    # ThumbAlt is spread inside a *field* of a bound fragment, so its data
    # never lands on the slot's own payload and it stays out of the offered
    # set (the phantom there is `ImageParts | LinkParts | NodeId`) -- the
    # README's nested-field paragraph, statically.
    #
    # The reading contract's other half needs concrete handles known in one
    # scope, so it draws on three more packages: `bindings_overlap` accepts a
    # loop over a literal tuple of two concrete handles bound to the same
    # slot (the loop variable types as their union, and each still reads its
    # own slice) but rejects a handle already erased to the base
    # `GQLFragment[BaseModel]` -- the README's type-erased-path paragraph,
    # statically; `bindings_composition` accepts a read of `BaseParts`, never
    # itself passed to `bind()` but reachable through `ImageParts`'s spread of
    # it, through its own handle (an indirect, root-spread-reached read);
    # `bindings_two_templates` accepts the same fragment handle reading the
    # results of the two different templates it was bound into (a
    # cross-template read). The scratch file lives under tmp_path, outside
    # the repo tree, so `just lint`'s whole-project basedpyright run never
    # picks it up.
    check_file = tmp_path / "check_slot_phantom.py"
    write_text(
        check_file,
        """
            import pydantic

            from iron_gql import slots
            from tests.generated.bindings_closure_shape import queries as closure_gql
            from tests.generated.bindings_composition import queries as composition_gql
            from tests.generated.bindings_overlap import queries as overlap_gql
            from tests.generated.bindings_overlap.gql import api as overlap_api
            from tests.generated.bindings_two_templates import queries as two_tpl_gql
            from tests.generated.slots_multi import queries


            def use_erased(
                handle: slots.GQLFragment[pydantic.BaseModel],
                node: overlap_api.GetAttachmentResultPostAttachmentSlot[
                    overlap_api.ImageCaption | overlap_api.ImageSize
                ]
                | None,
            ) -> None:
                _err_type_erased = handle.read(node)


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

                use_erased(overlap_gql.image_caption, overlap_node)

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
        """,
    )
    diagnostics = basedpyright_report(check_file).general_diagnostics

    # Five rejections, each on its own line, in file order: `use_erased`'s own
    # body (a handle already erased to `GQLFragment[BaseModel]`), then inside
    # `main` a fragment reading another slot of its own binding, one the
    # binding never offered anywhere, a read of a slot left unfilled, and one
    # reached only through a nested field of a bound fragment.
    errors = [d for d in diagnostics if d.severity == "error"]
    assert len(errors) == 5, f"expected exactly five errors, got: {diagnostics}"
    assert [d.range.start.line for d in errors] == [18, 26, 27, 30, 41], errors

    # And the reads that are offered keep their own fragment's model -- the
    # phantom types the node, never the value `read` hands back. This also
    # covers overlap (two handles, one loop, one read each), an indirect
    # spread-only handle, and the same handle read across two templates.
    infos = [d.message for d in diagnostics if d.severity == "information"]
    assert infos == [
        'Type of "ok" is "AlbumTitleData | None"',
        'Type of "ok_a" is "LinkHrefData | None"',
        'Type of "ok_b" is "AlbumCoverData | None"',
        'Type of "overlap_read" is "ImageCaptionData | ImageSizeData | None"',
        'Type of "indirect" is "BasePartsData | None"',
        'Type of "cross_a" is "ImagePartsData | None"',
        'Type of "cross_b" is "ImagePartsData | None"',
    ]


def test_a_fragment_class_rejects_a_zero_argument_second_handle(tmp_path: Path):
    # A handle is an identity token: `GQLSlotNode._slot_data` is keyed by
    # `id(handle)`, so a second instance of a fragment's class reads nothing
    # of a binding the singleton was bound into. It type-checks everywhere
    # the singleton does, though -- `bind()` keys its dispatch by fragment
    # name and the bound operation carries the singleton's own handles -- so
    # the mistake used to surface only at `read`, as a ValueError claiming
    # the fragment was not part of a binding it plainly was.
    #
    # The generated class declares no constructor of its own, so it inherits
    # the runtime base's required metadata parameters -- and the spelling a
    # call site actually reaches for, `ImageCaption()`, the one a class that
    # looks like a marker type invites, is rejected where it is written.
    # Written out in full it is not: the model and the fragment name are
    # exported from the same generated module, so `api.ImageCaption(
    # fragment_name=..., adapter=...)` type-checks, and the read that follows
    # types as the singleton's own. The static half stops the plausible
    # mistake, not every one; the runtime `ValueError` -- a same-named handle
    # built the long way still reads nothing -- is the line that catches the
    # rest, pinned in tests/test_bindings_runtime.py.
    check_file = tmp_path / "check_second_handle.py"
    write_text(
        check_file,
        """
            import pydantic

            from tests.generated.bindings_overlap.gql import api

            _err_second_handle = api.ImageCaption()
            _ok_written_out = api.ImageCaption(
                fragment_name="ImageCaption",
                adapter=pydantic.TypeAdapter(api.ImageCaptionData),
            )
        """,
    )
    errors = basedpyright_errors(check_file)
    assert len(errors) == 1, f"expected exactly one error, got: {errors}"
    assert errors[0].range.start.line == 4, errors
