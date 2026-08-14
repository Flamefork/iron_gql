import pytest

from tests.conftest import generated_package

# Два независимых root field имеют slot с одинаковым именем `attachment`, но
# несовместимые union types. Единственный literal tuple bind принадлежит
# `GetPostAttachment` и не должен добавлять overload в `GetPageAttachment`.
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


def test_a_repeated_fragment_in_a_tuple_reaches_the_lookup_error():
    # Widening позиций tuple допускает `(image_url, image_url)` статически,
    # но runtime громко отвергает отсутствующую combination до запроса.
    with pytest.raises(LookupError, match="unknown bind combination"):
        _ = queries.get_post_attachment.bind(
            attachment=(queries.image_url, queries.image_url)
        )
