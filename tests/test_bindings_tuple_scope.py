"""Finding 1 of the parametric-bind final review: a literal tuple's phantom
must stay scoped to the template it was written on.

`render._literal_tuple_forms` used to key its dedup dict by the slot's own
`python_name` alone, collected across every binding of the whole package, and
`render_templates` applied that one shared dict to each template in turn. Two
templates whose slots share a python name -- an ordinary coincidence, not a
mistake -- then leaked tuple forms into each other even when their slot types
share no fragment: a template that never had a literal tuple bind of its own
grew an overload for someone else's, with no dispatch-table row behind it.
"""

from pathlib import Path

import pytest

from tests.conftest import basedpyright_errors
from tests.conftest import generated_package
from tests.conftest import write_text

# Two independent root fields, each with a slot named "attachment" -- the
# same python keyword -- but of disjoint union types: nothing compatible with
# `Media` is compatible with `Attachment`, and vice versa. `GetPostAttachment`
# gets the package's only literal tuple bind; `GetPageAttachment` gets none of
# its own.
SCHEMA = """
type Query {
    post(id: ID!): Post
    page(id: ID!): Page
}

type Post {
    id: ID!
    attachment: Attachment
}

type Page {
    id: ID!
    attachment: Media
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}

union Media = VideoMedia | AudioMedia

type VideoMedia {
    videoUrl: String!
}

type AudioMedia {
    audioUrl: String!
}
"""

QUERIES = '''
from tests.generated.bindings_tuple_scope.gql.api import api_gql

image_url = api_gql(
    """
    fragment ImageUrl on ImageAttachment {
        url
    }
    """
)

link_url = api_gql(
    """
    fragment LinkUrl on LinkAttachment {
        href
    }
    """
)

get_post_attachment = api_gql(
    """
    query GetPostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

get_page_attachment = api_gql(
    """
    query GetPageAttachment($id: ID!) {
        page(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

# The package's only literal tuple bind, and only on GetPostAttachment: the
# combination it discovers must not become an overload GetPageAttachment
# also carries, since GetPageAttachment's own "attachment" slot cannot spread
# either fragment (it is a Media, not an Attachment).
post_attachment = get_post_attachment.bind(attachment=(image_url, link_url))
'''

generated_package("bindings_tuple_scope", schema=SCHEMA, queries=QUERIES)

from tests.generated.bindings_tuple_scope import queries


def test_a_literal_tuple_bind_stays_scoped_to_its_own_template(tmp_path: Path):
    # GetPageAttachment's "attachment" slot is a Media union that shares no
    # member type with Attachment, so no overload of its `bind()` should ever
    # accept a tuple of ImageUrl/LinkUrl -- there is no fragment compatible
    # with it in this package at all, tuple or otherwise. If this call
    # type-checks clean, GetPostAttachment's own tuple form leaked in under
    # the shared "attachment" keyword, and at runtime the combination is
    # missing from the dispatch table (`LookupError`) because nothing ever
    # discovered it as GetPageAttachment's own.
    check_file = tmp_path / "check_tuple_scope_leak.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_tuple_scope import queries

            leaked = queries.get_page_attachment.bind(
                attachment=(queries.image_url, queries.link_url)
            )
        """,
    )
    errors = basedpyright_errors(check_file)
    assert errors != [], (
        "GetPageAttachment accepted a tuple of ImageUrl/LinkUrl -- "
        "GetPostAttachment's own literal tuple form leaked across templates"
    )


def test_a_literal_tuple_bind_is_still_accepted_by_its_own_template(tmp_path: Path):
    # The positive twin: scoping the dedup by template must not cost
    # GetPostAttachment its own combination, still deduplicated the way the
    # design intends when the same tuple is written more than once.
    check_file = tmp_path / "check_tuple_scope_own.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_tuple_scope import queries

            reveal_type(
                queries.get_post_attachment.bind(
                    attachment=(queries.image_url, queries.link_url)
                )
            )
        """,
    )
    errors = basedpyright_errors(check_file)
    assert errors == [], f"expected no type errors, got: {errors}"


def test_a_sub_tuple_of_a_literal_bind_is_refused(tmp_path: Path):
    # Why the form is a tuple and not a `Sequence`. A sequence fixes no
    # length, so the overload written for `(image_url, link_url)` also
    # admitted `[image_url]` -- and handed it back typed as offering
    # `LinkUrl`, which the one-fragment binding that call really reaches
    # never bound, so `LinkUrl.read(...)` type-checked and raised
    # `ValueError` at runtime. A fixed-length tuple has no sub-list to
    # capture: the one-fragment call has to be written as the bare definition
    # that names its own combination.
    check_file = tmp_path / "check_sub_tuple.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_tuple_scope import queries

            narrowed = queries.get_post_attachment.bind(
                attachment=(queries.image_url,)
            )
            listed = queries.get_post_attachment.bind(
                attachment=[queries.image_url, queries.link_url]
            )
        """,
    )
    errors = basedpyright_errors(check_file)
    refused = {
        error.range.start.line for error in errors if error.rule == "reportCallIssue"
    }
    assert refused == {2, 5}, f"expected both calls refused, got: {errors}"


def test_a_literal_tuple_bind_accepts_either_order(tmp_path: Path):
    # The caller's own order is not the discovered bind's, and the runtime
    # does not care either (`slots.dispatch_key` sorts) -- so each position of the
    # form is widened to the whole combination rather than the orderings being
    # spelled out (`render._tuple_slot_form`).
    check_file = tmp_path / "check_tuple_order.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_tuple_scope import queries

            swapped = queries.get_post_attachment.bind(
                attachment=(queries.link_url, queries.image_url)
            )
            reveal_type(swapped)
        """,
    )
    errors = basedpyright_errors(check_file)
    assert errors == [], f"expected no type errors, got: {errors}"


def test_a_repeated_fragment_in_a_tuple_reaches_the_lookup_error(tmp_path: Path):
    # What widening the positions gives up. `(image_url, image_url)` names no
    # combination -- a slot spreads each of its fragments once -- and the only
    # annotation that would refuse it statically is a union of every ordering,
    # which is factorial in the arity (see `_tuple_slot_form`). So it
    # type-checks and lands on the documented `LookupError` instead: loud, at
    # the call, and never a request that goes out wrong.
    check_file = tmp_path / "check_tuple_repeat.py"
    write_text(
        check_file,
        """
            from tests.generated.bindings_tuple_scope import queries

            repeated = queries.get_post_attachment.bind(
                attachment=(queries.image_url, queries.image_url)
            )
        """,
    )
    assert basedpyright_errors(check_file) == []
    with pytest.raises(LookupError, match="unknown bind combination"):
        _ = queries.get_post_attachment.bind(
            attachment=(queries.image_url, queries.image_url)
        )
