"use strict";
/**
 * sentinel_daemon.js —— 哨兵桥的常驻宿主。
 *
 * 背景：原来每次生成 sentinel token 都要 cold-start 一个 62MB 的 Node 进程，
 * 用完即死；线上并发一高就是「并发数 × 62MB」的内存开销。
 *
 * 做法：进程常驻，但对每个请求都调用同一个 handleRequest()，而 handleRequest
 * 内部每次都重新 makeGenerator() + vm.createContext()，所以指纹状态与
 * 「全新进程」逐字段等价 —— 不会让不同账号的令牌产生相关性。
 *
 * 协议：Unix domain socket。客户端「写入 payload → shutdown(写端) → 读到 EOF」，
 * 与一次性模式的 stdin/stdout 语义完全一致，方便上层零成本切换。
 *
 * 并发：handleRequest 内部有一段同步 PoW 计算，会短暂占用事件循环。
 * 因此支持起多个进程、每个进程监听自己的 socket（sentinel-0.sock、-1.sock…），
 * 由客户端轮流选择。
 */
const net = require("net");
const fs = require("fs");
const path = require("path");

function log(msg) {
  try { process.stderr.write("[sentinel_daemon] " + msg + "\n"); } catch (_) {}
}

function serve(handleRequest, socketPath) {
  try { fs.mkdirSync(path.dirname(socketPath), { recursive: true }); } catch (_) {}
  // 复用残留 socket 会让连接打到已经死掉的旧进程上，必须先清掉。
  try { if (fs.existsSync(socketPath)) fs.unlinkSync(socketPath); } catch (_) {}

  let served = 0;
  let failed = 0;

  const server = net.createServer({ allowHalfOpen: true }, (conn) => {
    let buf = "";
    conn.setEncoding("utf8");
    conn.on("data", (d) => { buf += d; });
    conn.on("error", () => {});
    conn.on("end", async () => {
      let out;
      try {
        out = await handleRequest(buf);
        served += 1;
      } catch (e) {
        failed += 1;
        out = JSON.stringify({ error: "daemon: " + ((e && e.stack) || e) });
      }
      try { conn.end(String(out == null ? "" : out)); } catch (_) {}
    });
  });

  server.on("error", (e) => log("server error: " + ((e && e.message) || e)));

  server.listen(socketPath, () => {
    try { fs.chmodSync(socketPath, 0o660); } catch (_) {}
    log("listening " + socketPath + " pid=" + process.pid);
  });

  const cleanup = () => {
    try { server.close(); } catch (_) {}
    try { fs.unlinkSync(socketPath); } catch (_) {}
    log("stopped, served=" + served + " failed=" + failed);
    process.exit(0);
  };
  process.on("SIGTERM", cleanup);
  process.on("SIGINT", cleanup);

  // 常驻进程必须有兜底：任何漏网的异常都不许让宿主流失。
  process.on("uncaughtException", (e) => log("uncaughtException(ignored): " + ((e && e.stack) || e)));
  process.on("unhandledRejection", (e) => log("unhandledRejection(ignored): " + ((e && e.stack) || e)));

  return server;
}

module.exports = { serve };
