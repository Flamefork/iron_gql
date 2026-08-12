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
# the source level (it resolves to the attachment-only combination,
# exactly what omitting `preview` also produces -- see
# test_zero_kwarg_and_partial_bindings_resolve_at_runtime and
# test_bind_with_explicit_empty_list_matches_omitted_slot below), not
# just at a runtime call site outside the discovered source.
partial = get_attachment.bind(attachment=image_parts, preview=[])
bare = get_attachment.bind()
# Both slots filled in one call, each through its own on-type base: the
# slots pick their forms independently, so the product of the per-slot
# forms already covers this and no overload of its own is needed.
both = get_attachment.bind(attachment=image_parts, preview=image_parts)
# "slot given several": a tuple of two disjoint fragments -- the one shape
# the enumeration does not produce, so a literal bind still has to.
several = get_attachment.bind(preview=(other_parts, link_parts))
# `image_parts` is already bound alone to `attachment` above (`partial`,
# `both`); here it also sits inside a tuple bind of that same slot,
# alongside a disjoint fragment -- the "registry plus one reader" shape.
image_and_link = get_attachment.bind(attachment=(image_parts, link_parts))
# A second tuple bind of the *same* slot, sharing a fragment with the
# first: two combinations of one arity, overlapping. Written here because
# a form per combination then admits `(image_parts, image_parts)` twice
# over, each returning its own phantom, which is `reportOverlappingOverload`
# on the generated module -- see
# test_two_tuple_binds_of_one_slot_share_a_form.
image_and_other = get_attachment.bind(attachment=(image_parts, other_parts))
# Другая arity означает другой overload namespace. Первые два positional
# parameters намеренно повторяют TFillAttachment1/2; generated module и этот
# call site проходят BasedPyright, закрепляя разделение на public typing
# boundary.
all_three = get_attachment.bind(attachment=(image_parts, other_parts, link_parts))
