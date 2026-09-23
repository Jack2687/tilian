#!/usr/bin/env node
/**
 * sentinel_bridge.js —— 用真·sdk.js（Node/V8 运行）生成 create_account 需要的
 * openai-sentinel-token（真 t）+ openai-sentinel-so-token（真 so 设备指纹）。
 *
 * 纯 Python/httpx 拿不到 so-token（它由混淆 sdk.js 采集浏览器传感器算出），
 * 这里在 Node 里铺好浏览器 shim（sentinel_bootstrap.js）+ 加载真 sdk.js，
 * 调 SentinelSDK.__proto2(flow) 拿 {t, so, c, seed, diff, powReq}，
 * Node 侧解 PoW 拼装两个 header 值，JSON 打到 stdout。
 *
 * 用法：把 {ua, cores, deviceId, flow, proxy, version} 以 JSON 从 stdin 传入。
 *
 * UPI 私有副本（2026-09 对齐印度 UPI.har）：
 *   - deployId → 指纹数组 [6] "prod-<deployId>"（与部署证明 payload 同源）；
 *   - pageUrl 查询参数名 → [15]；
 *   - [5] 改为 chatgpt.com 承载的 sentinel sdk 地址；
 *   - [1] 使用页面语言下的时区长名；[8] 与 language 同值；[14] 由 device 派生稳定；
 *   - [10][11][12] 每次生成随机探针（浏览器行为）。
 *   SENTINEL_SELFTEST=1 时只打印指纹数组（不联网），用于与 HAR 比对。
 * 该文件只服务 UPI 引擎；GCash/GoPay 使用各自 engine 下的资产。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Resolve the Python used by the project before falling back to PATH. The
// proxy transport helper is launched from this Node process, and Windows
// installations commonly have only the project venv (no `python.exe` PATH
// entry).
function resolvePython() {
  const configured = String(process.env.PYTHON_BIN || process.env.SENTINEL_PYTHON || "").trim();
  if (configured) return configured;
  const projectRoot = path.resolve(__dirname, "..", "..");
  const local = process.platform === "win32"
    ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
    : path.join(projectRoot, ".venv", "bin", "python");
  if (fs.existsSync(local)) return local;
  return process.platform === "win32" ? "python" : "python3";
}

function readStdin() {
  return new Promise((resolve) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (d) => (buf += d));
    process.stdin.on("end", () => resolve(buf));
  });
}

// ---- PoW（对齐 sentinel.go：configArray(answer=true) + fnv1a32 + b64） ----
function fnv1a32(buf) {
  let h = 0x811c9dc5 >>> 0;
  for (let i = 0; i < buf.length; i++) {
    h ^= buf[i];
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  h ^= h >>> 16;
  h = Math.imul(h, 2246822507) >>> 0;
  h ^= h >>> 13;
  h = Math.imul(h, 3266489909) >>> 0;
  h ^= h >>> 16;
  return h >>> 0;
}

function b64Compact(v) {
  return Buffer.from(JSON.stringify(v), "utf8").toString("base64");
}

function uuidv4() {
  const b = require("crypto").randomBytes(16);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => (x + 0x100).toString(16).slice(1));
  return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
}

// ── UPI 专用对齐补充（对齐 2026-09-17 印度 UPI.har 的 p 数组形态）────────
// 说明：本文件是 UPI 引擎私有副本，GCash/GoPay 仍使用各自 engine 下的资产，
// 修改这里不会影响其它渠道。
function sha256Bytes(text) {
  return require("crypto").createHash("sha256").update(String(text), "utf8").digest();
}

function bytesToUuid(b) {
  const h = [...b].map((x) => (x + 0x100).toString(16).slice(1));
  return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
}

// HAR #5/#12/#228：同一浏览器会话的 [14] 恒为同一个 uuid → 由 device 派生，跨 mint 稳定。
function stableUuid(seedText) {
  const b = Buffer.from(sha256Bytes("upi-sid|" + String(seedText || "")).subarray(0, 16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  return bytesToUuid(b);
}

// HAR #5/#12/#228：探针项 [10][11][12] 在同一个页面上每次生成都不同 → 每次 mint 随机。
const PROBE_NAV = [
  ["getBattery", "function getBattery() { [native code] }"],
  ["webkitTemporaryStorage", "[object DeprecatedStorageQuota]"],
  ["adAuctionComponents", "function adAuctionComponents() { [native code] }"],
  ["mediaSession", "[object MediaSession]"],
  ["credentials", "[object CredentialsContainer]"],
  ["storage", "[object StorageManager]"],
  ["keyboard", "[object Keyboard]"],
  ["geolocation", "[object Geolocation]"],
];
const PROBE_WINDOW = [
  "location", "resizeBy", "scrollTo", "print", "open", "stop", "find",
  "_reactListening", "hcaptcha-invisible", "__zod_globalRegistry",
];
const PROBE_EVENT = [
  "onbeforeunload", "onresize", "onfocus", "onblur", "ononline", "onoffline",
  "onvisibilitychange", "__oai_so_sy0",
];

function pickProbe() {
  const pick = (rows) => rows[Math.floor(Math.random() * rows.length) % rows.length];
  const nav = pick(PROBE_NAV);
  return {
    probeNav: nav[0] + SEP + nav[1],
    reactKey: pick(PROBE_WINDOW),
    eventName: pick(PROBE_EVENT),
  };
}

// HAR #1 的 [1] 形如 "Thu Sep 17 2026 02:39:26 GMT+0530 (भारतीय मानक समय)"：
// 括号里是"页面语言下的时区长名"，不是 IANA 名。Node 有 full-icu 时可直接取到。
function timezoneLabel(language, timezone) {
  try {
    const parts = new Intl.DateTimeFormat(language || "en-US", {
      timeZone: timezone,
      timeZoneName: "long",
    }).formatToParts(new Date());
    const hit = parts.find((part) => part.type === "timeZoneName");
    if (hit && hit.value) return hit.value;
  } catch (_) {}
  return String(timezone || "");
}

const SEP = "\u2212"; // U+2212 MINUS SIGN
const ANS_SDK_URL = "https://sentinel.openai.com/backend-api/sentinel/sdk.js";

function makeGenerator(ua, cores, language, timezone, screenW, screenH, extra) {
  const opts = extra || {};
  const initialPerformance = 350.0 + Math.random() * 2200.0;
  const sw = Number(screenW) || 1920;
  const sh = Number(screenH) || 1080;
  const probe = pickProbe();
  return {
    ua,
    cores: cores || 16,
    language: language || "en-US",
    // HAR #5/#12/#228：navigator.languages 在该会话只有一项且与 language 同值。
    languages: [language || "en-US"],
    timezone: timezone || "America/Chicago",
    tzLabel: timezoneLabel(language, timezone || "America/Chicago"),
    // HAR：同一浏览器会话的 sid 全程不变 → 由 device 派生，跨 mint 稳定。
    sid: opts.deviceId ? stableUuid(opts.deviceId) : uuidv4(),
    heapLimit: 4395630592,
    // 与真实浏览器一致：screen.width + screen.height（指纹数组 [0]）
    screenNum: sw + sh,
    nine: 0,
    loadTs: Date.now() - initialPerformance,
    initialPerformance,
    startedAt: Date.now(),
    sentinelOrigin: opts.sentinelOrigin || "https://chatgpt.com",
    // HAR #12/#228 的 [6]：与 oai-web-deployment-attestation 的 deployId 同源。
    deployId: String(opts.deployId || "").trim(),
    // HAR #5/#228 的 [15]：页面 URL 上第一个查询参数名（如 promo_campaign）。
    pageParam: String(opts.pageParam || "").trim(),
    probeNav: probe.probeNav,
    reactKey: probe.reactKey,
    eventName: probe.eventName,
  };
}

function configArray(g) {
  const perfNow = g.initialPerformance + Math.max(0, Date.now() - g.startedAt);
  const now = new Date();
  let offsetMinutes = 0;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: g.timezone,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now).reduce((out, part) => {
      if (part.type !== "literal") out[part.type] = Number(part.value);
      return out;
    }, {});
    const localAsUtc = Date.UTC(
      parts.year, parts.month - 1, parts.day,
      parts.hour, parts.minute, parts.second
    );
    offsetMinutes = Math.round((localAsUtc - now.getTime()) / 60000);
  } catch (_) {}
  const d = new Date(now.getTime() + offsetMinutes * 60000);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
  const mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
  const p2 = (n) => (n < 10 ? "0" : "") + n;
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offset = Math.abs(offsetMinutes);
  const tstr =
    `${wd} ${mo} ${p2(d.getUTCDate())} ${d.getUTCFullYear()} ` +
    `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())} ` +
    `GMT${sign}${p2(Math.floor(offset / 60))}${p2(offset % 60)} (${g.tzLabel || g.timezone})`;
  return [
    g.screenNum, // 0
    tstr, // 1  括号内为页面语言下的时区长名（HAR #1）
    g.heapLimit, // 2
    0, // 3 PoW 计数器
    g.ua, // 4
    // HAR #12/#228：[5] 是 chatgpt.com 承载的 sentinel sdk 脚本地址
    (g.sentinelOrigin || "https://chatgpt.com") + "/backend-api/sentinel/sdk.js",
    // HAR #12/#228：[6] = "prod-<deployId>"，与部署证明 payload 的 deployId 同源
    g.deployId ? "prod-" + g.deployId : null,
    g.language, // 7
    g.languages.join(","), // 8
    g.nine, // 9
    g.probeNav, // 10
    g.reactKey, // 11
    g.eventName, // 12
    perfNow, // 13
    g.sid, // 14
    g.pageParam, // 15 HAR #5/#228：页面 URL 首个查询参数名
    g.cores, // 16
    g.loadTs, // 17
    0, 0, 0, 0, 0, 0, 0, // 18..24
  ];
}

const MAX_ATTEMPTS = 500000;
const ERR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D";

function solvePoW(g, seed, difficulty) {
  if (!difficulty) difficulty = "0";
  const data = configArray(g);
  const seedB = Buffer.from(seed, "ascii");
  const dl = difficulty.length;
  const startedAt = Date.now();
  for (let i = 0; i < MAX_ATTEMPTS; i++) {
    data[3] = i;
    data[9] = Math.round(Date.now() - startedAt);
    const payload = b64Compact(data);
    const h = fnv1a32(Buffer.concat([seedB, Buffer.from(payload, "ascii")]));
    const hex = h.toString(16).padStart(8, "0");
    const cmpLen = Math.min(dl, hex.length);
    if (hex.slice(0, cmpLen) <= difficulty.slice(0, cmpLen)) {
      return "gAAAAAB" + payload + "~S";
    }
  }
  return "gAAAAAB" + ERR_PREFIX + b64Compact(null) + "~S";
}

function makeFetch(ua, language, cookieHeader, dispatcher, fetchImpl, sentinelOrigin, frameUrl) {
  // sdk 里的 fetch 桥到 fetchImpl：改写 URL 到 sentinel.openai.com、补头、走代理。
  // 关键：dispatcher 与 fetchImpl 必须来自同一个 undici 版本，否则 Node 内置 fetch
  // 校验外部 dispatcher 的 Request handler 会报 "invalid onRequestStart method"。
  return function (url, opts) {
    opts = opts || {};
    let u = String(url);
    if (u.indexOf("/backend-api/sentinel/req") !== -1) {
      u = sentinelOrigin + "/backend-api/sentinel/req";
    } else if (u.charAt(0) === "/") {
      u = sentinelOrigin + u;
    }
    const headers = Object.assign(
      {
        "content-type": "text/plain;charset=UTF-8",
        origin: sentinelOrigin,
        referer: frameUrl,
        "user-agent": ua,
        accept: "*/*",
        "accept-language": `${language},${language.split("-")[0]};q=0.9,en;q=0.8`,
        ...(cookieHeader ? { cookie: cookieHeader } : {}),
      },
      opts.headers || {}
    );
    const fopts = { method: opts.method || "GET", headers };
    if (opts.body != null) fopts.body = opts.body;
    if (dispatcher) fopts.dispatcher = dispatcher;
    return fetchImpl(u, fopts);
  };
}

