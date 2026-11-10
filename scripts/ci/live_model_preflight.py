#!/usr/bin/env python3
"""Decide whether the live (Category B) model tests can run at all.

The live job depends on an external, quota-limited provider. When the provider is not
usable (daily free limit reached, bad or missing credential, model withdrawn, network or
provider outage), running the tests only produces a red workflow that says nothing about
the code. This script sends ONE tiny request to the model under test and reports whether
it answered:

* answered (HTTP 2xx)                      -> available=true, the live tests run and may fail for real
* anything else, or a missing credential   -> available=false, the live steps are skipped
                                              and a warning annotation + job summary explain why

Set REQUIRE_LIVE=true (the workflow's ``require_live`` input) to make an unavailable
provider fail the job instead, for release acceptance runs.

A genuine test failure after a successful preflight is never hidden: only provider
availability is treated as "skip".

Inputs (environment): LIVE_MODEL (openrouter/<id>), OPENROUTER_API_KEY,
OPENCODE_PROVIDER_CREDENTIAL, REQUIRE_LIVE, GITHUB_OUTPUT, GITHUB_STEP_SUMMARY.
Secret values are never printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_PATTERN = re.compile(r"openrouter/[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}")
_REASON_MAX = 240


def _one_line(text: str) -> str:
    """Single line, printable, bounded: safe for annotations, summaries and GITHUB_OUTPUT."""
    cleaned = re.sub(r"[^\x20-\x7e]+", " ", str(text)).strip()
    return cleaned[:_REASON_MAX]


def _provider_message(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return _one_line(error.get("message", ""))
    return _one_line(error or "")


def classify(status: int, body: bytes) -> tuple[bool, str]:
    """Map an HTTP result to (available, reason)."""
    if 200 <= status < 300:
        return True, "model answered the preflight request"
    detail = _provider_message(body)
    hints = {
        401: "credential rejected",
        402: "payment or credits required",
        403: "access forbidden",
        404: "model not found (withdrawn or renamed)",
        429: "rate limit or daily quota exhausted",
    }
    label = hints.get(status, "provider error" if status >= 500 else "request rejected")
    return False, _one_line(f"HTTP {status}: {label}" + (f" - {detail}" if detail else ""))


def probe(base_url: str, api_key: str, model_id: str, *, timeout: float = 45.0, attempts: int = 2) -> tuple[bool, str]:
    """Send one minimal chat completion; the credential is redacted from the returned reason."""
    available, reason = _probe(base_url, api_key, model_id, timeout=timeout, attempts=attempts)
    return available, _redact(reason, api_key)


def _probe(base_url: str, api_key: str, model_id: str, *, timeout: float, attempts: int) -> tuple[bool, str]:
    """Send one minimal chat completion. Retries only transient failures (network, 5xx)."""
    request_body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 16,
    }).encode("utf-8")
    last: tuple[bool, str] = (False, "no attempt was made")
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=request_body,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https base URL
                return classify(response.status, response.read(65536))
        except urllib.error.HTTPError as exc:
            last = classify(exc.code, exc.read(65536))
            if exc.code < 500:
                return last
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = (False, _one_line(f"provider unreachable: {type(exc).__name__}: {reason}"))
        if attempt < attempts:
            time.sleep(3)
    return last


def _redact(reason: str, secret: str) -> str:
    return reason.replace(secret, "[REDACTED]") if secret else reason


def _write_output(available: bool, reason: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"available={'true' if available else 'false'}\nreason={_one_line(reason)}\n")


def _write_summary(model: str, available: bool, reason: str, required: bool) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    if available:
        text = f"### Live model preflight\n\n`{model}` is reachable: live tests will run.\n"
    else:
        outcome = "FAILED (require_live is set)" if required else "SKIPPED"
        text = (
            f"### Live model tests {outcome}\n\n"
            f"`{model}` could not be used: **{reason}**\n\n"
            "The live (Category B) tests were not run, so this workflow says nothing about real-model "
            "behaviour for this commit. Required checks are unaffected. Rerun this workflow when the "
            "provider is available (or choose another model with the `model` input).\n"
        )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def main() -> int:
    model = os.environ.get("LIVE_MODEL", "")
    if not MODEL_PATTERN.fullmatch(model):
        print("::error::LIVE_MODEL must look like openrouter/<model-id>")
        return 1  # malformed input is a configuration error, not provider unavailability
    required = os.environ.get("REQUIRE_LIVE", "").strip().lower() == "true"

    missing = [name for name in ("OPENROUTER_API_KEY", "OPENCODE_PROVIDER_CREDENTIAL") if not os.environ.get(name)]
    if missing:
        available, reason = False, "credential not configured: " + ", ".join(missing)
    else:
        available, reason = probe(OPENROUTER_BASE_URL, os.environ["OPENROUTER_API_KEY"], model.split("/", 1)[1])

    _write_output(available, reason)
    _write_summary(model, available, reason, required and not available)
    if available:
        print(f"Live model preflight OK: {model} answered.")
        return 0
    if required:
        print(f"::error::Live model unavailable and require_live is set: {reason}")
        return 1
    print(f"::warning title=Live model tests skipped::{model} unavailable ({reason}). Live tests were not run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
