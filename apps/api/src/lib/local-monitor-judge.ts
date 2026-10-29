import { request } from "node:http";

// The private socket delegates to the host's existing, tools-disabled CLI route.
// No new provider credentials, model loading, network listener or paid fallback.
export function callLocalMonitorJudge(system: string, prompt: string): Promise<{ text: string }> {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({ system, prompt });
    if (Buffer.byteLength(payload) > 128_000) {
      reject(new Error("Monitor judge input exceeds 128 KB"));
      return;
    }
    const req = request({
      socketPath: process.env.SELF_HOSTED_JUDGE_SOCKET,
      path: "/judge", method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
    }, res => {
      const chunks: Buffer[] = [];
      let size = 0;
      res.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > 64_000) { res.destroy(new Error("Monitor judge response exceeds 64 KB")); return; }
        chunks.push(chunk);
      });
      res.on("error", reject);
      res.on("end", () => {
        if (res.statusCode !== 200) { reject(new Error(`Local CLI judge failed: HTTP ${res.statusCode}`)); return; }
        try {
          const result = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (typeof result.text !== "string" || !result.text.trim()) throw new Error("Empty CLI judgment");
          resolve({ text: result.text });
        } catch (error) { reject(error); }
      });
    });
    // One bounded request. Existing proxy/CLI policy owns any provider retries.
    req.setTimeout(120_000, () => req.destroy(new Error("Local CLI judge timed out")));
    req.on("error", reject);
    req.end(payload);
  });
}