function makeCurlFetch(proxy) {
  const { spawn } = require("child_process");
  const marker = "\n__SENTINEL_HTTP_STATUS__:";
  return function curlFetch(url, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const args = [
        "-sS",
        "--compressed",
        "--connect-timeout", "30",
        "--max-time", "120",
        "--proxy", proxy,
        "-X", opts.method || "GET",
      ];
      Object.entries(opts.headers || {}).forEach(([name, value]) => {
        args.push("-H", `${name}: ${value}`);
      });
      if (opts.body != null) {
        args.push("--data-binary", String(opts.body));
      }
      args.push("-w", marker + "%{http_code}", String(url));
      const child = spawn(process.env.SENTINEL_CURL || "curl", args, {
        stdio: ["ignore", "pipe", "pipe"],
      });
      const stdout = [];
      const stderr = [];
      child.stdout.on("data", (chunk) => stdout.push(chunk));
      child.stderr.on("data", (chunk) => stderr.push(chunk));
      child.on("error", reject);
      child.on("close", (code) => {
        const raw = Buffer.concat(stdout).toString("utf8");
        const markerAt = raw.lastIndexOf(marker);
        const body = markerAt >= 0 ? raw.slice(0, markerAt) : raw;
        const status = markerAt >= 0 ? Number(raw.slice(markerAt + marker.length)) : 0;
        if (code !== 0) {
          reject(new Error(Buffer.concat(stderr).toString("utf8").slice(0, 500) || `curl exit ${code}`));
          return;
        }
        resolve({
          status,
          ok: status >= 200 && status < 300,
          headers: { get: () => null },
          text: async () => body,
          json: async () => JSON.parse(body),
        });
      });
    });
  };
}

