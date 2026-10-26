from tests.generated.secret_variables.gql.api import api_gql

direct = api_gql(
    """
    query Direct($string: SecretString, $blob: SecretBlob, $number: SecretNumber) {
        direct(string: $string, blob: $blob, number: $number)
    }
    """
)

in_input = api_gql(
    """
    query InInput($vault: Vault!) {
        inInput(vault: $vault)
    }
    """
)

in_list = api_gql(
    """
    query InList($strings: [SecretString!], $blobs: [SecretBlob!], $numbers: [SecretNumber!]) {
        inList(strings: $strings, blobs: $blobs, numbers: $numbers)
    }
    """
)

in_mapping = api_gql(
    """
    query InMapping($bag: JSON!) {
        inMapping(bag: $bag)
    }
    """
)
