# Search models for Code Reacher

Which local embedding model finds code best for a plain-English question, and what each costs. Measured 2026-11-16 on
CodeSearchNet, the public code-search benchmark, and used to choose the model Dridh ships.

## Method

- **Data.** The CodeSearchNet test split, Python and Java: 1,000 functions per language (a fixed random sample), and
  200 queries each. A query is the first sentence of a function's docstring, its author's own statement of what it does;
  the answer is that function among all 1,000.
- **What is embedded** for each function, in four variants, built by Dridh's own extractors. The query sentence is never
  part of its own answer:
  - **r1** entity type, qualified name, signature, the name split into words, and the path (what Dridh embedded before);
  - **r2** r1 and the rest of the docstring (its purpose);
  - **r3** r1 and the start of the body, docstring and comments removed;
  - **r4** r1, the purpose and the body (what Dridh embeds since 0.9.0rc4).
- **Measures.** MRR@10 (the mean of 1/rank of the right function within the top ten), top-1 (the right function first),
  and the hybrid search Dridh runs (full-text top 30 and vector top 30 fused by reciprocal rank, k=60). Speed and memory
  were measured embedding the same texts on a 4-CPU machine, relative to the model shipped before.

## Results, best first

| # | Model | Licence | Params | Download | Peak RAM | Speed vs bge-small-en-v1.5 | Python MRR r1 | r2 | r3 | r4 | Java MRR r1 | r2 | r3 | r4 | Python top-1 r1 → r4 | Java top-1 r1 → r4 | Hybrid MRR r4 (Python / Java) | Score r4 (mean MRR) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Alibaba-NLP/gte-modernbert-base | Apache-2.0 | 149M | 600 MB | 2.2 GB | 0.38× | 0.854 | – | – | 0.931 | 0.840 | – | – | 0.893 | 0.795 → 0.900 | 0.785 → 0.850 | 0.889 / 0.836 | **0.912** |
| 2 | thenlper/gte-base | MIT | 109M | 437 MB | 1.8 GB | 0.41× | 0.844 | 0.868 | 0.913 | 0.926 | 0.839 | 0.853 | 0.865 | 0.886 | 0.790 → 0.895 | 0.785 → 0.835 | 0.878 / 0.831 | **0.906** |
| 3 | thenlper/gte-large | MIT | 335M | 1338 MB | 4.0 GB | 0.13× | 0.854 | – | – | 0.929 | 0.843 | – | – | 0.877 | 0.800 → 0.890 | 0.785 → 0.820 | 0.872 / 0.830 | **0.903** |
| 4 | BAAI/bge-large-en-v1.5 | MIT | 335M | 1338 MB | 4.0 GB | 0.11× | 0.833 | – | – | 0.927 | 0.819 | – | – | 0.869 | 0.780 → 0.890 | 0.750 → 0.815 | 0.881 / 0.820 | **0.898** |
| 5 | Qdrant/stella-en-400M-v5-doc-onnx | MIT | 435M | 1750 MB | 4.0 GB | 0.10× | 0.832 | – | – | 0.905 | 0.822 | – | – | 0.885 | 0.770 → 0.855 | 0.750 → 0.830 | 0.877 / 0.848 | **0.895** |
| 6 | **thenlper/gte-small** (shipped) | MIT | 33M | 134 MB | 1.4 GB | 1.13× | 0.812 | 0.840 | 0.897 | 0.914 | 0.817 | 0.833 | 0.853 | 0.875 | 0.765 → 0.875 | 0.770 → 0.825 | 0.871 / 0.818 | **0.895** |
| 7 | nomic-ai/modernbert-embed-base | Apache-2.0 | 149M | 600 MB | 3.7 GB | 0.34× | 0.821 | – | – | 0.911 | 0.834 | – | – | 0.877 | 0.770 → 0.870 | 0.775 → 0.815 | 0.872 / 0.822 | **0.894** |
| 8 | Snowflake/snowflake-arctic-embed-m-v1.5 | Apache-2.0 | 109M | 437 MB | 2.1 GB | 0.47× | 0.814 | – | – | 0.906 | 0.786 | – | – | 0.864 | 0.750 → 0.860 | 0.710 → 0.800 | 0.873 / 0.842 | **0.885** |
| 9 | BAAI/bge-base-en-v1.5 | MIT | 109M | 219 MB | 1.7 GB | 0.39× | 0.817 | 0.849 | 0.902 | 0.916 | 0.821 | 0.843 | 0.840 | 0.853 | 0.755 → 0.865 | 0.765 → 0.790 | 0.889 / 0.831 | **0.885** |
| 10 | BAAI/bge-base-en | MIT | 109M | 436 MB | 1.5 GB | 0.34× | 0.815 | 0.852 | 0.892 | 0.919 | 0.807 | 0.832 | 0.834 | 0.844 | 0.745 → 0.880 | 0.745 → 0.770 | 0.897 / 0.833 | **0.881** |
| 11 | intfloat/multilingual-e5-large | MIT | 560M | 2253 MB | 3.8 GB | 0.13× | 0.804 | – | – | 0.909 | 0.793 | – | – | 0.848 | 0.735 → 0.870 | 0.730 → 0.795 | 0.884 / 0.841 | **0.879** |
| 12 | BAAI/bge-small-en | MIT | 33M | 134 MB | 0.8 GB | 1.18× | 0.759 | 0.788 | 0.865 | 0.897 | 0.769 | 0.794 | 0.833 | 0.851 | 0.690 → 0.855 | 0.690 → 0.795 | 0.883 / 0.815 | **0.874** |
| 13 | BAAI/bge-small-en-v1.5 (before 0.9.0rc4) | MIT | 33M | 67 MB | 1.0 GB | 1.00× | 0.771 | 0.809 | 0.886 | 0.899 | 0.788 | 0.805 | 0.827 | 0.848 | 0.710 → 0.860 | 0.725 → 0.790 | 0.879 / 0.817 | **0.873** |
| 14 | intfloat/e5-base-v2 | MIT | 109M | 437 MB | 1.8 GB | 0.50× | 0.748 | – | – | 0.859 | 0.781 | – | – | 0.862 | 0.665 → 0.790 | 0.705 → 0.815 | 0.853 / 0.812 | **0.861** |
| 15 | Qdrant/constella-nano | MIT | 33M | 138 MB | 1.1 GB | 1.10× | 0.739 | 0.762 | 0.802 | 0.838 | 0.744 | 0.760 | 0.788 | 0.805 | 0.655 → 0.755 | 0.665 → 0.730 | 0.862 / 0.798 | **0.822** |
| 16 | mixedbread-ai/mxbai-embed-xsmall-v1 | Apache-2.0 | 24M | 97 MB | 0.9 GB | 6.62× | 0.656 | 0.685 | 0.716 | 0.755 | 0.662 | 0.667 | 0.696 | 0.704 | 0.590 → 0.700 | 0.580 → 0.610 | 0.821 / 0.786 | **0.730** |
| 17 | Qdrant/constella-zero | MIT | 31M | 32 MB | 0.3 GB | 91.63× | 0.675 | 0.696 | 0.708 | 0.746 | 0.711 | 0.663 | 0.694 | 0.694 | 0.585 → 0.655 | 0.645 → 0.605 | 0.802 / 0.743 | **0.720** |
| 18 | minishlab/potion-base-8M | MIT | 8M | 31 MB | 0.2 GB | 198.85× | 0.610 | 0.647 | 0.620 | 0.656 | 0.595 | 0.580 | 0.584 | 0.605 | 0.520 → 0.570 | 0.500 → 0.505 | 0.771 / 0.715 | **0.631** |
| 19 | minishlab/potion-retrieval-32M | MIT | 32M | 131 MB | 0.3 GB | 181.63× | 0.606 | 0.646 | 0.610 | 0.638 | 0.616 | 0.605 | 0.583 | 0.598 | 0.530 → 0.545 | 0.520 → 0.500 | 0.762 / 0.720 | **0.618** |

## What was decided

- **`thenlper/gte-small` is shipped** (since 0.9.0rc4): the best model of its size (33 M parameters, 134 MB), about as
  fast as the model before it, and within 0.02 of models three to ten times larger. Through Dridh's real code path
  (the shipped loader and text), the right method came first for **87.5% of Python and 83.5% of Java queries**.
- **The richer text (r4) mattered more than the model.** For the 16 language models, adding the purpose and the start of
  the body (r1 → r4) raised MRR by 0.07 to 0.14 for Python and 0.03 to 0.08 for Java; switching the shipped model raised
  it by about 0.02 (0.899 → 0.914 Python, 0.848 → 0.875 Java). The three static models at the bottom gained little or lost. The richer text makes a full index
  about 2.7 times slower: see [Code Reacher at scale](CODE-REACHER-AT-SCALE.md).
- Larger models (`gte-modernbert-base`, `gte-base`) score up to 0.017 higher but are 2.8 to 3 times slower than gte-small and
  have three to four and a half times its download.
- Code without doc comments has no purpose text to embed. [Code summaries](CODE-SUMMARIES.md) measures what
  model-written summaries add for it.
