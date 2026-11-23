# A real feature on a free model

One feature taken through every stage with a real, free model, to see where a cheap model struggles. Measured
2026-11-23 with Dridh 0.9.0rc5 (before the fixes below), OpenCode 2.3.2 and `opencode/big-pickle` for every agent.

## The feature

"Library holds": a small Python command-line library with a JSON store. Members place holds on books (queued in order),
renew loans for 14 days unless another member holds the book, and list a book's holds. Three use cases with 29 flows,
records `Member`, `Book`, `Loan` and `Hold`. A person (the tester) answered the inquiry questions and approved each stage.

## Results

| | |
|---|---|
| Agent runs | 26, all succeeded; no provider or quota errors |
| Tokens | 2.4 M (686 k input, 120 k output, 1.3 M cache read); cost $0 |
| Agent time | 35 min (ideator 2, designer 8, planner 16, executor 7) |
| Stories | 4 of 4 completed, each on its first attempt, **0 repairs** |
| Tests | 82 passing in the final worktree |

| Story | Flow report | Flows proven by a passing test named for them |
|---|---|---|
| Package scaffold | no flows (infrastructure) | – |
| JSON store | 29 flows, all "not built here", with the reason ("belongs to the service and command line stories") | 0 of 29 |
| Domain service | 29 flows implemented | 29 of 29 (29 tests named `test_<flow id>_...`) |
| Command line | 29 flows implemented | 29 of 29 (58 named tests, including the service's) |

**The Executor was the strongest stage.** It returned a valid flow report every time, named tests for every flow it
built, and said honestly which flows a story could not own.

**The new checks did not loop.** The record-fields check never fired: the model defined each record's fields. The plan's
contract and file-ownership checks never fired either. One Designer pass used all three repairs on JSON shape mistakes (a
list where a string was expected, an extra `##` section) and passed on its last try.

## Problems found, and fixed in 0.9.0rc5

1. **An approved plan could not be executed.** The Testing planner wrote Markdown code spans in its test scopes. Writing
   the plan and reading it back drops the backticks, so `dridh execute start` saw a plan that differed from the approved
   one and refused it; it had already created a run and marked a story in progress. Now both sides are compared as they
   read back, story titles too, and the check runs before any run is created.
2. **Proof was hidden by "not built here".** Every story owned all 29 flows, so the store story's honest "not built
   here" became 29 ledger drafts, which overrode the other stories' passing tests: coverage showed 0 of 29 and delivery
   waited for 29 approvals of deviations that did not happen. Now a flow proven by any story that owns it is proven, a
   "not built here" for it needs no decision, and none is drafted while another owner can still build the flow. A
   behaviour change (DEVIATED) still needs a person's decision.
3. **The feature did not advance on the command line.** `dridh ideator start` did not bind the session to the feature,
   so approvals left it at INITIALISED. Now `--feature` binds it (the only open feature by default) and each approval
   moves the feature as far as its evidence allows.
4. An unquoted `mode: off` in `dridh.yaml` (YAML reads it as false) crashed with a raw traceback; it now means off, and
   any invalid setting is a readable error. A mismatched approval confirmation now says what was expected, and its hint
   no longer names a command that does not apply.

Not covered: story tests in Docker containers (the Docker daemon was not running, so they ran on the host) and delivery
to GitHub.
