# EARS Spec: Refactor client module into smaller components

## EARS Format

- **While** the application is running and relying on the AgentBus client,
- **When** the `client.py` module is accessed or imported,
- **If** the codebase structure is being maintained or extended,
- **Then** the client functionality must be cleanly separated into smaller, cohesive modules (each under 550 lines), such that functionality remains identical and all tests pass without regression.

## Plan
1. Check last commit (already verified).
2. Analyze `src/agentbus_client/client.py`.
3. Extract cohesive components (e.g. models, async transport, sync transport, error definitions, etc.) into separate files within a `client_core` or similar module namespace, while keeping the public API exposed through `src/agentbus_client/client.py` (using imports) for backwards compatibility.
4. Run all unit tests to ensure no regressions.
5. Commit the changes referencing the ticket ID.