// Windows 系统 curl 使用 Schannel；SOCKS5 代理下可能触发
// SEC_E_NO_CREDENTIALS。用项目已有的 curl_cffi/BoringSSL 传输替代。
function makePythonCurlFetch(proxy) {
  const { spawn } = require("child_process");
  const helper = path.join(__dirname, "sentinel_curl_fetch.py");
  const python = resolvePython();
  return function pythonCurlFetch(url, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const child = spawn(python, [helper], { stdio: ["pipe", "pipe", "pipe"] });
      const stdout = [];
      const stderr = [];
      child.stdout.on("data", (chunk) => stdout.push(chunk));
      child.stderr.on("data", (chunk) => stderr.push(chunk));
      child.on("error", reject);
      child.on("close", (code) => {
        if (code !== 0) {
          reject(new Error(Buffer.concat(stderr).toString("utf8").slice(0, 500) || `python curl exit ${code}`));
          return;
        }
        try {
          const result = JSON.parse(Buffer.concat(stdout).toString("utf8"));
          const status = Number(result.status || 0);
          const body = String(result.body || "");
          resolve({ status, ok: status >= 200 && status < 300,
            headers: { get: () => null }, text: async () => body,
            json: async () => JSON.parse(body) });
        } catch (error) { reject(error); }
      });
      child.stdin.end(JSON.stringify({ url: String(url), method: opts.method || "GET",
        headers: opts.headers || {}, body: opts.body == null ? null : String(opts.body),
        proxy, timeout: 120 }));
    });
  };
}

