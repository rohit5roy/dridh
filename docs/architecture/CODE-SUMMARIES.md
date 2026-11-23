# Code summaries

Code search embeds each entity's name, signature, doc comment and the start of its code. Code without doc comments is
found by its names alone, so a search in plain words ("reserve a book for a member") can miss it. `dridh code
summarize` closes that gap, and only that gap.

## What it does

```bash
dridh code summarize --dry-run      # the entities it would summarise; no model is called
dridh code summarize --limit 200    # summarise up to 200 of them
```

1. **It picks** functions, methods, classes, interfaces and constructors from the current index that have a body (three
   lines or more) and less than 40 characters of doc comment. The most-referenced come first. Entities already
   summarised for the same snapshot are skipped, so a second run continues where the first stopped.
2. **It asks** the Code Summarizer worker (`dridh/workers/code-summarizer`) for one sentence per entity, 20 entities
   per call. The worker has no file access: it gets each entity's kind, name, file, signature and the start of its code
   (1,500 characters).
3. **Python checks** every reply. Each requested id must be answered once, with one plain sentence of at most 200
   characters and no Markdown. A bad reply goes back to the worker through the usual repair loop, at most three times.
   A batch that still fails is reported, and the batches before it are kept.
4. **It stores** each summary in `code_summary`, with the model and the index snapshot. The summary is added to
   full-text search and placed at the front of the entity's embedded text, and the entity's vector is rebuilt.
   `dridh code search` results include it as `summary`.

## Limits

- It runs only when asked, never during `dridh index` or the background re-index.
- Summaries belong to an index snapshot. When a file changes, the next index gives its entities new rows, and they
  can be summarised again.
- `--model` chooses the worker's model. Without it, the worker's model is resolved like any agent's; see
  [Agent runner](AGENT-RUNNER.md#which-model-a-run-uses). Use a cheap model: the task is one short sentence per entity.
- `dridh observe` shows how many entities of the current index have summaries.
