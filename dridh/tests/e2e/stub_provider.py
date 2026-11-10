#!/usr/bin/env python3
"""Deterministic OpenAI-compatible provider used as the model boundary of the E2E.

Only the model response is simulated. OpenCode, the Dridh plugin, the bridge, the
Coordinator, the workspace, the ledger, the worker sessions and every tool call
run for real. The role of each request is read from the agent profile text that
OpenCode places in the system message, so the script cannot be confused by the
user's wording.

Usage: stub_provider.py <port> <log.jsonl> <control.json>

The control file is re-read on every request. It may set "code_reacher_delay"
(seconds to stall a worker before it replies), used by the timeout and
cancellation scenarios.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1])
LOG = Path(sys.argv[2])
CONTROL = Path(sys.argv[3])

WF_ID = re.compile(r"wf-[0-9a-f]{12}")
OUTCOME = re.compile(r'"outcome"\s*:\s*"([A-Z_]+)"')

GREETER = 'def greet(name):\n    return f"Hello, {name}!"\n'
TEST_GREETER = (
    "import os\nimport sys\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
    "import unittest\nfrom src.greeter import greet\n\n\n"
    "class GreetTest(unittest.TestCase):\n"
    "    def test_greet(self):\n"
    "        self.assertEqual(greet('Ada'), 'Hello, Ada!')\n\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n"
)

PLAN = {
    "schema": "dridh.plan",
    "schema_version": 1,
    "objective": "Create a greeting function with tests",
    "stages": [
        {
            "id": "S-01",
            "title": "Greeting module",
            "tasks": [
                {
                    "id": "T-001",
                    "title": "Greeting function",
                    "agent": "dridh/code-reacher",
                    "depends_on": [],
                    "allowed_paths": ["src/"],
                    "instructions": "Create src/greeter.py defining greet(name) that returns 'Hello, <name>!'.",
                    "max_attempts": 2,
                    "acceptance": [
                        {"type": "file_exists", "path": "src/greeter.py"},
                        {"type": "file_contains", "path": "src/greeter.py", "text": "def greet"},
                    ],
                },
                {
                    "id": "T-002",
                    "title": "Greeting tests",
                    "agent": "dridh/code-reacher",
                    "depends_on": ["T-001"],
                    "allowed_paths": ["tests/"],
                    "instructions": "Create tests/test_greeter.py with a unittest for greet.",
                    "max_attempts": 2,
                    "acceptance": [
                        {"type": "command", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]}
                    ],
                },
            ],
        }
    ],
    "acceptance": [],
}

_state: dict = {}

EXECUTE_STEPS = {
    "ping": "return await tools.dridh.bridge({ operation: 'runtime.ping' })",
    "info": "return await tools.dridh.bridge({ operation: 'runtime.info' })",
    "create": "return await tools.dridh.bridge({ operation: 'workflow.create', payload: { objective: %(objective)s } })",
    "plan": "return await tools.dridh.bridge({ operation: 'workflow.plan', payload: { workflow_id: '%(wf)s' } })",
    "execute_unapproved": "return await tools.dridh.bridge({ operation: 'workflow.execute', payload: { workflow_id: '%(wf)s' } })",
    "execute": "return await tools.dridh.bridge({ operation: 'workflow.execute', payload: { workflow_id: '%(wf)s' } })",
    "report": "return await tools.dridh.bridge({ operation: 'workflow.report', payload: { workflow_id: '%(wf)s' } })",
    "status": "return await tools.dridh.bridge({ operation: 'workflow.status', payload: { workflow_id: '%(wf)s' } })",
}


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _control() -> dict:
    try:
        return json.loads(CONTROL.read_text())
    except (OSError, ValueError):
        return {}


def _log(entry: dict) -> None:
    with LOG.open("a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _tool_names(request: dict) -> set[str]:
    return {tool["function"]["name"] for tool in request.get("tools", [])}


def _tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {"tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _execute(code: str, call_id: str) -> dict:
    return _tool_call("execute", {"code": code}, call_id)


def _role(system: str) -> str:
    for role in (
        "dridh/coordinator",
        "dridh/workers/planner-indexer",
        "dridh/workers/planner-component",
        "dridh/workers/planner-testing",
        "dridh/planner",
        "dridh/code-reacher",
        "dridh/executor",
        "dridh/ideator",
        "dridh/designer",
        "dridh/documents",
    ):
        if f"Role identifier: {role}" in system:
            return role
    return "none"


def _json_after_marker(text: str, marker: str = "APPROVED INPUT (canonical JSON):") -> dict:
    """Parse a marked, multiline JSON object from the Planner worker prompt."""
    position = text.rfind(marker)
    if position < 0:
        raise ValueError(f"the Phase 13 worker prompt is missing {marker!r}")
    decoder = json.JSONDecoder()
    payload = text[position + len(marker):].lstrip()
    value, _end = decoder.raw_decode(payload)
    if not isinstance(value, dict):
        raise ValueError("the Phase 13 worker prompt's canonical payload is not an object")
    return value


def _last_json_object(text: str) -> dict:
    """Parse the canonical Planner payload, not JSON fragments from instructions."""
    for marker in ("APPROVED INPUT (canonical JSON):", "HUMAN CLARIFICATIONS (apply without changing approved requirements):"):
        if marker in text and marker.startswith("APPROVED"):
            return _json_after_marker(text, marker)
    decoder = json.JSONDecoder()
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        for offset, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _end = decoder.raw_decode(candidate[offset:])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("the Phase 13 worker prompt did not contain a canonical JSON object")


def _phase13_indexer(user_text: str) -> dict:
    baseline = _last_json_object(user_text)
    mapping: dict[str, set[str]] = {}
    for item in baseline.get("tdd_traceability", []):
        if item.get("flow_id") and item.get("component_id"):
            mapping.setdefault(item["flow_id"], set()).add(item["component_id"])
    components = {item["id"]: item["name"] for item in baseline.get("components", [])}
    flows = baseline.get("flows", [])
    stories = []
    for flow in flows:
        for component_id in sorted(mapping.get(flow["id"], set())):
            key = re.sub(r"[^a-z0-9]+", "-", f"{flow['id']}-{component_id}".lower()).strip("-")
            stories.append({
                "key": key,
                "component_id": component_id,
                "title": f"{flow['subject']} — {components.get(component_id, component_id)}",
                "description": f"Implement the approved {flow['type'].lower()} behaviour: {flow['action']} and verify {flow['result']}.",
                "use_case_ids": [flow["use_case_id"]],
                "flow_ids": [flow["id"]],
                "todos": ["Implement the behaviour inside the approved component boundary.", "Add deterministic unit and integration coverage."],
                "acceptance_criteria": [f"The {flow['id']} journey returns the approved result: {flow['result']}."],
                "definition_of_done": ["Implementation and tests are reviewed.", "The relevant integration test passes against real dependencies."],
                "dependencies": [],
                "dependency_rationale": {},
                "file_ownership": [f"src/phase13/{key}.py"],
                "resource_ownership": [],
                "infrastructure": False,
                "infrastructure_rationale": "",
            })
    return {
        "summary": "Deterministic stub proposal maps every approved flow to its TDD component.",
        "blockers": [],
        "stories": stories,
        "project_structure": {
            "summary": "Preserve the approved Order Service and Notification Service boundaries.",
            "recommendations": ["Keep flow implementation inside its owning component.", "Keep integration tests isolated per run."],
        },
    }


def _phase13_component(user_text: str) -> dict:
    payload = _last_json_object(user_text)
    return {
        "summary": "Deterministic component ownership proposals.",
        "blockers": [],
        "components": [
            {
                "component_id": item["id"],
                "summary": f"Owns the approved {item['name']} implementation and integration boundary.",
                "responsibilities": item["definition"],
                "boundaries": f"Do not implement responsibilities owned by another component; canonical definition remains in the approved TDD ({item['id']}).",
                "dependencies": "Use only the dependencies in the approved TDD and story graph; no additional dependencies are introduced.",
            }
            for item in payload.get("components", [])
        ],
    }


def _phase13_testing(user_text: str) -> dict:
    payload = _last_json_object(user_text)
    baseline = payload.get("baseline", payload)
    flows = baseline.get("flows", [])
    traceability = baseline.get("tdd_traceability", [])
    flow_ids = {item["id"] for item in flows}
    components = sorted({
        item["component_id"] for item in traceability if item.get("flow_id") in flow_ids
    })
    use_cases = sorted({item["use_case_id"] for item in flows})
    return {
        "summary": "One Testcontainers phase covers all approved success, alternate, exception, boundary and business-rule journeys.",
        "blockers": [],
        "mock_policy": "Do not mock application external dependencies for routine verification; run against Testcontainers-backed real services.",
        "phases": [{
            "key": "order-intake-journeys",
            "test_scope": "End-to-end Order Intake journeys, including payment decline, empty basket and already-shipped cancellation cases.",
            "use_case_ids": use_cases,
            "flow_ids": sorted(flow_ids),
            "component_ids": components,
            "dependency_mode": "TESTCONTAINERS",
            "environment": "Testcontainers PostgreSQL and payment-adapter container; isolated network and per-run test data.",
            "execution_command": "pytest -m integration tests/integration/test_order_intake.py",
            "evidence": ".opencode/dridh/evidence/phase13-order-intake-tests.json",
            "service_inventory": "PostgreSQL 16 container; payment-adapter simulator container; Order Service and Notification Service.",
            "parallel_isolation": "Use a unique database/schema per run, unique container network, seeded data and deterministic cleanup.",
            "fallback_rationale": "",
        }],
    }


def decide(request: dict) -> dict:
    messages = request.get("messages", [])
    if not request.get("tools"):
        return {"content": "Dridh E2E session"}

    system = " ".join(_text(m) for m in messages if m.get("role") == "system")
    role = _role(system)
    last_index = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    user_text = _text(messages[last_index]) if messages else ""
    results = [m for m in messages[last_index + 1 :] if m.get("role") == "tool"]
    step = len(results)
    last_result = _text(results[-1]) if results else ""
    created_wf = next((WF_ID.search(_text(m)).group(0) for m in results if WF_ID.search(_text(m))), None)
    _state["last_result"] = last_result[:300]

    if role == "dridh/workers/planner-indexer":
        return {"content": json.dumps(_phase13_indexer(user_text))}
    if role == "dridh/workers/planner-component":
        return {"content": json.dumps(_phase13_component(user_text))}
    if role == "dridh/workers/planner-testing":
        return {"content": json.dumps(_phase13_testing(user_text))}
    if role == "dridh/planner":
        return {"content": json.dumps(PLAN)}

    if role == "dridh/ideator":
        if not results:
            return {"content": json.dumps({"clarifications": ["What format?"], "alternatives": ["JSON", "CSV"], "assumptions": ["UTF-8 encoding"]})}
        return {"content": json.dumps({"status": "DONE", "summary": "Ideation complete", "files_changed": []})}

    if role == "dridh/designer":
        if not results:
            return {"content": json.dumps({"components": ["greeter", "tests"], "interfaces": ["greet(name) -> str"], "decisions": ["Use f-string"]})}
        return {"content": json.dumps({"status": "DONE", "summary": "Design complete", "files_changed": []})}

    if role == "dridh/documents":
        if not results:
            return {"content": json.dumps({"documents": ["README.md"], "consistency": "OK"})}
        return {"content": json.dumps({"status": "DONE", "summary": "Documentation complete", "files_changed": []})}

    if role in ("dridh/code-reacher", "dridh/executor"):
        probe_tool = re.search(r"PROBE_TOOL:([a-z_]+)", user_text)
        if probe_tool:
            probe_args = {
                "skill": {"id": "effect"},
                "opencode_worktree_remove": {"directory": (re.search(r"PROBE_WORKTREE:([^\s\"']+)", user_text) or [None, os.environ.get("PROBE_WORKTREE", "/nonexistent-dridh-probe")])[1]},
                "write": {"path": ".opencode/probe-write.txt", "content": "probe"},
            }
            if not results:
                return _tool_call(probe_tool.group(1), probe_args.get(probe_tool.group(1), {}), "call_probe_tool_1")
            return {"content": "PROBE_WORKER_REPLY"}
        if "PROBE_EXECUTE" in user_text:
            if not results:
                return _execute(EXECUTE_STEPS["ping"], "call_probe_exec_1")
            return {"content": "PROBE_WORKER_REPLY"}
        task = "T-002" if "T-002" in user_text else "T-001"
        if not results:
            delay = float(_control().get("code_reacher_delay", 0))
            if delay:
                time.sleep(delay)
            if task == "T-001":
                return _tool_call("write", {"path": "src/greeter.py", "content": GREETER}, "call_write_1")
            return _tool_call("write", {"path": "tests/test_greeter.py", "content": TEST_GREETER}, "call_write_1")
        files = ["src/greeter.py"] if task == "T-001" else ["tests/test_greeter.py"]
        return {"content": json.dumps({"status": "DONE", "summary": f"{task} written", "files_changed": files})}

    if role == "dridh/coordinator":
        probe_code = re.search(r"PROBE_CODE:(.*)", user_text, re.S)
        if probe_code:
            if not results:
                return _execute(probe_code.group(1).strip(), "call_probe_code_1")
            return {"content": "PROBE_CODE_REPLY"}
        if "PROBE_RUNTIME" in user_text:
            if step == 0:
                return _execute(EXECUTE_STEPS["ping"], "call_probe_ping_1")
            if step == 1:
                return _execute(EXECUTE_STEPS["info"], "call_probe_info_1")
            return {"content": "RUNTIME_PROBE_DONE"}

        if "Continue workflow" in user_text:
            wf = WF_ID.search(user_text).group(0)
            if not results:
                return _execute(EXECUTE_STEPS["execute"] % {"wf": wf}, "call_exec_1")
            if '"report_path"' in last_result:
                return {"content": f"WORKFLOW_REPORTED {wf}"}
            outcome = OUTCOME.search(last_result)
            state = outcome.group(1) if outcome else "UNKNOWN"
            if state == "DONE":
                return _execute(EXECUTE_STEPS["execute"] % {"wf": wf}, f"call_exec_{step + 1}")
            if state in ("COMPLETED", "FAILED", "CANCELLED"):
                return _execute(EXECUTE_STEPS["report"] % {"wf": wf}, f"call_report_{step + 1}")
            return {"content": f"WORKFLOW_RESULT {state} {wf}"}

        objective = user_text.split("Objective:", 1)[-1].strip().strip('"').strip() or "Create a greeting function with tests"
        objective_literal = json.dumps(objective)
        wf = created_wf or "wf-000000000000"
        if step == 0:
            return _execute(EXECUTE_STEPS["ping"], "call_ping_1")
        if step == 1:
            return _execute(EXECUTE_STEPS["create"] % {"objective": objective_literal}, "call_create_1")
        if step == 2:
            return _execute(EXECUTE_STEPS["plan"] % {"wf": wf}, "call_plan_1")
        if step == 3:
            return _execute(EXECUTE_STEPS["execute_unapproved"] % {"wf": wf}, "call_refused_1")
        return {"content": f"PLAN_AWAITING_APPROVAL {wf}"}

    return {"content": "NO_SCRIPT_FOR_ROLE"}


def _chunks(model: str, action: dict) -> list[dict]:
    base = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": 0, "model": model}
    if "tool_calls" in action:
        calls = [
            {
                "index": index,
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for index, call in enumerate(action["tool_calls"])
        ]
        return [
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": calls}, "finish_reason": None}]),
            dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
        ]
    return [
        dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": action["content"]}, "finish_reason": None}]),
        dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "scripted", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(raw or b"{}")
        action = decide(request)
        system = " ".join(_text(m) for m in request.get("messages", []) if m.get("role") == "system")
        users = [m for m in request.get("messages", []) if m.get("role") == "user"]
        _log(
            {
                "role": _role(system) if request.get("tools") else "title",
                "model": request.get("model"),
                "tools": sorted(_tool_names(request)),
                "action": "tool_calls" if "tool_calls" in action else "content",
                "tool_names": [call["name"] for call in action.get("tool_calls", [])],
                "user_head": (_text(users[-1]) if users else "")[:160],
                "last_result_head": _state.get("last_result", ""),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in _chunks(request.get("model", "scripted"), action):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