const DAEMON_MODE = String(process.env.SENTINEL_DAEMON_SOCKET || "").trim().length > 0;

async function main(preloadedRaw) {
  const raw = preloadedRaw === undefined ? await readStdin() : String(preloadedRaw || "");
  let args;
  try {
    args = JSON.parse(raw || "{}");
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "bad args json: " + e.message }));
    process.exit(1);
    return;
  }

  const ua = args.ua || "";
  const cores = args.cores || 16;
  const deviceId = args.deviceId || "";
  const flow = args.flow || "oauth_create_account";
  const proxy = args.proxy || "";
  const version = args.version || "20260219f9f6";
  const pageUrl = args.pageUrl || "https://auth.openai.com/about-you";
  const language = args.language || "en-US";
  const timezone = args.timezone || "America/Chicago";
  const cookieHeader = args.cookieHeader || "";
  const sentinelOrigin = args.sentinelOrigin || "https://chatgpt.com";
  // 指纹档案驱动（与请求 UA/平台/屏幕一致，避免哨兵身份与请求身份打架）
  const platform = args.platform || "Win32";
  const platformLabel = args.platformLabel || "Windows";
  const screenW = Number(args.screenW) || 1920;
  const screenH = Number(args.screenH) || 1080;
  const availH = Number(args.availH) || (screenH - 48);
  const maxTouch = args.maxTouchPoints ? 1 : 0;
  const frameUrl = `${sentinelOrigin}/backend-api/sentinel/frame.html?sv=${version}`;
  // HAR #12/#228 的 [6]：部署 id（由 oai-web-deployment-attestation 解出后传入）
  const deployId = String(args.deployId || "").trim();
  // HAR #5/#228 的 [15]：页面 URL 首个查询参数名，未显式给出时从 pageUrl 推导
  let pageParam = String(args.pageParam || "").trim();
  if (!pageParam) {
    try {
      const search = String(pageUrl).includes("?") ? new URL(pageUrl).searchParams : null;
      if (search) pageParam = String(search.keys().next().value || "");
    } catch (_) {}
  }

  // 离线自检：不联网、不加载 SDK，只打印指纹数组，便于与 HAR 逐位比对。
  if (String(process.env.SENTINEL_SELFTEST || "") === "1") {
    const g = makeGenerator(ua, cores, language, timezone, screenW, screenH, {
      deviceId,
      deployId,
      pageParam,
      sentinelOrigin,
    });
    process.stdout.write(JSON.stringify({ selfTest: true, configArray: configArray(g) }));
    return;
  }

  // fetch + ProxyAgent 必须同源（同一 undici 版本）。优先用 npm undici；
  // 无代理且 undici 缺失时退回 Node 内置全局 fetch。
  let undici = null;
  try {
    undici = require("undici");
  } catch (_) {}

  // 代理（undici ProxyAgent）——与注册同 IP 命中 /sentinel/req。
  // 注意：undici 不会自动解析 URL 里的 user:pass@，必须显式给 Proxy-Authorization token。
  let dispatcher = null;
  let fetchImpl = undici && undici.fetch ? undici.fetch : fetch;
  if (proxy) {
    if (undici && /^https?:/i.test(proxy)) {
      try {
        const pu = new URL(proxy);
        const opts = { uri: pu.origin }; // http://host:port（去掉 userinfo）
        if (pu.username || pu.password) {
          const auth = decodeURIComponent(pu.username) + ":" + decodeURIComponent(pu.password);
          opts.token = "Basic " + Buffer.from(auth, "utf8").toString("base64");
        }
        dispatcher = new undici.ProxyAgent(opts);
      } catch (e) {
        process.stdout.write(JSON.stringify({ error: "ProxyAgent 构造失败：" + ((e && e.message) || e) }));
        process.exit(1);
        return;
      }
    } else {
      // SOCKS5 + Windows Schannel curl 会触发 SEC_E_NO_CREDENTIALS；
      // 通过 curl_cffi 使用 BoringSSL，保留同一代理和请求头。
      fetchImpl = makePythonCurlFetch(proxy);
    }
  }

  const assetsDir = __dirname;
  const bootstrapSrc = fs.readFileSync(path.join(assetsDir, "sentinel_bootstrap.js"), "utf8");
  const sdkSrc = fs.readFileSync(path.join(assetsDir, "sentinel_sdk.js"), "utf8");

  // Node 的 crypto/navigator/performance/self 是只读全局，会和 bootstrap 的赋值冲突。
  // 用独立 vm 沙箱（全新 global），只注入 sdk 实际需要的 host 能力。
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    queueMicrotask,
    fetch: makeFetch(
      ua,
      language,
      cookieHeader,
      dispatcher,
      fetchImpl,
      sentinelOrigin,
      frameUrl
    ),
    TextEncoder,
    TextDecoder,
    btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
    atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
    crypto: globalThis.crypto, // webcrypto：subtle + getRandomValues + randomUUID
    URL,
    URLSearchParams,
    __UA__: ua,
    __CORES__: cores,
    __SDK_URL__: `${sentinelOrigin}/sentinel/${version}/sdk.js`,
    __SEED_DID_KEY__: "oai-did",
    __SEED_DID_VAL__: deviceId,
    __PAGE_URL__: pageUrl,
    __LANGUAGE__: language,
    __LANGUAGES__: [language, "en-US", "en"],
    __TIMEZONE__: timezone,
    __COOKIE_HEADER__: cookieHeader,
    __PLATFORM__: platform,
    __PLATFORM_LABEL__: platformLabel,
    __SCREEN_W__: screenW,
    __SCREEN_H__: screenH,
    __AVAIL_H__: availH,
    __MAX_TOUCH__: maxTouch,
  };
  vm.createContext(sandbox);

  try {
    vm.runInContext(bootstrapSrc, sandbox, { filename: "sentinel_bootstrap.js" });
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "bootstrap: " + ((e && e.stack) || e) }));
    process.exit(1);
    return;
  }
  try {
    vm.runInContext(sdkSrc + "\n;globalThis.__SENTINEL_SDK__ = SentinelSDK;", sandbox, { filename: "sentinel_sdk.js" });
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "sdk: " + ((e && e.stack) || e) }));
    process.exit(1);
    return;
  }

  const SDK = sandbox.__SENTINEL_SDK__;
  if (!SDK || typeof SDK.__proto2 !== "function") {
    process.stdout.write(JSON.stringify({ error: "sdk.__proto2 未定义" }));
    process.exit(1);
    return;
  }

  let rawDriver;
  try {
    const p = SDK.__proto2(flow);
    rawDriver = await Promise.resolve(p);
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "proto2: " + (e && e.stack || e) }));
    process.exit(1);
    return;
  }

  let rd;
  try {
    rd = JSON.parse(String(rawDriver));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: "driver decode: " + e.message, raw: String(rawDriver).slice(0, 300) }));
    process.exit(1);
    return;
  }

  if (!rd.c) {
    process.stdout.write(JSON.stringify({ error: "driver 无 c", soErr: rd.soErr || "", raw: String(rawDriver).slice(0, 300) }));
    process.exit(1);
    return;
  }

  const g = makeGenerator(ua, cores, language, timezone, screenW, screenH, {
    deviceId,
    deployId,
    pageParam,
    sentinelOrigin,
  });
  let p;
  if (rd.powReq && rd.seed) {
    p = solvePoW(g, rd.seed, rd.diff);
  } else {
    const data0 = configArray(g);
    data0[3] = 1;
    p = "gAAAAAB" + b64Compact(data0) + "~S";
  }

  const mainTok = JSON.stringify({ p, t: rd.t || "", c: rd.c, id: deviceId, flow });
  let so = "";
  if (rd.so) {
    so = JSON.stringify({ so: rd.so, c: rd.c, id: deviceId, flow });
  }

  const out = JSON.stringify({
    main: mainTok,
    so,
    soErr: rd.soErr || "",
    powReq: !!rd.powReq,
    hasT: !!rd.t,
    hasSo: !!rd.so,
  });

  // token 已写完，优雅关闭 undici 连接后自然退出，避开 Windows libuv 退出断言
  // （该断言发生在 stdout 写出之后，不影响结果；但会让子进程 exit code 变脏）。
  await new Promise((r) => process.stdout.write(out, r));
  try {
    const u = require("undici");
    await Promise.resolve(u.getGlobalDispatcher().close()).catch(() => {});
  } catch (_) {}
  if (dispatcher && typeof dispatcher.close === "function") {
    try {
      await Promise.resolve(dispatcher.close()).catch(() => {});
    } catch (_) {}
  }
  // 兜底：若仍有 ref 句柄卡住事件循环，2s 后硬退出（unref 不阻塞正常退出）。
  // 常驻模式下绝不能退出，否则会带走后续请求。
  if (!DAEMON_MODE) setTimeout(() => process.exit(0), 2000).unref();
}

