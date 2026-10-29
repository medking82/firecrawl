import { sql } from "drizzle-orm";
import { db } from "../db/connection";

type LocalJobKind = "access" | "scrape_state";

// This is a database-backed operational store for the isolated browser stack.
// Monitor/local-artifact configuration alone must never activate it.
export function localBrowserJobStoreEnabled(): boolean {
  return Boolean(process.env.SELF_HOSTED_BROWSER_SOCKET);
}

export async function writeLocalBrowserJob(
  kind: LocalJobKind,
  id: string,
  value: object,
  expiresAtMs: number,
): Promise<boolean> {
  if (!Number.isFinite(expiresAtMs))
    throw new Error("Invalid local job expiry");
  await db.execute(sql`
    INSERT INTO public.self_hosted_browser_jobs (kind, id, value, expires_at)
    VALUES (${kind}, ${id}::uuid, ${JSON.stringify(value)}::jsonb,
            ${new Date(expiresAtMs).toISOString()}::timestamptz)
    ON CONFLICT (kind, id) DO UPDATE
    SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at
  `);
  return true;
}

export async function readLocalBrowserJob(
  kind: LocalJobKind,
  id: string,
): Promise<string | null> {
  // Storage errors are outages. Never turn them into a cache miss or fall back
  // to hosted stores; native ownership and retention checks depend on this row.
  const result = await db.execute(sql`
    SELECT value FROM public.self_hosted_browser_jobs
    WHERE kind = ${kind} AND id = ${id}::uuid
      AND expires_at > clock_timestamp()
  `);
  return result.rows.length ? JSON.stringify(result.rows[0].value) : null;
}
