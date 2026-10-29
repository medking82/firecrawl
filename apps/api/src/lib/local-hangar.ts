import { request } from "node:http";

/** Private host lifecycle socket, never exposed to disposable execution containers. */
export function localHangarRequest(method: string, pathname: string, body?: unknown, key?: string, timeout = 40_000): Promise<Response> {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const req = request({ socketPath: process.env.SELF_HOSTED_BROWSER_SOCKET, path: pathname, method,
      headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) } }, res => {
      const chunks: Buffer[] = []; let size = 0;
      res.on("data", (chunk: Buffer) => { size += chunk.length; if (size > 256_000) res.destroy(new Error("Browser output limit")); else chunks.push(chunk); });
      res.on("error", reject);
      res.on("end", () => resolve(new Response(Buffer.concat(chunks).toString("utf8"), { status: res.statusCode ?? 502 })));
    });
    req.on("error", reject); req.setTimeout(timeout, () => req.destroy(new Error("Browser request timeout"))); req.end(payload);
  });
}