/* ═══════════ 运行模式（对齐 momo/GoPay 的常驻哨兵桥）═══════════
 * 一次性模式（默认）：stdin 读一个请求 → stdout 写结果 → 退出，与历史行为完全一致。
 * 常驻模式（SENTINEL_DAEMON_SOCKET=/run/tilian/upi-sentinel-0.sock）：
 *   进程复用，省掉每次 mint 冷启动一个 ~62MB Node 进程的开销（momo 的 gopay 桥同款）。
 * 隔离性：每次请求仍然重新 makeGenerator() + vm.createContext()，指纹状态与"全新进程"
 *   逐字段等价，不会产生跨账号相关性。
 */
async function handleRequest(raw) {
  const chunks = [];
  const originalWrite = process.stdout.write.bind(process.stdout);
  const originalExit = process.exit;
  // 单次请求期间把 stdout / exit 收进内存：main() 内部的 early-exit 与结果输出
  // 都只影响这一次请求，不会结束常驻进程。
  process.stdout.write = (chunk, encoding, callback) => {
    chunks.push(typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"));
    const cb = typeof encoding === "function" ? encoding : callback;
    if (typeof cb === "function") cb();
    return true;
  };
  process.exit = () => {};
  try {
    await main(raw);
  } finally {
    process.stdout.write = originalWrite;
    process.exit = originalExit;
  }
  return chunks.join("");
}

async function runOnce() {
  const out = await handleRequest(undefined);
  await new Promise((r) => process.stdout.write(out, r));
  try {
    const u = require("undici");
    await Promise.resolve(u.getGlobalDispatcher().close()).catch(() => {});
  } catch (_) {}
  if (!DAEMON_MODE) setTimeout(() => process.exit(0), 2000).unref();
}

if (DAEMON_MODE) {
  const net = require("net");
  const server = net.createServer({ allowHalfOpen: true }, (conn) => {
    const chunks = [];
    let handled = false;
    const respond = async () => {
      if (handled) return;
      handled = true;
      const payloadText = Buffer.concat(chunks).toString("utf8");
      let out = "";
      try {
        out = await handleRequest(payloadText);
      } catch (e) {
        out = JSON.stringify({ error: "daemon: " + ((e && e.stack) || e) });
      }
      // 常驻模式诊断：每次请求的载荷/结果字节数（排障时看这一行就够）
      process.stderr.write(
        "[upi-sentinel] daemon request " + Buffer.byteLength(payloadText) + "B -> " + String(out || "").length + "B\n"
      );
      try {
        conn.end(out);
      } catch (_) {}
    };
    conn.on("data", (chunk) => {
      chunks.push(chunk);
      // 调用方是"发一个 JSON 请求体然后半关闭"，因此载荷能 JSON.parse 就说明收全了。
      // （Windows 命名管道下 'end' 可能不触发，这一步保证跨平台可用。）
      const text = Buffer.concat(chunks).toString("utf8").trim();
      if (!text) return;
      try {
        JSON.parse(text);
      } catch (_) {
        return;
      }
      void respond();
    });
    conn.on("end", () => { void respond(); });
    conn.on("error", () => {});
  });
  server.listen(String(process.env.SENTINEL_DAEMON_SOCKET).trim(), () => {
    process.stderr.write("[upi-sentinel] daemon listening on " + process.env.SENTINEL_DAEMON_SOCKET + "\n");
  });
  server.on("error", (e) => {
    process.stderr.write("[upi-sentinel] daemon error: " + ((e && e.message) || e) + "\n");
    process.exit(1);
  });
} else {
  runOnce().catch((e) => {
    process.stdout.write(JSON.stringify({ error: "fatal: " + ((e && e.stack) || e) }), () => process.exit(1));
  });
}
