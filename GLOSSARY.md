# iron_gql

A GraphQL to Python code generator and runtime: it reads a schema and the GraphQL statements written in a package's source, and generates one typed module that executes them and parses their results into Pydantic models.

## Statements and modules

**Statement**:
A GraphQL text literal passed to a generated module's gql function in the package's source; one statement holds an operation or one or more fragment definitions.
_Avoid_: query string, document

**Generated module**:
The Python module the generator writes for one package: the namespace in which models are shared, names are claimed and the runtime client lives.
_Avoid_: query module, API module

**Template**:
An operation with at least one slot: it is not executed itself, only bound.

## Fragment slots

**Slot**:
A template's named point for caller-supplied fragments, shared by its `@slot` fields with the same response key; its name is the `bind()` keyword.
_Avoid_: fragment slot (in prose after the first mention)

**Unfilled slot**:
A slot to which a binding supplies no fragments: its static selection stays in the operation, but no fragment can read it.

**Fragment definition value**:
The typed Python value of one named GraphQL fragment, returned by a statement that holds only that fragment in a package with a template; it reads its own data back from slots. "Fragment definition" alone means the GraphQL syntax `fragment X on T { … }`.
_Avoid_: fragment value, fragment definition (for the value)

**Fragment factory**:
A fragment definition value whose document closure uses a GraphQL variable: it cannot be bound itself, only through its applications.

**Fragment application**:
A fragment factory together with values for its variables, made by `with_args`; it is bound like a fragment definition value and reads the same data as its factory.
_Avoid_: applied fragment

**On-type base**:
The `On{Type}` base shared by every fragment definition value and application whose type condition names one type, through which shared code accepts any bindable fragment on that type while keeping its exact type.

**Combination**:
A template with a choice of fragments for each of its slots, an empty choice included, independent of application argument values: one combination is one operation text the generator writes.

**Binding**:
An executable instance of a template's combination, returned by `bind()` and carrying the argument values of any fragment applications bound into it.
_Avoid_: bound operation

**Offered fragments**:
The fragments a binding makes readable at a slot: those bound to it and those reachable from them through spreads at the slot's root, on the slot's types.
_Avoid_: offered set, readable set

**Fragment coverage**:
The concrete runtime types on which an offered fragment's fields are present in one slot of one combination; it can be narrower than the fragment's own type condition.

**Readable closure**:
What a fragment definition value promises a slot offers wherever it is bound: itself and the fragments it spreads at its root, as far as every slot it fits offers them.
_Avoid_: closure (unqualified)

**Document closure**:
A fragment together with every fragment it spreads, at any depth: what has to be sent with it.
_Avoid_: closure (unqualified)

## Model names

**Shape**:
The structural identity of a result model or result union alias — its GraphQL type, fields and nested shapes — by which models are shared and names are given, independent of where the selection stands.
_Avoid_: selection (for the model), path

**Structural key**:
The canonical spelling of a shape, by which models are merged and names are told apart.
_Avoid_: shape key

**Bare name**:
A model or result union alias named just after its GraphQL type (`User`), which only the type's sole shape in the module gets.

**Short name**:
A model name `{Type}With{Fields}` (`UserWithIdName`), which only the sole shape of that type with exactly those fields gets.

**Detailed name**:
A model name that spells its shape one level deep (`PostWithAuthor_Opt_UserWithName`), with a digest of the structural key after it when two shapes still spell alike.

**Claimant**:
The identity behind a candidate generated name: a GraphQL type for its bare name, a type with one set of fields (a short-name claimant) for its short name, a shape for its detailed name. Its claim stands even when it holds several shapes and so cannot receive the name.
_Avoid_: slot (for a short-name claimant)

**Fallback model**:
The shared result model for the possible concrete types of an interface selection that have no type-specific variant of their own while other possible types do.
_Avoid_: catch-all model

**Result union alias**:
A `type` alias for a union or interface result, named by its shape like a model.
_Avoid_: alias (unqualified: in GraphQL an alias renames a field), union alias

**Pinned name**:
A generated name that follows from what the developer wrote rather than from a shape, and never moves: an operation, template or fragment class, their bases and results, an on-type base, every model of a slot, every model and alias of a fragment's data.
_Avoid_: fixed name, developer-named name

**Kind suffix**:
The word `Query`, `Mutation`, `Subscription` or `Fragment` that the class name of an operation, template or fragment always carries; the names derived from that class, its bound base and result, extend it.

## Runtime

**Runtime client**:
The `GQLClient` or `AsyncGQLClient` a generated module creates at import to execute its operations and that owns its own connections.

**Client swap**:
A `use_client` replacement of a generated module's runtime client for the whole process, undone on exit.

## Testing

**Generation envelope**:
The contract that any weird but valid generator input ends in a diagnosed rejection or an importable module, never a crash.
_Avoid_: envelope (unqualified)
