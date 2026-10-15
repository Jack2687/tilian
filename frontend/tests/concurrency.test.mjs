import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const settingsKey = "tilian:runtime-settings-changed";
const source = parse(readFileSync(new URL("../src/App.vue", import.meta.url), "utf8")).descriptor.scriptSetup.content;
const code = ts.transpileModule(`${source}\nexport const state = {
  concurrency, concurrencyMax, currentUser, cardState, meta, selectedMode,
  initializedConcurrencyModes, refreshSettings, loadOverview, selectMode, logoutUser,
};`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;

function metadata(limit, defaultConcurrency = limit) {
  return { app: "test", version: "test", modes: [], serverTime: Date.now(),
    engine: { defaultConcurrency, maxConcurrency: 100 },
    limits: { userConcurrency: limit, maxConcurrency: limit } };
}

function harness(t, { draft, limit = 10 } = {}) {
  const storage = new Map([["tilian:user-token", "test-token"]]);
  if (draft) storage.set("tilian:workspace-draft:v1", JSON.stringify(draft));
  const events = new Map();
  const timers = new Map();
  const mounted = [];
  const unmounted = [];
  let timerId = 0;
  let token = "test-token";
  let response = metadata(limit);
  let metaCalls = 0;
  const user = { id: 1, username: "test", displayName: "Test", role: "user", status: "active", taskConcurrency: 3, maxAccountsPerTask: 100, allowFusion: true, createdAt: "2026-01-01" };
  const api = {
    me: async () => ({ user }),
    meta: async () => { metaCalls++; return typeof response === "function" ? response() : response; },
    stats: async () => ({}),
    recentSuccess: async () => ({}),
    announcement: async () => ({ announcement: null }),
    listTasks: async () => [],
  };
  class ApiError extends Error {}
  const sessionStorage = { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const eventTarget = {
    addEventListener: (name, callback) => events.set(name, callback),
    removeEventListener: (name) => events.delete(name),
  };
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
    exports, sessionStorage, window, document: { ...eventTarget, visibilityState: "visible" },
    require: (name) => {
      if (name === "vue") return { ...vue, onMounted: (callback) => mounted.push(callback), onBeforeUnmount: (callback) => unmounted.push(callback) };
      if (name === "./api/client") return { api, ApiError, getAuthToken: () => token, setAuthToken: (value) => { token = value; } };
      if (name === "./runtimeSettings") return { RUNTIME_SETTINGS_CHANGED_KEY: settingsKey };
      throw new Error(`Unexpected script import: ${name}`);
    },
  }));
  t.after(() => { for (const callback of unmounted) callback(); scope.stop(); });
  return {
    state: exports.state, timers, events,
    mount: async () => { for (const callback of mounted) await callback(); await vue.nextTick(); },
    setResponse: (value) => { response = value; },
    calls: () => metaCalls,
    choose: async (value) => {
      exports.state.initializedConcurrencyModes.add(exports.state.selectedMode.value);
      exports.state.concurrency.value = value;
      await vue.nextTick();
    },
    poll: async () => {
      const timer = [...timers.values()].find((value) => value.interval && value.delay === 5000);
      assert.ok(timer, "settings poll must be installed");
      timer.callback();
      await new Promise(setImmediate);
    },
  };
}

test("periodic refresh replaces stale user/card limits and preserves a smaller selection", async (t) => {
  const app = harness(t, { limit: 3 });
  await app.mount();
  app.state.cardState.value = { remain: 10, status: "active", maxConcurrency: 3 };
  await app.choose(2);
  app.setResponse(metadata(10));
  await app.poll();
  assert.equal(app.state.currentUser.value.taskConcurrency, 3, "fixture retains the old login limit");
  assert.equal(app.state.concurrencyMax.value, 10);
  assert.equal(app.state.concurrency.value, 2);
});

test("lowering the configured limit clamps the selected value", async (t) => {
  const app = harness(t);
  await app.mount();
  await app.choose(8);
  app.setResponse(metadata(4));
  await app.poll();
  assert.equal(app.state.concurrencyMax.value, 4);
  assert.equal(app.state.concurrency.value, 4);
});

test("refresh and channel changes preserve every saved channel's concurrency", async (t) => {
  const app = harness(t, { draft: { selectedMode: "upi", channels: { upi: { concurrency: 2 }, paypal: { concurrency: 5 } } } });
  await app.mount();
  assert.equal(app.state.concurrency.value, 2);
  await app.state.loadOverview();
  assert.equal(app.state.concurrency.value, 2);
  app.state.selectMode("paypal");
  await vue.nextTick();
  assert.equal(app.state.concurrency.value, 5);
  app.state.selectMode("upi");
  await vue.nextTick();
  assert.equal(app.state.concurrency.value, 2);
  app.state.selectMode("gopay");
  await vue.nextTick();
  assert.equal(app.state.concurrency.value, 10, "new channels use the latest default");
});

test("admin change and focus events refresh immediately without overlapping requests", async (t) => {
  const app = harness(t, { limit: 3 });
  await app.mount();
  let release;
  app.setResponse(() => new Promise((resolve) => { release = resolve; }));
  app.events.get("storage")({ key: settingsKey });
  app.events.get("focus")();
  app.events.get("visibilitychange")();
  assert.equal(app.calls(), 2, "initial request plus one shared refresh");
  release(metadata(12));
  await new Promise(setImmediate);
  assert.equal(app.state.concurrencyMax.value, 12);
});

test("logging out stops polling and ignores an in-flight settings response", async (t) => {
  const app = harness(t);
  await app.mount();
  let release;
  app.setResponse(() => new Promise((resolve) => { release = resolve; }));
  const refresh = app.state.refreshSettings();
  app.state.logoutUser();
  release(metadata(20));
  await refresh;
  assert.equal(app.state.meta.value, null);
  assert.equal([...app.timers.values()].filter((timer) => timer.interval).length, 0);
});
