from warnings import warn

import graphql


class UnknownGQLTypeWarning(UserWarning):
    pass


class GraphQLDeprecationWarning(UserWarning):
    pass


def warn_deprecated_field(
    query_name: str,
    runtime_type: graphql.GraphQLObjectType,
    representative: graphql.FieldNode,
    field_def: graphql.GraphQLField,
) -> None:
    field_path = f"{runtime_type.name}.{representative.name.value}"
    if field_def.deprecation_reason is not None:
        reason = field_def.deprecation_reason
        warn(
            f"Query '{query_name}': field '{field_path}' is deprecated: {reason}",
            GraphQLDeprecationWarning,
            stacklevel=2,
        )
    if representative.arguments:
        for arg_node in representative.arguments:
            schema_arg = field_def.args[arg_node.name.value]
            if schema_arg.deprecation_reason is not None:
                arg_ref = f"'{arg_node.name.value}' on '{field_path}'"
                reason = schema_arg.deprecation_reason
                warn(
                    f"Query '{query_name}': argument {arg_ref} is deprecated: {reason}",
                    GraphQLDeprecationWarning,
                    stacklevel=2,
                )
