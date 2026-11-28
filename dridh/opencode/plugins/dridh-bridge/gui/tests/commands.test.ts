// Which commands the desktop panel may run, and how replies cross from the main process to the window.
import assert from "node:assert/strict"
import { test } from "node:test"
import { CONFIRM, failure, permitted, pythonFromShebang, READ, readReply, RECORD, success } from "../src/commands.ts"

test("views, records and decisions are each limited to their own commands", () => {
  assert.ok(permitted(READ, ["ui", "state"]))
  assert.ok(permitted(READ, ["ui", "document", "design"]))
  assert.ok(permitted(RECORD, ["reviews", "checklist", "r-1", "An item", "--checked"]))
  assert.ok(permitted(CONFIRM, ["designer", "approve", "designer-1", "--reviewer", "Ada"]))
  // A view is not a record, a record is not a decision, and nothing else is listed.
  assert.ok(!permitted(READ, ["reviews", "checklist", "r-1", "x", "--checked"]))
  assert.ok(!permitted(RECORD, ["designer", "approve", "designer-1"]))
  for (const args of [["designer", "generate", "s"], ["designer", "gate", "s"], ["ledger", "draft"], ["validate", "tdd"], ["init"], ["ui"]]) {
    assert.ok(![READ, RECORD, CONFIRM].some((list) => permitted(list, args)), args.join(" "))
  }
})

test("the panel cannot choose the project or the output format", () => {
  assert.ok(!permitted(READ, ["ui", "state", "--project", "/elsewhere"]))
  assert.ok(!permitted(READ, ["ui", "state", "--workspace=/elsewhere"]))
  assert.ok(!permitted(CONFIRM, ["designer", "approve", "s", "--json"]))
  assert.ok(!permitted(READ, ["ui", 1 as unknown as string]))
})

test("replies carry data or Dridh's own error, and a broken reply is a failure", () => {
  assert.deepEqual(readReply(success({ a: 1 })), { ok: true, data: { a: 1 } })
  const error = Object.assign(new Error("The confirmation did not match."), { code: "APPROVAL_REQUIRED", recovery: "Type it again." })
  assert.deepEqual(readReply(failure(error)), { ok: false, error: { message: "The confirmation did not match.", code: "APPROVAL_REQUIRED", recovery: "Type it again." } })
  assert.equal(readReply("not json").ok, false)
  assert.equal(readReply(JSON.stringify({ data: 1 })).ok, false)
})

test("the Python that runs dridh is read from its first line", () => {
  assert.deepEqual(pythonFromShebang("#!/home/a/.local/share/uv/tools/dridh/bin/python3"), ["/home/a/.local/share/uv/tools/dridh/bin/python3"])
  assert.deepEqual(pythonFromShebang("#!/usr/bin/env python3.13"), ["python3.13"])
  assert.equal(pythonFromShebang("#!/bin/sh"), undefined)
  assert.equal(pythonFromShebang("\x7fELF"), undefined)
})
