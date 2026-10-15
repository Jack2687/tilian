import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const descriptor = parse(readFileSync(new URL("../src/App.vue", import.meta.url), "utf8")).descriptor;
const code = ts.transpileModule(`${descriptor.scriptSetup.content}\nexport const state = {
  tasks, cardCode, expandedTaskItems, itemDetailsOpen, toggleItemDetails,
  itemStageDetail, stageLabel, eventTypeLabel, refreshTasks,
  eventsOpen, eventsLoading, eventsError, taskEvents, eventItemIndex,
  visibleTaskEvents, openEvents, refreshTaskEvents, closeEvents,
};`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function task(id = "task-a", items = []) {
  return { id, mode: "upi", status: "running", items, createdAt: "2026-01-01T00:00:00Z" };
}

function event(number, itemIndex = 0) {
  return {
    eventId: String(number), type: "task.item.progress.v1", itemIndex,
    payload: { stage: "engine_execute", detail: `Step ${number}` },
    createdAt: "2026-01-01T00:00:00Z",
  };
}

function eventIds(events) {
  return Array.from(events, (entry) => entry.eventId);
}

function harness(t) {
  const storage = new Map([["tilian:cdk", "test-card"]]);
  const timers = new Map();
  const unmounted = [];
  const requests = [];
  let timerId = 0;
  let eventResponse = async () => [];
  let taskResponse = [];
  const api = {
    taskEvents: (id, cdk, afterEventId = "") => {
      requests.push({ id, cdk, afterEventId });
      return eventResponse(id, cdk, afterEventId);
    },
    listTasks: async () => taskResponse,
  };
  class ApiError extends Error {}
  const eventTarget = { addEventListener() {}, removeEventListener() {} };
  const window = {
    ...eventTarget,
    location: { pathname: "/" },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay, interval: false }); return id; },
    setInterval: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay, interval: true }); return id; },
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
  };
  const scope = vue.effectScope();
  const exports = {};
  scope.run(() => vm.runInNewContext(code, {
    exports, window, Error, document: { ...eventTarget, visibilityState: "visible" },
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    require: (name) => {
      if (name === "vue") return { ...vue, onMounted() {}, onBeforeUnmount: (callback) => unmounted.push(callback) };
      if (name === "./api/client") return { api, ApiError, getAuthToken: () => "", setAuthToken() {} };
      if (name === "./runtimeSettings") return { RUNTIME_SETTINGS_CHANGED_KEY: "tilian:runtime-settings-changed" };
      throw new Error(`Unexpected script import: ${name}`);
    },
  }));
  t.after(() => { for (const callback of unmounted) callback(); scope.stop(); });
  return {
    state: exports.state, requests, timers,
    respond: (handler) => { eventResponse = handler; },
    nextTasks: (value) => { taskResponse = value; },
  };
}

test("failed and timed-out details can repeatedly close and reopen", (t) => {
  const { state } = harness(t);
  for (const [index, status] of ["failed", "timed_out"].entries()) {
    const item = { index, status };
    assert.equal(state.itemDetailsOpen("task-a", item), true);
    for (let repeat = 0; repeat < 2; repeat++) {
      state.toggleItemDetails("task-a", item);
      assert.equal(state.itemDetailsOpen("task-a", item), false);
      state.toggleItemDetails("task-a", item);
      assert.equal(state.itemDetailsOpen("task-a", item), true);
    }
  }
  const successful = { index: 2, status: "success" };
  assert.equal(state.itemDetailsOpen("task-a", successful), false);
  state.toggleItemDetails("task-a", successful);
  assert.equal(state.itemDetailsOpen("task-a", successful), true);
});

test("task refresh preserves a manually closed failure and isolates account choices", async (t) => {
  const app = harness(t);
  const failed = { index: 0, status: "failed", error: "Example failure" };
  app.state.toggleItemDetails("task-a", failed);
  app.nextTasks([task("task-a", [{ ...failed, stageDetail: "Updated detail" }])]);
  await app.state.refreshTasks();
  assert.equal(app.state.itemDetailsOpen("task-a", app.state.tasks.value[0].items[0]), false);
  assert.equal(app.state.itemDetailsOpen("task-a", { ...failed, index: 1 }), true);
  assert.equal(app.state.itemDetailsOpen("task-b", failed), true);
});

test("terminal stage details never fall back to the queued message", (t) => {
  const { state } = harness(t);
  for (const status of ["failed", "timed_out", "canceled", "success", "plus_success"]) {
    const detail = state.itemStageDetail({ index: 0, status });
    assert.ok(detail);
    assert.doesNotMatch(detail, /等待 Worker/);
  }
  assert.equal(state.itemStageDetail({ index: 0, status: "failed", stageDetail: "Server detail", error: "Error" }), "Server detail");
  assert.equal(state.itemStageDetail({ index: 0, status: "failed", error: "Error" }), "Error");
  assert.match(state.itemStageDetail({ index: 0, status: "queued" }), /等待 Worker/);
  assert.equal(state.stageLabel("engine_bootstrap"), "引擎启动失败");
  assert.equal(state.stageLabel("bank_authorization"), "等待银行授权");
  assert.equal(state.eventTypeLabel("task.item.progress.v1"), "阶段更新");
  assert.equal(state.eventTypeLabel("task.canceled.v1"), "任务已取消");
});

