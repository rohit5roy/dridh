# Documentation

Design notes and reference for the parts of Dridh that are not obvious from the code. The user guide is the [README](../README.md); contributing rules are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Architecture

- [Bridge protocol](architecture/BRIDGE-PROTOCOL.md): the JSON envelope between the OpenCode plugin and the Python runtime, with the operations, error codes and exit-code rules.
- [Error classification](architecture/ERROR-CLASSIFICATION.md): the error categories at each layer, and the retry policy.
- [TypeScript plugin](architecture/TYPESCRIPT-PLUGIN.md): the OpenCode plugin and terminal UI: tool input, how `dridh` is found, hooks, and tests.
- [Agent runner](architecture/AGENT-RUNNER.md): how Dridh runs OpenCode agents: invocation, failure categories, timeouts, and the live tests.
- [Story cards](architecture/STORY-CARDS.md): the approved contract Python attaches to each story, the check that cards do not contradict it, and what a file ownership entry may be.
- [Recovery scenarios](architecture/RECOVERY-SCENARIOS.md): sixteen failure scenarios, how Dridh recovers from each, and the tests that cover them.

## Benchmarks

- [Code Reacher at scale](benchmarks/CODE-REACHER-AT-SCALE.md): indexing and search measured on two large public repositories, with the defects those runs found.

## Security

- [Redaction](security/REDACTION.md): what is removed from errors, logs and diagnostics, and how the redaction tests run.
