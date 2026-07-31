from tests.generated.runtime_upload_content_type.gql.api import api_gql

upload = api_gql(
    """
    mutation Upload($file: Upload!) {
        uploadFile(file: $file)
    }
    """
)
