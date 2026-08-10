from tests.generated.type_matrix.gql.api import api_gql

every_cell = api_gql(
    """
    query EveryCell($stringPlain: String, $stringRequired: String!, $stringList: [String!]!, $stringSparse: [String], $intPlain: Int, $intRequired: Int!, $intList: [Int!]!, $intSparse: [Int], $idPlain: ID, $idRequired: ID!, $idList: [ID!]!, $idSparse: [ID], $sizePlain: Size, $sizeRequired: Size!, $sizeList: [Size!]!, $sizeSparse: [Size], $filterPlain: Filter, $filterRequired: Filter!, $filterList: [Filter!]!, $filterSparse: [Filter]) {
        echo(payload: {stringPlain: $stringPlain, stringRequired: $stringRequired, stringList: $stringList, stringSparse: $stringSparse, intPlain: $intPlain, intRequired: $intRequired, intList: $intList, intSparse: $intSparse, idPlain: $idPlain, idRequired: $idRequired, idList: $idList, idSparse: $idSparse, sizePlain: $sizePlain, sizeRequired: $sizeRequired, sizeList: $sizeList, sizeSparse: $sizeSparse, filterPlain: $filterPlain, filterRequired: $filterRequired, filterList: $filterList, filterSparse: $filterSparse}, stringPlain: $stringPlain, stringRequired: $stringRequired, stringList: $stringList, stringSparse: $stringSparse, intPlain: $intPlain, intRequired: $intRequired, intList: $intList, intSparse: $intSparse, idPlain: $idPlain, idRequired: $idRequired, idList: $idList, idSparse: $idSparse, sizePlain: $sizePlain, sizeRequired: $sizeRequired, sizeList: $sizeList, sizeSparse: $sizeSparse, filterPlain: $filterPlain, filterRequired: $filterRequired, filterList: $filterList, filterSparse: $filterSparse) {
            seen
            size
            sizes
        }
    }
    """
)

defaults = api_gql(
    """
    query Defaults {
        labelled { seen size }
    }
    """
)

slotted = api_gql(
    """
    query Slotted($payload: Payload!) {
        echo(payload: $payload) @slot { __typename }
    }
    """
)

size_parts = api_gql(
    """
    fragment SizeParts on Echo {
        seen
        tagged(size: $frag_size, term: $frag_term)
    }
    """
)

bound = slotted.bind(echo=size_parts)
