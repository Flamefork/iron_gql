from tests.generated.directives.gql.api import api_gql

include_skip = api_gql(
    """
    query IncludeSkip($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
        user(id: $id) {
            name
            email @include(if: $withEmail)
            phone @skip(if: $skipPhone)
        }
    }
    """
)

include_non_null = api_gql(
    """
    query IncludeNonNull($withName: Boolean!) {
        user {
            id
            name @include(if: $withName)
        }
    }
    """
)

skip_non_null = api_gql(
    """
    query SkipNonNull($skipName: Boolean!) {
        user {
            id
            name @skip(if: $skipName)
        }
    }
    """
)

include_nullable = api_gql(
    """
    query IncludeNullable($withName: Boolean!) {
        user {
            id
            nullableName @include(if: $withName)
        }
    }
    """
)

include_inline_fragment = api_gql(
    """
    query IncludeInlineFragment($withDetails: Boolean!) {
        user {
            id
            ... @include(if: $withDetails) {
                name
                email
            }
        }
    }
    """
)

conditional_and_unconditional = api_gql(
    """
    query ConditionalAndUnconditional($withDetails: Boolean!) {
        user {
            id
            name
            ... @include(if: $withDetails) {
                name
            }
        }
    }
    """
)

skip_literal_false = api_gql(
    """
    query SkipLiteralFalse {
        user {
            id
            name @skip(if: false)
        }
    }
    """
)

include_skip_same_field = api_gql(
    """
    query IncludeSkipSameField($show: Boolean!, $hide: Boolean!) {
        user {
            id
            name @include(if: $show) @skip(if: $hide)
        }
    }
    """
)

include_camel_case = api_gql(
    """
    query IncludeCamelCase($withName: Boolean!) {
        user {
            id
            firstName @include(if: $withName)
        }
    }
    """
)

include_list = api_gql(
    """
    query IncludeList($withTags: Boolean!) {
        user {
            id
            tags @include(if: $withTags)
        }
    }
    """
)

include_literal_true = api_gql(
    """
    query IncludeLiteralTrue {
        user {
            id
            name @include(if: true)
        }
    }
    """
)

include_nested_object = api_gql(
    """
    query IncludeNestedObject($withAddress: Boolean!) {
        user {
            id
            address @include(if: $withAddress) {
                city
                zip
            }
        }
    }
    """
)

shared_variable = api_gql(
    """
    query SharedVariable($flag: Boolean!) {
        user {
            id
            email @include(if: $flag)
            phone @skip(if: $flag)
        }
    }
    """
)

inline_literal_false = api_gql(
    """
    query InlineLiteralFalse {
        user {
            id
            ... @include(if: false) { name }
        }
    }
    """
)

contradictory_pair = api_gql(
    """
    query ContradictoryPair($flag: Boolean!) {
        user {
            id
            name @include(if: $flag) @skip(if: $flag)
        }
    }
    """
)

mixed_polarity_variable = api_gql(
    """
    query MixedPolarityVariable($a: Boolean!, $b: Boolean!) {
        user {
            id
            ... @include(if: $b) { name }
            ... @include(if: $a) @skip(if: $b) { email }
        }
    }
    """
)

complementary_conjunctions = api_gql(
    """
    query ComplementaryConjunctions($a: Boolean!, $b: Boolean!) {
        user {
            id
            ... @include(if: $a) { ... @include(if: $b) { name } }
            ... @skip(if: $a) { ... @skip(if: $b) { name } }
        }
    }
    """
)
