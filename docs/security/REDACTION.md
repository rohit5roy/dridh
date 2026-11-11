# Credential Redaction

**Module:** `dridh/src/dridh/common/redaction.py`  
**Security suite:** `dridh/tests/security/test_redaction_security.py`

## Key detection

Keys are canonicalised to snake_case:

1. Insert `_` at camelCase boundaries (`accessToken` -> `access_token`).
2. Lowercase.
3. Replace non-alphanumeric runs with `_`.

A key is sensitive when it:

- equals a sensitive exact name (`api_key`, `password`, `token`, `authorization`,
  `credential`, `secret`, `cookie`, etc.), or
- ends with a sensitive suffix (`_api_key`, `_access_key`, `_secret_key`,
  `_password`, `_token`, `_credential`, `_client_secret`, `_access_key_id`,
  `_aws_secret_access_key`, etc.).

This catches `openai_api_key`, `OPENAI_API_KEY`, `dbPassword`, `x-api-key`,
`aws_secret_access_key`, `refreshToken`, `clientSecret` and nested arrays.
Benign names such as `token_count`, `max_tokens`, `secretary` and `monkey` are
preserved.

## Value patterns

- Bearer tokens, `sk-` keys, GitHub PATs, `ghp_*`, Slack tokens, AWS `AKIA` IDs.
- Private-key blocks.
- `key=value`, `key: value`, quoted JSON-like pairs and CLI flags
  (`--api-key value`, `--db-password=value`).
- URL credentials (`https://user:password@host`).
- Sensitive query-string parameters (`?token=…`, `&api_key=…`): the name is matched with the key rules above, and the value is replaced.
- Environment assignments on their own line or in a block (`OPENAI_API_KEY=…`, `DB_PASSWORD=…`): the value is replaced, the name kept.
- Secret-bearing filesystem paths, replaced with `[REDACTED_PATH]`:
  - `/.ssh/` and `/.aws/` directories;
  - SSH private keys named `id_rsa` or `id_ed25519`;
  - OpenCode's `~/.config/opencode/auth.json`;
  - `.env` files, including `.env.<name>`;
  - `.npmrc` and `.pypirc`;
  - `credentials` as a file: `/credentials`, `credentials.json`, `credentials.yaml`/`.yml`, `.toml`, `.ini`, `.cfg`, `.txt`, `.xml`.

  The bare word "credentials" in ordinary prose is not redacted.

## Surfaces covered

- `DridhError.message` and `details`.
- Structured log records and nested fields.
- Subprocess command text and stderr diagnostics.
- Agent runner error details.
- TypeScript bridge error messages/details (`redactText` in `bridge-client.ts`).

## Verification

`dridh/tests/security/test_redaction_security.py` contains 37 parametric and
targeted tests. Every test uses a synthetic value and asserts the value is absent
from the serialised output. The suite is part of normal `pytest` and can be run
alone:

```bash
cd dridh
uv run pytest tests/security -q
```
