# The contract between the generator and the consumers of a generated package.
# `codegen.render` writes the client binding under this name; `iron_gql.testing`
# — and any other code that swaps the client out — reads it back. The rule lives
# outside `codegen` so that reading it never pulls the generator (and with it
# graphql-core) into a runtime-only install.


def client_binding_name(package_name: str) -> str:
    return f"{package_name.upper()}_CLIENT"
