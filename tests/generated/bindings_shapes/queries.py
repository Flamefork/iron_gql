from tests.generated.bindings_shapes.gql.api import api_gql

get_attachment = api_gql(
    """
    query GetAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
            preview @slot { __typename }
        }
    }
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url
    }
    """
)

other_parts = api_gql(
    """
    fragment OtherParts on ImageAttachment {
        url
    }
    """
)

link_parts = api_gql(
    """
    fragment LinkParts on LinkAttachment {
        href
    }
    """
)

# `preview=[]` spelled explicitly rather than omitted: pins the rule at
# the source level (it resolves to the attachment-only binding, exactly what
# omitting `preview` also produces -- see
# test_zero_kwarg_and_partial_bindings_resolve_at_runtime and
# test_bind_with_explicit_empty_list_matches_omitted_slot below), not
# just at a runtime call site outside the discovered source.
partial = get_attachment.bind(attachment=image_parts, preview=[])
bare = get_attachment.bind()
# Mixed spelling: `attachment` bare, `preview` as a one-element list --
# each slot picks its own calling convention independently. Pins the bug
# the union-parameter overload fixes: the old two-corner (all-bare,
# all-list) overload pair had no overload this call could match.
both = get_attachment.bind(attachment=image_parts, preview=[image_parts])
# "slot given several": a list of two disjoint fragments.
several = get_attachment.bind(preview=[other_parts, link_parts])
# `image_parts` is already bound alone to `attachment` above (`partial`,
# `both`); here it also sits inside a list bind of that same slot,
# alongside a disjoint fragment -- the "registry plus one reader" shape
# . The old solo/list overlap rejection forbade this
# combination only because the two-corner overload encoding made the
# solo binding's `Sequence[ImageParts]` a subtype of the wider
# `Sequence[ImageParts | LinkParts]`; the union-parameter overload
# doesn't have that shape, so the combination is legal.
image_and_link = get_attachment.bind(attachment=[image_parts, link_parts])
