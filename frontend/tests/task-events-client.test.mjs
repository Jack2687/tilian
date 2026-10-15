import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8")
  .replace("import.meta.env.VITE_API_BASE", '"/api/v1"');
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function client(fetch) {
  const exports = {};
  vm.runInNewContext(code, {
    exports, fetch, Headers, URLSearchParams,
    sessionStorage: { getItem: () => "test-token" },
  });
  return exports;
}

test("event pages ignore SSE end markers and preserve cursor and credential headers", async () => {
  const event = { eventId: "event-201", type: "task.item.completed.v1", payload: { error: "fixture failure" } };
  const { api } = client(async (url, { headers }) => {
    assert.equal(url, "/api/v1/tasks/task-1/events?limit=200&afterEventId=event-200");
    assert.equal(headers.get("X-Tilian-CDK"), "test-card");
    assert.equal(headers.get("Authorization"), "Bearer test-token");
    return { ok: true, text: async () => `data: ${JSON.stringify(event)}\r\n\r\nevent: end\r\ndata: {}\r\n\r\n` };
  });
  const events = await api.taskEvents("task-1", "test-card", "event-200");
  assert.equal(events.length, 1);
  assert.equal(events[0].eventId, event.eventId);
  assert.equal(events[0].payload.error, "fixture failure");
});

test("empty event streams remain empty and failed fetches reject", async () => {
  const empty = client(async () => ({ ok: true, text: async () => "event: end\ndata: {}\n\n" }));
  assert.equal((await empty.api.taskEvents("task-1", "test-card")).length, 0);
  const failed = client(async () => ({ ok: false, status: 403 }));
  await assert.rejects(failed.api.taskEvents("task-1", "test-card"), (error) => error.status === 403);
});
