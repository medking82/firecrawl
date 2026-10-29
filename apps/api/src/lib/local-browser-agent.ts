import { request } from "node:http";
import { executeHangarBrowser } from "./hangar";
import type { AgentResult } from "./scrape-interact/browser-agent";

type Decision = { code: string; done: boolean; summary: string };
const MAX_STEPS = 5;

function clip(value: string, bytes: number): string {
  if (Buffer.byteLength(value) <= bytes) return value;
  return Buffer.from(value)
    .subarray(0, bytes - 3)
    .toString("utf8");
}

function decide(
  socketPath: string,
  task: string,
  snapshot: string,
  history: string[],
  deadline: number,
): Promise<Decision> {
  return new Promise((resolve, reject) => {
    // The CLI deadline leaves room for its response before our absolute timer.
    const remaining = deadline - Date.now();
    if (remaining <= 250)
      return reject(new Error("Local browser deadline exceeded"));
    const payload = JSON.stringify({
      task,
      snapshot,
      history,
      timeout: Math.min(110, (remaining - 200) / 1000),
    });
    if (Buffer.byteLength(payload) > 128_000)
      return reject(new Error("Local browser input exceeds 128 KB"));
    const req = request(
      {
        socketPath,
        path: "/browser-step",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      res => {
        const chunks: Buffer[] = [];
        let size = 0;
        res.on("data", (chunk: Buffer) => {
          size += chunk.length;
          if (size > 64_000) {
            res.destroy(new Error("Local browser response exceeds 64 KB"));
            return;
          }
          chunks.push(chunk);
        });
        res.on("error", reject);
        res.on("end", () => {
          if (res.statusCode !== 200)
            return reject(
              new Error(`Local browser CLI failed: HTTP ${res.statusCode}`),
            );
          try {
            const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            if (
              !value ||
              typeof value !== "object" ||
              Object.keys(value).sort().join(",") !== "code,done,summary" ||
              typeof value.code !== "string" ||
              Buffer.byteLength(value.code) > 20_000 ||
              typeof value.done !== "boolean" ||
              typeof value.summary !== "string" ||
              !value.summary.trim() ||
              Buffer.byteLength(value.summary) > 4_000 ||
              value.done === Boolean(value.code.trim())
            )
              throw new Error("Invalid local browser decision");
            resolve(value);
          } catch (error) {
            reject(error);
          }
        });
      },
    );
    const timer = setTimeout(
      () => req.destroy(new Error("Local browser deadline exceeded")),
      remaining,
    );
    req.on("close", () => clearTimeout(timer));
    req.on("error", reject);
    req.end(payload);
  });
}

// Uses only the supervisor's persistent Node/Playwright page. No CLI browser
// commands, alternate providers or retained task/debug files are involved.
export async function executeLocalBrowserAgent(
  prompt: string,
  browserId: string,
  timeout: number,
): Promise<AgentResult> {
  const deadline = Date.now() + Math.min(timeout, 120) * 1000;
  const history: string[] = [];
  const outputs: string[] = [];
  let snapshot = "";
  const result = (
    output: string,
    stderr = "",
    killed = false,
  ): AgentResult => ({
    output,
    stdout: outputs.join("\n"),
    result: snapshot,
    stderr,
    exitCode: stderr ? 1 : 0,
    killed,
  });
  const execute = async (code: string) => {
    const remaining = deadline - Date.now();
    if (remaining < 1000) throw new Error("Local browser deadline exceeded");
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        executeHangarBrowser(browserId, {
          code,
          language: "node",
          timeout: Math.max(1, Math.floor(remaining / 1000)),
        }),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("Local browser deadline exceeded")),
            remaining,
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  };
  try {
    const socket = process.env.SELF_HOSTED_BROWSER_LLM_SOCKET;
    if (!socket) throw new Error("Local browser CLI socket is missing");
    if (!Number.isFinite(timeout) || timeout <= 0 || !prompt.trim())
      throw new Error("Invalid local browser input");
    for (let step = 0; step < MAX_STEPS; step++) {
      const observed = await execute(`console.log(JSON.stringify({
        url: page.url(), title: await page.title(),
        text: (await page.locator('body').innerText({ timeout: 3000 })).slice(0, 12000),
        controls: await page.locator('a,button,input,select,textarea,[role]').evaluateAll(nodes =>
          nodes.slice(0, 60).map(el => ({
            tag: el.tagName, type: el.getAttribute('type'),
            id: el.id.slice(0, 100), name: (el.getAttribute('name') || '').slice(0, 100),
            role: el.getAttribute('role'),
            label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').slice(0, 160),
            text: (el.textContent || '').slice(0, 160)
          })))
      }));`);
      if (observed.exitCode !== 0 || observed.killed)
        return result("", "Local browser snapshot failed", observed.killed);
      snapshot = clip(observed.stdout || observed.result || "", 50_000);
      if (!snapshot.trim())
        return result("", "Local browser snapshot is empty");
      const decision = await decide(
        socket,
        prompt,
        snapshot,
        history,
        deadline,
      );
      if (decision.done) return result(decision.summary);
      // Leave the final decision for a verified answer, not an unobserved action.
      if (step === MAX_STEPS - 1)
        return result("", "Local browser step limit reached");
      const execution = await execute(decision.code);
      const output = clip(execution.stdout || execution.result || "", 4_000);
      if (output) outputs.push(output);
      if (execution.exitCode !== 0 || execution.killed)
        return result("", "Local browser action failed", execution.killed);
      history.push(
        clip(
          JSON.stringify({ step: step + 1, summary: decision.summary, output }),
          4_000,
        ),
      );
    }
    return result("", "Local browser step limit reached");
  } catch (error) {
    // No fallback when local mode is selected; do not echo page/code exceptions.
    const deadlineExceeded =
      Date.now() >= deadline ||
      (error instanceof Error &&
        error.message === "Local browser deadline exceeded");
    return result(
      "",
      deadlineExceeded
        ? "Local browser deadline exceeded"
        : "Local browser CLI failed",
      deadlineExceeded,
    );
  }
}
