#!/usr/bin/env node
"use strict";

// Live regression: run only after the isolated manager fix is deployed.
// Windows: node cdp-rst-smoke.cjs [--receipt <new-file>]
// GA401:   node cdp-rst-smoke.cjs --remote [--receipt <new-file>]
// Uses native fetch/WebSocket and node:net; no npm dependencies or external pages.
const { readFile, writeFile } = require("node:fs/promises");
const { join, resolve } = require("node:path");
const { randomBytes, createHash } = require("node:crypto");
const net = require("node:net");

class SmokeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function requireCondition(condition, code) {
  if (!condition) throw new SmokeError(code);
}
function options(argv) {
  let remote = false, receipt = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--remote" && !remote) remote = true;
    else if (argv[i] === "--receipt" && receipt === null && argv[i + 1] && !argv[i + 1].startsWith("--"))
      receipt = resolve(argv[++i]);
    else throw new SmokeError("invalid_arguments");
  }
  return { remote, receipt };
}
function remaining(deadline, maximum, code) {
  const milliseconds = Math.min(maximum, deadline - Date.now());
  requireCondition(milliseconds > 0, code);
  return milliseconds;
}
async function bounded(promise, deadline, maximum, code) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new SmokeError(code)), remaining(deadline, maximum, code));
      }),
    ]);
  } finally { clearTimeout(timer); }
}

// Preserve the token-bearing path privately. Only the explicitly allowed CDP
// origin can be used; --remote remaps the public tunnel port to GA401 loopback.
function cdpEndpoint(value, remote) {
  let url;
  try { url = new URL(value); } catch { throw new SmokeError("invalid_cdp_endpoint"); }
  requireCondition(url.protocol === "ws:" && url.hostname === "127.0.0.1" &&
    !url.username && !url.password && !url.hash && url.pathname.startsWith("/cdp/") &&
    (url.port === "13008" || (remote && url.port === "3008")), "invalid_cdp_endpoint");
  if (remote) url.port = "3008";
  return url;
}

class CdpPeer {
  constructor(url) {
    this.socket = new WebSocket(url.href);
    this.pending = new Map();
    this.sequence = 0;
    const fail = code => {
      for (const operation of this.pending.values()) operation.reject(new SmokeError(code));
      this.pending.clear();
    };
    this.socket.addEventListener("error", () => fail("cdp_transport_error"));
    this.socket.addEventListener("close", () => fail("cdp_connection_closed"));
    this.socket.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(typeof event.data === "string" ? event.data : Buffer.from(event.data).toString("utf8")); }
      catch { fail("invalid_cdp_message"); return; }
      const operation = this.pending.get(message.id);
      if (!operation) return;
      if (message.error) operation.reject(new SmokeError("cdp_command_failed"));
      else operation.resolve(message.result);
    });
  }
  async open(deadline) {
    try {
      await bounded(new Promise((resolve, reject) => {
        this.socket.addEventListener("open", resolve, { once: true });
        this.socket.addEventListener("error", () => reject(new SmokeError("cdp_open_failed")), { once: true });
        this.socket.addEventListener("close", () => reject(new SmokeError("cdp_open_closed")), { once: true });
      }), deadline, 5000, "cdp_open_timeout");
      return this;
    } catch (error) { this.close(); throw error; }
  }
  async version(deadline) {
    requireCondition(this.socket.readyState === WebSocket.OPEN, "cdp_not_open");
    const id = ++this.sequence;
    try {
      const result = await bounded(new Promise((resolve, reject) => {
        this.pending.set(id, { resolve, reject });
        this.socket.send(JSON.stringify({ id, method: "Browser.getVersion" }));
      }), deadline, 5000, "cdp_reply_timeout");
      requireCondition(result && typeof result.product === "string" && result.product.length > 0 &&
        typeof result.protocolVersion === "string", "invalid_cdp_version");
    } finally { this.pending.delete(id); }
  }
  close() {
    try { this.socket.close(); } catch { /* Cleanup deadline still owns termination. */ }
  }
}

