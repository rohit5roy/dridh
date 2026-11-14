# Flow coverage and the deviation ledger

Every approved use-case flow ends in one of two ways before a feature may leave execution:

- **Proven:** the story that owns the flow is complete, its Executor reported the flow `IMPLEMENTED`, and a passing test
  whose name carries the flow ID proves it.
- **Explained:** `LEDGER.md` has a `DEVIATED`, `NOT_DONE` or `WONT_DO` entry for the flow, with a current human approval.

Scripts decide all of this from the approved documents, the plan and the test results. No model call is involved
beyond the Executor's own run, whose flow report is a claim that the scripts check.

## The Executor's flow report

The Executor's prompt lists the story's approved flows (type and text, from the approved Use Case rows). Its reply adds
one entry per flow:

```json
{"status": "DONE", "summary": "...", "files_changed": ["..."],
 "flows": [{"id": "EF-01.1", "status": "IMPLEMENTED", "note": ""},
           {"id": "BE-01.1", "status": "NOT_DONE", "note": "the basket screen ships next release"}]}
```

`executor/flow_coverage.py` checks it: every flow listed exactly once, no unknown flow, a valid status, and a note for
`CHANGED` and `NOT_DONE`. An invalid report goes back to the Executor through the usual repair loop.

## Proving a flow

A test proves a flow when its name contains the flow ID (`test_ef_01_1_payment_declined`,
`@DisplayName("EF-01.1 payment declined")`, `it("[EF-01.1] payment declined")`) and it ran and passed. Per-test
results come from fresh JUnit XML reports (Maven, Gradle) or the runner's output (pytest `-rA`, `go test -v`, node,
Jest/Vitest). Dridh's default commands add `-rA` for pytest and `-v` for Go.

When a runner names no tests, a flow still counts as proven when a test file names it, the suite passed and nothing was
skipped (`method: static`). A flow reported `IMPLEMENTED` without proof fails the story's checks.

## Draft deviation entries

For each flow reported `CHANGED` or `NOT_DONE` in a story that passed its checks, `ledger/drafts.py` appends an entry
to `.opencode/dridh/documents/LEDGER.md` (created with Table 14 when missing) and imports it:

- `CHANGED` becomes `DEVIATED` and `NOT_DONE` stays `NOT_DONE`;
- the original requirement is copied from the approved Use Case rows;
- the approver is left empty. The same flow and story never get a second entry;
- the result is validated before it is written, so a failed draft never leaves a broken ledger behind.

A `BLOCKED` story's flows are recorded only when a human decides to drop it:
`dridh ledger draft --story ST-1.2 --reason "..."`.

`dridh ledger approve LDG-01 --reviewer <name>` (interactive) writes the reviewer into an empty Approved By cell, then
records the decision bound to the entry's hash. Editing an approved entry makes the approval stale.

## Leaving execution

`coordinator/delivery_check.py` (`dridh feature check-delivery`) is the gate. A feature may leave execution only when:

- every story is `COMPLETED`, or `BLOCKED` with all its flows explained;
- every approved flow is proven or explained;
- the ledger reconciles without errors, and every non-`DONE` entry has a current human approval.

Each blocker is named, for example "BE-01.1 (ST-1.1) is not proven by a passing test named for it". The OpenCode panel
shows pending approvals as the next action. Stories delivered before flow reports existed have no evidence: they block
with a message saying how to re-run the story or record the flow in the ledger.

## Seeing it

`dridh coverage` (or the Coverage tab in the OpenCode panel, `/dridh-coverage`) shows every approved flow with its
story, its state, the test that proves it or the ledger entry that explains it, and its code, grouped by use case.
