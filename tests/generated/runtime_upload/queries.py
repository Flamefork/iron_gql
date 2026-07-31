from tests.generated.runtime_upload.gql.api import api_gql

upload_file = api_gql(
    """
    mutation UploadFile($file: Upload!, $label: String!) {
        uploadFile(file: $file, label: $label)
    }
    """
)
