# Code Reacher on large real repositories, 2026-11-09

Code Reacher was run with the real local embedding model (FastEmbed 0.9.1, `BAAI/bge-small-en-v1.5`, 384 dimensions,
sqlite-vec loaded) on two public repositories it had never seen, on a 4-CPU Linux machine.

| | Django (`django/django`, shallow clone) | Shopizer (`shopizer-ecommerce/shopizer`) |
|---|---|---|
| Languages | Python, JavaScript | Java (Spring) |
| Source files indexed | 3,051 (3 minified vendor bundles skipped) | 1,210 |
| Lines | 561k | 115k |
| Entities embedded | 44,512 | 8,934 |
| Full index from scratch (parse and embed) | 559 s, 497 MB peak | 106 s, 410 MB peak (151 s before length-sorted batches) |
| Structural index only (`--no-embed`) | n/a | 3 s, 89 MB |
| Re-index after editing one file | 7.8 s (1 file parsed, 41 vectors) | n/a |
| API endpoints found | n/a | 329 Spring endpoints |

The model is about 65 MB. Releases from 0.9.0rc1 on ship it as `dridh-embedding-model.tar.gz`, which the installer
checks and unpacks into `~/.cache/dridh/fastembed`; without it, FastEmbed downloads it once from `huggingface.co`.

## Search quality

Plain-English queries, top result:

| Query | Top result |
|---|---|
| validate an email address | `django.core.validators.EmailValidator.parse_address` (then `EmailValidator.validate`) |
| parse the HTTP accept-language header | `django.utils.translation.trans_real._parse_accept_lang_header` |
| send mail to site admins when an error happens | `django.core.mail.mail_admins` |
| paginate a queryset | `django.views.generic.list.MultipleObjectMixin.paginate_queryset` |
| make a url slug from an article title (after adding a function) | the newly added function, after a 7.8 s incremental re-index |
| add a product to the shopping cart | `ShoppingCartApi.addToCart`, `ShoppingCartFacadeImpl.addItemsToShoppingCart` |
| reset a customer's password | `ResetCustomerPasswordApi.passwordResetRequest`, `CustomerFacadeImpl.resetPassword` |
| calculate shipping cost for an order | `ShippingServiceImpl.calculateOrderTotal` (weaker: the quote methods rank lower) |

## Defects found and fixed

1. **Minified bundles were indexed as source.** Django's vendored `jquery.min.js`, `select2.full.min.js` and
   `xregexp.min.js` became thousands of one-letter entities. Files named `*.min.js` (and `.mjs`/`.cjs`), and files whose
   lines average more than 400 bytes, are now skipped with the reason "minified or generated".
2. **Two definitions with one name on one line crashed the whole index** (`IntegrityError: FOREIGN KEY constraint
   failed`): the second entity was ignored by `INSERT OR IGNORE`, then its parameters referenced a row that did not
   exist. The first definition is now kept and the duplicate's relations attach to it.
3. **An interrupted embedding was never completed.** A run killed while the model downloaded left the structural index
   committed and no vectors; the next `dridh index` said "snapshot unchanged" and skipped embedding, so semantic search
   stayed empty. Embedding is incremental, so it now also runs on an unchanged snapshot (which also re-embeds after an
   embedding-model switch).
4. **Embedding used 2-3 GB of memory.** FastEmbed's default batch of 256 pads every text to the longest in the batch.
   A batch of 16 is both faster and five times smaller (2,048 Shopizer entities: 256 → 2.1 GB, 57 s; 16 → 0.4 GB, 38 s).
   Vectors are also written in committed chunks of 512, so a long run does not hold one transaction and an interrupted
   run keeps its progress.
5. **The model lived in the temporary directory** (FastEmbed's default), so it was downloaded again after a reboot and
   search degraded while offline. It is now kept in `~/.cache/dridh/fastembed` (`FASTEMBED_CACHE_PATH` overrides).

Regression tests: `test_minified_and_generated_files_are_skipped`, `test_two_definitions_with_one_name_on_one_line_do_not_break_the_index`,
`test_an_interrupted_embedding_is_completed_by_the_next_index` (in `dridh/tests/integration/test_code_reacher.py`), and
`dridh/tests/unit/test_embedding_cache.py`.

## Limits

- **Calls through interfaces stay unresolved.** Shopizer's controllers call `ShoppingCartFacade.addItemsToShoppingCart`
  (the interface), so the implementation shows no callers. 14% of Java calls (3,767 of 26,410) resolve to one indexed
  definition; most of the rest go to the JDK, Spring or getters on interface-typed fields. Code Reacher reports these as
  unresolved instead of guessing (by design).
- `code_summary` (model-written summaries) is still not built.