function resetTcp(url, deadline, completeHandshake) {
  return new Promise((resolve, reject) => {
    let resetSent = false, finished = false, headers = Buffer.alloc(0);
    const socket = net.createConnection({ host: "127.0.0.1", port: Number(url.port), family: 4 });
    const timer = setTimeout(() => finish(new SmokeError("raw_cdp_timeout")),
      remaining(deadline, 5000, "raw_cdp_timeout"));
    function finish(error) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (!socket.destroyed) socket.destroy();
      if (error) reject(error); else resolve();
    }
    function reset() {
      try {
        // Unlike end()/destroy()/WebSocket.close(), this uses the TCP RST path.
        socket.resetAndDestroy();
        resetSent = true;
      } catch { finish(new SmokeError("tcp_reset_failed")); }
    }
    socket.on("error", () => {
      if (!resetSent) finish(new SmokeError("raw_cdp_transport_error"));
    });
    socket.on("close", () => finish(resetSent ? null : new SmokeError("raw_cdp_closed_before_reset")));
    socket.on("connect", () => {
      const key = randomBytes(16).toString("base64");
      const expectedAccept = createHash("sha1").update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
      const request = `GET ${url.pathname}${url.search} HTTP/1.1\r\nHost: ${url.host}\r\n` +
        `Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n`;
      if (!completeHandshake) {
        // Flush a partial Upgrade request, without its final blank line, then RST.
        socket.write(request, reset);
        return;
      }
      socket.on("data", chunk => {
        if (resetSent || finished) return;
        headers = Buffer.concat([headers, chunk]);
        const end = headers.indexOf("\r\n\r\n");
        if (end < 0) {
          if (headers.length > 16384) finish(new SmokeError("upgrade_headers_too_large"));
          return;
        }
        if (end > 16384) return finish(new SmokeError("upgrade_headers_too_large"));
        const lines = headers.subarray(0, end).toString("latin1").split("\r\n");
        const fields = new Map();
        for (const line of lines.slice(1)) {
          const colon = line.indexOf(":");
          if (colon > 0) fields.set(line.slice(0, colon).trim().toLowerCase(), line.slice(colon + 1).trim());
        }
        if (!/^HTTP\/1\.1 101(?:\s|$)/.test(lines[0]) ||
            (fields.get("upgrade") || "").toLowerCase() !== "websocket" ||
            !(fields.get("connection") || "").toLowerCase().split(",").map(x => x.trim()).includes("upgrade") ||
            fields.get("sec-websocket-accept") !== expectedAccept)
          return finish(new SmokeError("invalid_websocket_upgrade"));
        reset();
      });
      socket.write(request + "\r\n");
    });
  });
}

