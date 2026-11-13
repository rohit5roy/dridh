"""Which approved flows a story's tests prove, decided by scripts, never by a model.

A test proves a flow when its name carries the flow ID and it ran and passed. Flow IDs are written the way a test name
allows: ``EF-02.1`` may appear as ``EF-02.1``, ``EF_02_1`` or ``ef_02_1`` (``ef0201`` is not accepted: too ambiguous).
For example ``test_ef_02_1_card_declined``, ``@DisplayName("UC-03 EF-02.1 card declined")`` or ``it("[EF-02.1] …")``.

Per-test results come from the runner, in this order:

* JUnit XML reports the build already writes (Maven Surefire, Gradle) and that are newer than the run;
* the runner's own output: pytest ``-rA`` lines, ``go test -v``, node's test runner, Jest/Vitest verbose lines.

When a runner reports no per-test results, a flow still counts as proven when a test file the story's worktree holds
names it, the whole suite passed and nothing was skipped (``method: static``). Otherwise it is not proven, and an
approved ledger entry is required for it.

The Executor's own flow report is a claim. ``check_report`` validates it against the story's flows, and an
``IMPLEMENTED`` claim must be backed by a proof.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET  # noqa: S405 - reports the project's own build wrote in its own worktree
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLOW_STATUSES = ("IMPLEMENTED", "CHANGED", "NOT_DONE")
# A flow ID in a test name: SF|AF|EF|BE|BR, two digits, then the step number, separated by "-", "_", "." or a space.
_FLOW_IN_NAME = re.compile(r"(?<![A-Za-z0-9])(SF|AF|EF|BE|BR)[-_ ](\d{2})[._ -](\d+)(?![0-9])", re.IGNORECASE)
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|src/test)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|Tests?\.(java|kt)$")
_SOURCE_SUFFIXES = (".py", ".java", ".kt", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist", ".opencode", ".gradle"}
MAX_RESULTS = 20_000

_PYTEST = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+?::\S+)", re.MULTILINE)
_GO = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.MULTILINE)
_NODE = re.compile(r"^\s*(✔|✓|√|✖|✕|×)\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$", re.MULTILINE)
_TAP = re.compile(r"^\s*(ok|not ok)\s+\d+\s+-\s+(.+?)(?:\s+#\s*(SKIP|TODO).*)?$", re.MULTILINE)


def flow_ids_in(text: str) -> set[str]:
    """Normalised flow IDs (``EF-02.1``) found in a test name or file."""
    return {f"{m.group(1).upper()}-{m.group(2)}.{int(m.group(3))}" for m in _FLOW_IN_NAME.finditer(text)}


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    skipped: bool = False


def _junit(root: Path, since: float) -> list[TestResult]:
    results: list[TestResult] = []
    for pattern in ("target/surefire-reports/TEST-*.xml", "target/failsafe-reports/TEST-*.xml", "build/test-results/**/TEST-*.xml",
                    "**/target/surefire-reports/TEST-*.xml", "**/build/test-results/**/TEST-*.xml"):
        for path in root.glob(pattern):
            try:
                if path.stat().st_mtime < since:
                    continue  # a report from an earlier run is not evidence for this one
                tree = ET.parse(path)  # noqa: S314 - see the import
            except (OSError, ET.ParseError):
                continue
            for case in tree.iter("testcase"):
                name = f"{case.get('classname', '')}.{case.get('name', '')}"
                children = {child.tag for child in case}
                results.append(TestResult(name, passed=not ({"failure", "error"} & children), skipped="skipped" in children))
    return list({r.name: r for r in results}.values())


def parse_results(output: str, root: Path | None = None, since: float = 0.0) -> list[TestResult] | None:
    """Per-test results of one run, or ``None`` when neither reports nor output name individual tests."""
    results = _junit(root, since) if root is not None else []
    for status, name in _PYTEST.findall(output):
        results.append(TestResult(name, passed=status in ("PASSED", "XPASS"), skipped=status in ("SKIPPED", "XFAIL")))
    for status, name in _GO.findall(output):
        results.append(TestResult(name, passed=status == "PASS", skipped=status == "SKIP"))
    for mark, name in _NODE.findall(output):
        results.append(TestResult(name.strip(), passed=mark in ("✔", "✓", "√")))
    for status, name, directive in _TAP.findall(output):
        results.append(TestResult(name.strip(), passed=status == "ok" and not directive, skipped=bool(directive)))
    return results[:MAX_RESULTS] if results else None


def tagged_test_files(root: Path) -> dict[str, set[str]]:
    """Flow ID -> test files under ``root`` whose text names it (used when the runner reports no per-test results)."""
    found: dict[str, set[str]] = {}
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = [d for d in subdirectories if d not in _SKIP_DIRS]
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not name.endswith(_SOURCE_SUFFIXES) or not _TEST_FILE.search(relative):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for flow in flow_ids_in(text):
                found.setdefault(flow, set()).add(relative)
    return found


def prove(flows: Iterable[str], test: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    """For each flow: ``proven`` (bool), ``method`` (``results``, ``static`` or ``none``) and the tests that name it."""
    raw = test.get("test_results")
    results = None if raw is None else [TestResult(r["name"], r["passed"], r.get("skipped", False)) for r in raw]
    suite_clean = bool(test.get("passed")) and not test.get("skipped")
    files = None
    proof: dict[str, dict[str, Any]] = {}
    for flow in sorted(set(flows)):
        named = [r for r in (results or []) if flow in flow_ids_in(r.name)]
        if named:
            ran = [r for r in named if not r.skipped]
            proven = bool(ran) and all(r.passed for r in ran)
            proof[flow] = {"proven": proven, "method": "results", "tests": [r.name for r in named][:10],
                           **({} if proven else {"reason": "a test named for it failed or was skipped"})}
            continue
        if results is not None:  # the runner listed its tests and none is named for this flow
            proof[flow] = {"proven": False, "method": "none", "tests": [], "reason": "no test named for it ran"}
            continue
        if files is None:
            files = tagged_test_files(root)
        tagged = sorted(files.get(flow, ()))
        proven = bool(tagged) and suite_clean
        proof[flow] = {"proven": proven, "method": "static" if tagged else "none", "tests": tagged[:10],
                       **({} if proven else {"reason": "no test file names it" if not tagged else "the suite did not pass cleanly"})}
    return proof


def check_report(report: Any, story_flows: Iterable[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """The Executor's flow report as ``{flow: {status, note}}`` and the findings that make it invalid."""
    expected = set(story_flows)
    if not expected:
        return {}, []
    if not isinstance(report, list):
        return {}, [f"The reply must list every flow of the story in \"flows\": {', '.join(sorted(expected))}."]
    flows: dict[str, dict[str, str]] = {}
    findings: list[str] = []
    mentioned: set[str] = set()
    for item in report:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            findings.append("Each entry in \"flows\" needs an \"id\", a \"status\" and a \"note\".")
            continue
        flow, status, note = item["id"].strip(), item.get("status"), item.get("note", "")
        mentioned.add(flow)
        if flow not in expected:
            findings.append(f"{flow} is not a flow of this story.")
        elif flow in flows:
            findings.append(f"{flow} is listed more than once.")
        elif status not in FLOW_STATUSES:
            findings.append(f"{flow}: status must be one of {', '.join(FLOW_STATUSES)}.")
        elif status != "IMPLEMENTED" and not (isinstance(note, str) and note.strip()):
            findings.append(f"{flow} is {status}: say in \"note\" what was built instead, or why it was not.")
        else:
            flows[flow] = {"status": status, "note": (note or "").strip()[:500]}
    missing = sorted(expected - mentioned)
    if missing:
        findings.append(f"These flows are missing from \"flows\": {', '.join(missing)}.")
    return flows, findings


def unproven_claims(flows: dict[str, dict[str, str]], proof: dict[str, dict[str, Any]]) -> list[str]:
    """Findings for flows reported IMPLEMENTED that no passing test proves."""
    return [f"{flow} is reported IMPLEMENTED but {proof[flow].get('reason', 'nothing proves it')}: add a test whose name "
            f"includes {flow} (for example test_{flow.lower().replace('-', '_').replace('.', '_')}_...)."
            for flow, item in sorted(flows.items()) if item["status"] == "IMPLEMENTED" and not proof.get(flow, {}).get("proven")]