test("normal account details do not use unconditional failure headings", () => {
  const template = descriptor.template.content;
  assert.match(template, /v-if="isFailedTaskItem\(item\)"[^>]*class="task-error-title"/);
  assert.match(template, /isFailedTaskItem\(item\)\s*\?\s*'失败阶段'\s*:\s*'当前阶段'/);
  assert.match(template, /@click="toggleItemDetails\(task\.id, item\)"/);
  assert.match(template, /@click="openEvents\(task, item\.index\)"/);
});

test("event pages read past 200 records and refresh incrementally without duplicates", async (t) => {
  const app = harness(t);
  const firstPage = Array.from({ length: 200 }, (_, index) => event(index + 1));
  app.respond(async (_id, _cdk, cursor) => {
    if (!cursor) return firstPage;
    if (cursor === "200") return [event(200), event(201)];
    if (cursor === "201") return [event(201), event(202)];
    throw new Error(`Unexpected cursor: ${cursor}`);
  });
  await app.state.openEvents(task());
  assert.equal(app.state.taskEvents.value.length, 201);
  assert.deepEqual(app.requests.map((entry) => entry.afterEventId), ["", "200"]);
  assert.ok(app.requests.every((entry) => entry.cdk === "test-card"));
  await app.state.refreshTaskEvents();
  assert.equal(app.requests.at(-1).afterEventId, "201");
  assert.equal(app.state.taskEvents.value.length, 202);
  assert.equal(app.state.taskEvents.value.at(-1).eventId, "202");
});

test("duplicate event IDs within one page are only displayed once", async (t) => {
  const app = harness(t);
  app.respond(async () => [event(1), event(1), event(2)]);
  await app.state.openEvents(task());
  assert.deepEqual(eventIds(app.state.taskEvents.value), ["1", "2"]);
});

test("account event filter keeps task-level context and can return to all accounts", async (t) => {
  const app = harness(t);
  const taskLevel = { ...event(1), type: "task.created.v1" };
  delete taskLevel.itemIndex;
  app.respond(async () => [taskLevel, event(2, 0), event(3, 1), event(4, null)]);
  await app.state.openEvents(task(), 1);
  assert.deepEqual(eventIds(app.state.visibleTaskEvents.value), ["1", "3", "4"]);
  app.state.eventItemIndex.value = 0;
  assert.deepEqual(eventIds(app.state.visibleTaskEvents.value), ["1", "2", "4"]);
  app.state.eventItemIndex.value = null;
  assert.deepEqual(eventIds(app.state.visibleTaskEvents.value), ["1", "2", "3", "4"]);
});

test("switching event dialogs ignores the previous task's delayed response", async (t) => {
  const app = harness(t);
  const oldResponse = deferred();
  const newResponse = deferred();
  app.respond((id) => id === "task-a" ? oldResponse.promise : newResponse.promise);
  const oldOpen = app.state.openEvents(task("task-a"));
  const newOpen = app.state.openEvents(task("task-b"), 1);
  oldResponse.resolve([event(1)]);
  await oldOpen;
  assert.equal(app.state.eventsOpen.value, "task-b");
  assert.equal(app.state.eventsLoading.value, true, "old request cannot finish the new request's loading state");
  assert.deepEqual(eventIds(app.state.taskEvents.value), []);
  newResponse.resolve([event(2, 1)]);
  await newOpen;
  assert.deepEqual(eventIds(app.state.taskEvents.value), ["2"]);
  assert.equal(app.state.eventsLoading.value, false);
});

test("closing event details discards pending data and stops polling", async (t) => {
  const app = harness(t);
  const pending = deferred();
  app.respond(() => pending.promise);
  const opened = app.state.openEvents(task());
  assert.equal([...app.timers.values()].filter((timer) => timer.interval).length, 1);
  app.state.closeEvents();
  pending.resolve([event(1)]);
  await opened;
  assert.equal(app.state.eventsOpen.value, null);
  assert.equal(app.state.eventsLoading.value, false);
  assert.equal(app.state.eventsError.value, "");
  assert.deepEqual(eventIds(app.state.taskEvents.value), []);
  assert.equal([...app.timers.values()].filter((timer) => timer.interval).length, 0);
  await app.state.refreshTaskEvents();
  assert.equal(app.requests.length, 1);
});

test("a stale event error cannot replace another task's successful events", async (t) => {
  const app = harness(t);
  const oldResponse = deferred();
  app.respond((id) => id === "task-a" ? oldResponse.promise : Promise.resolve([event(2)]));
  const oldOpen = app.state.openEvents(task("task-a"));
  await app.state.openEvents(task("task-b"));
  oldResponse.reject(new Error("Old request failed"));
  await oldOpen;
  assert.equal(app.state.eventsOpen.value, "task-b");
  assert.equal(app.state.eventsError.value, "");
  assert.deepEqual(eventIds(app.state.taskEvents.value), ["2"]);
});

test("overlapping event refreshes share the active request and errors can recover", async (t) => {
  const app = harness(t);
  const pending = deferred();
  app.respond(() => pending.promise);
  const opened = app.state.openEvents(task());
  await app.state.refreshTaskEvents();
  assert.equal(app.requests.length, 1);
  pending.reject(new Error("Temporary event failure"));
  await opened;
  assert.equal(app.state.eventsError.value, "Temporary event failure");
  assert.equal(app.state.eventsLoading.value, false);
  app.respond(async () => [event(1)]);
  await app.state.refreshTaskEvents();
  assert.equal(app.state.eventsError.value, "");
  assert.deepEqual(eventIds(app.state.taskEvents.value), ["1"]);
});