async function run(configuration, receipt, started) {
  const totalDeadline = started + 60000;
  const workDeadline = started + 40000;
  const owned = [];
  const peers = [];
  let stage = "preflight";
  const passed = name => receipt.checks.push(name);
  try {
    requireCondition(typeof fetch === "function" && typeof WebSocket === "function" &&
      typeof net.Socket.prototype.resetAndDestroy === "function", "node_runtime_unsupported");
    requireCondition(configuration.remote || (process.platform === "win32" && process.env.USERPROFILE),
      "windows_or_remote_required");
    const clientPath = configuration.remote ? "/home/marck/services/firecrawl-browser/shared/client.json" :
      join(process.env.USERPROFILE, ".local", "state", "firecrawl-browser", "client.json");
    let client;
    try { client = JSON.parse(await readFile(clientPath, "utf8")); }
    catch { throw new SmokeError("private_client_unreadable"); }
    const endpoint = configuration.remote ? client.remote_url : client.api_url;
    requireCondition(endpoint === (configuration.remote ? "http://127.0.0.1:3007" : "http://127.0.0.1:13007"),
      "invalid_api_endpoint");
    requireCondition(typeof client.api_key === "string" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(client.api_key), "invalid_private_key");
    const api = async (path, method = "GET", body, cleanup = false) => {
      const deadline = cleanup ? totalDeadline : workDeadline;
      let response, value;
      try {
        response = await fetch(endpoint + path, {
          method, redirect: "error", signal: AbortSignal.timeout(remaining(deadline, cleanup ? 7000 : 10000, "api_deadline")),
          headers: { Authorization: `Bearer ${client.api_key}`, "Content-Type": "application/json" },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const text = await response.text();
        requireCondition(Buffer.byteLength(text) <= 1000000, "api_response_too_large");
        value = JSON.parse(text);
      } catch (error) {
        throw error instanceof SmokeError ? error : new SmokeError("api_transport_or_json_failed");
      }
      requireCondition(response.status === 200, `api_http_${response.status}`);
      requireCondition(value && value.success === true, "api_operation_failed");
      return value;
    };
    const sessionPath = id => `/v2/browser/${encodeURIComponent(id)}`;
    const execute = async (id, code) => {
      const value = await api(sessionPath(id) + "/execute", "POST", { code, language: "node", timeout: 5 });
      requireCondition(value.exitCode === 0 && value.killed === false &&
        typeof value.stderr === "string" && !value.stderr.trim(), "node_execution_failed");
      return value;
    };
    const running = async id => requireCondition((await api(sessionPath(id))).status === "running", "session_not_running");
    // Attach cleanup to the run before the first creation can succeed.
    receipt.cleanup = [];
    try {
      const create = async label => {
        stage = `create_${label}`;
        const value = await api("/v2/browser", "POST", { ttl: 120, activityTtl: 90, streamWebView: false, blockAds: false });
        requireCondition(typeof value.id === "string" &&
          /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.id), "invalid_session_id");
        owned.push({ id: value.id, label });
        return { id: value.id, cdp: cdpEndpoint(value.cdpUrl, configuration.remote) };
      };
      const a = await create("a");
      const b = await create("b");
      requireCondition(a.id !== b.id, "duplicate_session_id");
      passed("two_owned_sessions_created");
      stage = "seed_b_dom";
      const marker = "CDP_RST_" + randomBytes(18).toString("hex");
      const html = `<title>CDP RST Smoke</title><p id="rst-proof">${marker}</p>`;
      await execute(b.id, `await page.setContent(${JSON.stringify(html)}); console.log(await page.locator('#rst-proof').innerText());`);
      const markerUnchanged = async () => {
        const value = await execute(b.id, "console.log(await page.locator('#rst-proof').innerText());");
        requireCondition(typeof value.stdout === "string" && value.stdout.trim() === marker, "b_dom_marker_changed");
      };
      await markerUnchanged();
      passed("b_dom_marker_seeded");
      stage = "open_b_cdp";
      const bPeer = new CdpPeer(b.cdp);
      peers.push(bPeer);
      await bPeer.open(workDeadline);
      await bPeer.version(workDeadline);
      passed("b_same_websocket_version_before_reset");
      stage = "a_rst_after_valid_upgrade";
      await resetTcp(a.cdp, workDeadline, true);
      passed("a_valid_101_then_tcp_rst");
      stage = "verify_b_after_upgrade_rst";
      await bPeer.version(workDeadline);
      await markerUnchanged();
      await running(a.id);
      await running(b.id);
      passed("b_same_websocket_dom_and_both_running_after_rst");
      stage = "a_rst_before_upgrade_complete";
      await resetTcp(a.cdp, workDeadline, false);
      passed("a_partial_upgrade_then_tcp_rst");
      stage = "verify_b_after_partial_rst";
      await bPeer.version(workDeadline);
      await markerUnchanged();
      await running(a.id);
      await running(b.id);
      passed("b_same_websocket_dom_and_both_running_after_partial_rst");
      stage = "a_cdp_reconnect_and_execute";
      const aPeer = new CdpPeer(a.cdp);
      peers.push(aPeer);
      await aPeer.open(workDeadline);
      await aPeer.version(workDeadline);
      const aResult = await execute(a.id, "console.log('A_RST_RECOVERY_OK');");
      requireCondition(aResult.stdout.trim() === "A_RST_RECOVERY_OK", "a_execution_recovery_failed");
      await bPeer.version(workDeadline);
      passed("a_reconnect_execute_and_b_original_websocket_alive");
    } catch (error) {
      receipt.failure = { stage, code: error instanceof SmokeError ? error.code : "unexpected_failure" };
    } finally {
      for (const peer of peers) peer.close();
      // Never enumerate, stop or inspect someone else's sessions or containers.
      for (const session of owned) {
        try {
          const deleted = await api(sessionPath(session.id), "DELETE", undefined, true);
          requireCondition(deleted.status === "stopped", "delete_not_stopped");
          const status = await api(sessionPath(session.id), "GET", undefined, true);
          requireCondition(status.status === "stopped", "cleanup_status_not_stopped");
          receipt.cleanup.push({ session: session.label, stopped: true });
        } catch (error) {
          receipt.cleanup.push({ session: session.label, stopped: false,
            code: error instanceof SmokeError ? error.code : "cleanup_failed" });
        }
      }
    }
    receipt.cleanupComplete = owned.length === receipt.cleanup.length && receipt.cleanup.every(row => row.stopped);
    receipt.ok = !receipt.failure && receipt.cleanupComplete && owned.length === 2;
  } catch (error) {
    receipt.failure = { stage, code: error instanceof SmokeError ? error.code : "unexpected_failure" };
  }
  return receipt;
}

async function main() {
  const started = Date.now();
  const receipt = { test: "cdp_tcp_rst_isolation", ok: false, checks: [], cleanup: [], cleanupComplete: false };
  const watchdog = setTimeout(() => {
    receipt.ok = false;
    receipt.failure = { stage: "deadline", code: "total_60s_deadline_exceeded" };
    receipt.elapsedMs = Date.now() - started;
    process.stdout.write(JSON.stringify(receipt) + "\n", () => process.exit(1));
    setTimeout(() => process.exit(1), 50);
  }, 60000);
  try {
    const configuration = options(process.argv.slice(2));
    receipt.mode = configuration.remote ? "remote" : "windows";
    await run(configuration, receipt, started);
    receipt.elapsedMs = Date.now() - started;
    if (configuration.receipt) {
      try { await writeFile(configuration.receipt, JSON.stringify(receipt, null, 2) + "\n", { flag: "wx", mode: 0o600 }); }
      catch { receipt.ok = false; receipt.receiptError = "receipt_write_failed"; }
    }
  } catch (error) {
    receipt.failure = { stage: "arguments", code: error instanceof SmokeError ? error.code : "unexpected_failure" };
    receipt.elapsedMs = Date.now() - started;
  }
  clearTimeout(watchdog);
  // A stuck peer must not keep a completed smoke process alive beyond its bound.
  process.stdout.write(JSON.stringify(receipt) + "\n", () => process.exit(receipt.ok ? 0 : 1));
}

main().catch(() => {
  process.stdout.write('{"test":"cdp_tcp_rst_isolation","ok":false,"failure":{"code":"unexpected_failure"}}\n',
    () => process.exit(1));
});
