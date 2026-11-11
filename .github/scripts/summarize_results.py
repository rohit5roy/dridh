#!/usr/bin/env python3
"""Summarise test results for CI job summaries and evidence files.

Subcommands:
    junit <file.xml> [...]   summarise pytest JUnit XML, including every skip and its reason
    spec <log>               summarise the counters printed by `node --test` (spec reporter)

Prints a Markdown summary to stdout. With --json, also writes a machine-readable
summary. Exits 1 when a failure, an error or a cancellation is present, when a
mandatory (Category A) test is skipped, when a Node test is skipped, or when a Node
log has no counters.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def category_of(reason: str) -> str:
    if "Category B" in reason:
        return "B"
    if reason.startswith("Local-only"):
        return "L"
    return "A"


def summarise_junit(files: list[Path]) -> dict:
    totals = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    skipped: list[dict] = []
    for file in files:
        root = ET.parse(file).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for case in suite.iter("testcase"):
                totals["tests"] += 1
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                if case.find("failure") is not None:
                    totals["failures"] += 1
                elif case.find("error") is not None:
                    totals["errors"] += 1
                elif case.find("skipped") is not None:
                    totals["skipped"] += 1
                    node = case.find("skipped")
                    reason = node.get("message") or (node.text or "").strip()
                    category = category_of(reason)
                    skipped.append({"test": name, "category": category, "reason": reason, "source": file.name})
                else:
                    totals["passed"] += 1
    return {"source": [file.name for file in files], **totals, "skipped_tests": skipped}


def summarise_spec(log: Path) -> dict:
    text = log.read_text(encoding="utf-8", errors="replace")
    counters = {}
    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        match = re.search(rf"^ℹ {key} (\d+)$", text, re.M)
        if match:
            counters[key] = int(match.group(1))
    return {"source": log.name, **counters}


def markdown_junit(summary: dict) -> str:
    lines = [
        "| Source | Tests | Passed | Failed | Errors | Skipped |",
        "|---|---:|---:|---:|---:|---:|",
        f"| {', '.join(summary['source'])} | {summary['tests']} | {summary['passed']} | {summary['failures']} | {summary['errors']} | {summary['skipped']} |",
    ]
    if summary["skipped_tests"]:
        lines += [
            "",
            "Skipped tests (reported, never counted as passed). A: mandatory, so a skip fails the job; "
            "B: live model tests; L: local-only checks on a developer machine.",
            "",
            "| Category | Test | Reason |",
            "|---|---|---|",
        ]
        lines += [f"| {item['category']} | `{item['test']}` | {item['reason']} |" for item in summary["skipped_tests"]]
    return "\n".join(lines)


def markdown_spec(summary: dict) -> str:
    return "\n".join(
        [
            "| Source | Tests | Pass | Fail | Cancelled | Skipped |",
            "|---|---:|---:|---:|---:|---:|",
            f"| {summary['source']} | {summary.get('tests', 'n/a')} | {summary.get('pass', 'n/a')} | {summary.get('fail', 'n/a')} | {summary.get('cancelled', 'n/a')} | {summary.get('skipped', 'n/a')} |",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    junit = sub.add_parser("junit")
    junit.add_argument("files", nargs="+", type=Path)
    junit.add_argument("--json", type=Path)
    spec = sub.add_parser("spec")
    spec.add_argument("log", type=Path)
    spec.add_argument("--json", type=Path)
    args = parser.parse_args()

    if args.command == "junit":
        summary = summarise_junit(args.files)
        print(markdown_junit(summary))
        mandatory_skips = [item["test"] for item in summary["skipped_tests"] if item["category"] == "A"]
        if mandatory_skips:
            print(f"Mandatory (Category A) tests skipped: {', '.join(mandatory_skips)}", file=sys.stderr)
        ok = (
            summary["failures"] == 0
            and summary["errors"] == 0
            and summary["tests"] > 0
            and not mandatory_skips
        )
    else:
        summary = summarise_spec(args.log)
        print(markdown_spec(summary))
        if summary.get("skipped", 0):
            print(f"{summary['skipped']} Node test(s) skipped; mandatory tests must not skip", file=sys.stderr)
        ok = (
            summary.get("tests", 0) > 0
            and summary.get("fail", 1) == 0
            and summary.get("cancelled", 1) == 0
            and summary.get("skipped", 0) == 0
        )
    if getattr(args, "json", None):
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
