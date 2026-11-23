# Code summaries: do they help search?

`dridh code summarize` asks a model for a one-sentence summary of each indexed function, method or class that has
little or no doc comment, and adds it to full-text search and to the embedded text. This measures what that changes.
Measured 2026-11-23.

## Method

- **Data.** The same CodeSearchNet sets as [Search models](SEARCH-MODELS.md): 1,000 functions per language and about 200
  queries each (the first sentence of a function's docstring). Twelve Python functions that do not parse as Python 3
  and one Java function containing its own query text were dropped: 198 Python and 199 Java queries.
- **Code without doc comments**, the case summaries are for. One project per language, one file per function, with
  **every docstring and comment removed**, so the query sentence appears nowhere in the code. Each Java method sits in
  a class of its own (`class CsnN { ... }`).
- **Dridh as shipped.** `dridh index` with `thenlper/gte-small` (not degraded), then the same queries through the
  product's searches: hybrid (`dridh code search`), full-text only, and vector only.
- **Summaries.** `dridh code summarize` through OpenCode 2.3.2 with `opencode/big-pickle`. When its free usage limit ran
  out, the rest were written with `opencode-go/deepseek-v4.1-flash`: 1,540 and 1,421 summaries respectively. The
  summarizer sees only each entity's kind, name, path, signature and code.

## Results

Top-1 is the right function first; MRR@10 the mean of 1/rank within the top ten. Python is counted at the function;
Java at the file (the method or its one-method class), because the summarized wrapper class and its method say the same
thing and take turns at the top, which a real codebase does not have.

| | Search | Top-1 before | Top-1 after | Top-5 before | Top-5 after | MRR@10 before | MRR@10 after |
|---|---|---|---|---|---|---|---|
| Python | **hybrid (default)** | 0.677 | **0.843** | 0.864 | **0.970** | 0.759 | **0.899** |
| Python | full-text | 0.621 | **0.798** | 0.737 | **0.899** | 0.667 | **0.844** |
| Python | vector | 0.843 | 0.864 | 0.939 | 0.955 | 0.889 | 0.905 |
| Java | **hybrid (default)** | 0.729 | **0.824** | 0.864 | **0.935** | 0.789 | **0.872** |
| Java | full-text | 0.553 | **0.749** | 0.759 | **0.905** | 0.630 | **0.814** |
| Java | vector | 0.799 | 0.834 | 0.899 | 0.930 | 0.840 | 0.875 |

- **Summaries make the default search find undocumented code first 17 points more often in Python and 10 in Java**, and
  put it in the top five 97% and 94% of the time.
- **They mostly repair the full-text half.** Full-text search gains 18 to 20 points at top-1; vector search, which
  already reads the code, gains 2 to 4. Before summaries, the hybrid search did worse than vectors alone on undocumented
  Python (0.677 against 0.843), because a weak full-text ranking pulled the fusion down; with summaries the two agree.
- Counted strictly at the Java method, top-1 moves from 0.724 to 0.688 while top-5 rises from 0.849 to 0.910: the
  method's own class now ranks first as often as the method. In real code a class and its methods say different things.

## Cost and quality

- **About 2,960 entities in about 45 minutes**, 150 calls of 20 entities, 0.9 M input and 0.1 M output tokens. Both
  models recorded no cost (a free tier and a subscription), so there is no dollar figure.
- **Summaries are accurate and plain**: 103 to 106 characters on average, none containing its query, sharing about a
  third of the query's words, as a paraphrase of the same behaviour does. Some describe the mechanism rather than the
  intent ("Detects a timestamp line in a daily log scrape" for "calculate the approximate date/time ... when the job was
  built").
- **A free model's limit runs out.** big-pickle's free usage ended after about 760 summaries per language. Since
  0.9.0rc5 `dridh code summarize` stops at the first exhausted batch, keeps what it wrote and says to run again later or
  with `--model`; a second run continues with the entities still missing.

## Verdict

Use summaries for code with few doc comments: they lift the default hybrid search from 0.68 to 0.84 (Python) and 0.73
to 0.82 (Java) at top-1. Code with a doc comment of 40 characters or more is skipped: its comment already says what it does.

Limits of this measurement: one embedding model, CodeSearchNet queries (descriptive, one sentence), one function per
file, and summaries from two models.
