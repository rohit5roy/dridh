# Story cards: the approved contract and file ownership

The Executor builds a story from its card alone. It cannot read the use cases or the technical design, so everything it
must honour has to be on the card. Python supplies the facts and checks the card; the Planner's indexer worker only
proposes it.

## The approved contract

The approved technical design's traceability table (Table 13) names the API and event references for each use case,
flow and component, for example `place_hold(member_id, item_id, today) -> None; holds_for(item_id) -> list[str]`.
Each reference is imported whole: the cell is split on `;`, and every part keeps its name, parameters and return type.

When the Executor receives a story, Python attaches `approved_contract`:

- the references of the story's flows, plus those of its use cases that name no flow;
- only the story's own component's references, when it has any (otherwise all of them, since a story may call an API
  another component provides);
- copied verbatim, in document order, without duplicates;
- read from the technical design revision the plan was approved against, not the latest one.

The Executor's prompt shows them under "Approved contract for this story": the names and parameters must be implemented
exactly, and the contract wins over a todo or acceptance criterion that disagrees. An infrastructure story, and a plan
whose design revision is not stored, gets no contract section and runs as before.

## Cards may not contradict the contract

When the indexer proposes the stories, a card whose title, description, todos, acceptance criteria or definition of
done mentions an approved name with different parameters is rejected, for example `renew(member_id, item_id, today)`
when the design says `renew(item_id, today)`. The error names the story, the text found and the approved signature, and
goes back to the worker through the usual repair loop.

Parameter names are compared in order. Whitespace, type annotations, defaults, return types, `self`/`cls`/`this` and
Python's `*` and `/` markers are ignored, and so is a parameter's type written before its name (`String itemId`). A
mention without parentheses is not compared, and neither is an example call that passes values
(`renew("B1", date(2026, 1, 1))`).

## File ownership is a path

A story may change only the files its `file_ownership` covers. Each entry must be a plain project-relative path,
directory or glob: letters, digits and `. _ - / * ? [ ] @ +` only, with no spaces, parentheses, commas or notes, and no
leading `/`, drive letter or `..`. An entry such as `lendlib/library.py (hold methods only)` is rejected: ownership is per
file, so a story limited to part of a file lists the file's path and says which part in its todos or definition of done.

Enforcement and the parallel-safety check read entries the same way. A path owns itself and, for a directory,
everything under it. A glob matches with `**` across directories and `*` and `?` within one; other characters are
literal, so `app/[id]/page.tsx` owns that file. For deciding which stories may run in parallel, a glob counts as its
literal base directory, which can only report more overlap, never less.
