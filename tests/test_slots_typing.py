from typing import Any

from tests.conftest import generated_package

# Отдельный fixture для typing-контрактов factory: один обязательный fragment
# variable (`SizedImage`) и один обычный fragment (`Wrapper`), который включает
# его через root spread. Поэтому `Wrapper` тоже становится factory, а projection
# можно прочитать транзитивно через независимо созданный definition этой factory.
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
