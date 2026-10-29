// Runs only in the isolated monitor API container, with its DB and artifact volume.
const { existsSync } = require('node:fs');
const { createRequire } = require('node:module');
const { lstat, realpath, unlink } = require('node:fs/promises');
const { resolve, sep } = require('node:path');
const apiRoot = existsSync(resolve(__dirname, 'dist/src/lib/local-artifacts.js')) ? __dirname : resolve(__dirname, '../../apps/api');
const apiRequire = createRequire(resolve(apiRoot, 'package.json'));
const { Client } = apiRequire('pg');
const { localJobArtifactKey } = require(resolve(apiRoot, 'dist/src/lib/local-artifacts.js'));

const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}';
const DIFF = new RegExp(`^monitors/(${UUID})/(${UUID})/(${UUID})/(${UUID})\\.diff\\.json$`, 'i');
function validateKey(row) {
  if (row.job_id) {
    if (row.artifact_key !== localJobArtifactKey(row.job_id)) throw new Error('Invalid snapshot GC key');
  } else {
    const match = row.artifact_key.match(DIFF);
    if (!match || match[2] !== row.monitor_id) throw new Error('Invalid diff GC key');
  }
}
async function removeFile(root, row) {
  validateKey(row);
  const base = await realpath(root);
  const path = resolve(base, row.artifact_key);
  if (!path.startsWith(base + sep)) throw new Error('GC path outside artifact volume');
  try {
    const parent = await realpath(resolve(path, '..'));
    if (!parent.startsWith(base + sep)) throw new Error('GC parent outside artifact volume');
    const info = await lstat(path);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error('GC target is not a regular file');
    await unlink(path); // Exact known file only; never recursively removes a directory.
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}
async function lockIdleMonitor(db, id) {
  const { rows } = await db.query(`SELECT *, status='deleted' AND deleted_at<now()-make_interval(days=>retention_days) AS gc_retired
    FROM public.monitors WHERE id=$1 FOR UPDATE SKIP LOCKED`, [id]);
  if (!rows.length) {
    const exists = await db.query('SELECT 1 FROM public.monitors WHERE id=$1', [id]);
    return exists.rows.length ? { busy: true } : { missing: true };
  }
  const active = await db.query("SELECT 1 FROM public.monitor_checks WHERE monitor_id=$1 AND status IN ('queued','running') LIMIT 1", [id]);
  return { monitor: rows[0], busy: Boolean(rows[0].current_check_id || active.rows.length) };
}
async function begin(db) {
  await db.query('BEGIN');
  await db.query("SET LOCAL lock_timeout='1s'");
  await db.query("SET LOCAL statement_timeout='5s'");
}
async function prune(db, root, { dryRun = false, limit = 100 } = {}) {
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Error('Invalid GC batch limit');
  if (!root || !process.env.DATABASE_URL) throw new Error('Isolated DB and artifact directory required');
  const stats = { dry_run: dryRun, monitors_busy: 0, expired_checks: 0, queued_artifacts: 0,
    protected_artifacts: 0, artifacts_deleted: 0, artifacts_missing: 0, failures: 0 };
  const held = await db.query('SELECT pg_try_advisory_lock(82510,621) AS held');
  if (!held.rows[0].held) return { ...stats, busy: true };
  try {
    const candidates = await db.query(`SELECT m.id FROM public.monitors m WHERE
      EXISTS (SELECT 1 FROM public.monitor_checks c WHERE c.monitor_id=m.id
        AND c.status NOT IN ('queued','running') AND (c.id IS DISTINCT FROM m.last_check_id
          OR (m.status='deleted' AND m.deleted_at<now()-make_interval(days=>m.retention_days)))
        AND coalesce(c.finished_at,c.created_at) < now()-make_interval(days=>m.retention_days))
      OR (m.status='deleted' AND m.deleted_at < now()-make_interval(days=>m.retention_days)
        AND EXISTS(SELECT 1 FROM public.monitor_pages p WHERE p.monitor_id=m.id))
      ORDER BY m.id LIMIT $1`, [limit]);
    for (const { id } of candidates.rows) {
      await begin(db);
      try {
        const locked = await lockIdleMonitor(db, id);
        if (locked.busy || locked.missing) { stats.monitors_busy++; await db.query('ROLLBACK'); continue; }
        const m = locked.monitor;
        const expired = await db.query(`SELECT id FROM public.monitor_checks WHERE monitor_id=$1
          AND status NOT IN ('queued','running') AND id IS DISTINCT FROM $2::uuid
          AND coalesce(finished_at,created_at)<now()-make_interval(days=>$3) ORDER BY created_at LIMIT $4`,
          [id, m.gc_retired ? null : m.last_check_id, m.retention_days, limit]);
        const ids = expired.rows.map(x => x.id);
        const pages = ids.length ? (await db.query('SELECT * FROM public.monitor_check_pages WHERE check_id=ANY($1::uuid[])', [ids])).rows : [];
        const queue = new Map();
        function snapshot(job) { if (job) queue.set(localJobArtifactKey(job), job); }
        for (const p of pages) {
          snapshot(p.previous_scrape_id); snapshot(p.current_scrape_id);
          if (p.diff_gcs_key) {
            const match = p.diff_gcs_key.match(DIFF);
            if (!match || match[1] !== m.team_id || match[2] !== id || match[3] !== p.check_id) throw new Error('Invalid retained diff path');
            queue.set(p.diff_gcs_key, null);
          }
        }
        // A deleted monitor has no future baseline, but active/paused baselines
        // remain pinned regardless of age. Deletion gets its own retention grace.
        const retired = await db.query(`SELECT id,last_scrape_id FROM public.monitor_pages WHERE monitor_id=$1
          AND $2::text='deleted' AND $3::timestamptz < now()-make_interval(days=>$4)`,
          [id, m.status, m.deleted_at, m.retention_days]);
        for (const p of retired.rows) snapshot(p.last_scrape_id);
        for (const [key, job] of queue) {
          await db.query(`INSERT INTO public.self_hosted_artifact_gc(artifact_key,monitor_id,job_id)
            VALUES($1,$2,$3) ON CONFLICT(artifact_key) DO NOTHING`, [key,id,job]);
        }
        if (ids.length) await db.query('DELETE FROM public.monitor_checks WHERE id=ANY($1::uuid[])', [ids]);
        if (retired.rows.length) await db.query('DELETE FROM public.monitor_pages WHERE id=ANY($1::uuid[])', [retired.rows.map(x => x.id)]);
        if (m.gc_retired) await db.query('UPDATE public.monitors SET last_check_id=NULL,last_check_summary=NULL WHERE id=$1', [id]);
        stats.expired_checks += ids.length; stats.queued_artifacts += queue.size;
        await db.query(dryRun ? 'ROLLBACK' : 'COMMIT');
      } catch (error) { await db.query('ROLLBACK'); throw error; }
    }
    const pending = await db.query('SELECT * FROM public.self_hosted_artifact_gc WHERE not_before<=now() ORDER BY created_at,artifact_key LIMIT $1', [limit * 4]);
    for (const row of pending.rows) {
      await begin(db);
      try {
        const locked = await lockIdleMonitor(db, row.monitor_id);
        if (locked.busy) { stats.monitors_busy++; await db.query('ROLLBACK'); continue; }
        validateKey(row);
        const referenced = row.job_id
          ? await db.query(`SELECT EXISTS(SELECT 1 FROM public.monitor_pages WHERE last_scrape_id=$1)
              OR EXISTS(SELECT 1 FROM public.monitor_check_pages WHERE previous_scrape_id=$1 OR current_scrape_id=$1) AS used`, [row.job_id])
          : await db.query('SELECT EXISTS(SELECT 1 FROM public.monitor_check_pages WHERE diff_gcs_key=$1) AS used', [row.artifact_key]);
        if (referenced.rows[0].used) {
          stats.protected_artifacts++;
          await db.query("UPDATE public.self_hosted_artifact_gc SET not_before=now()+interval '1 hour' WHERE artifact_key=$1", [row.artifact_key]);
        } else if (!dryRun) {
          if (await removeFile(root, row)) stats.artifacts_deleted++; else stats.artifacts_missing++;
          await db.query('DELETE FROM public.self_hosted_artifact_gc WHERE artifact_key=$1', [row.artifact_key]);
        }
        await db.query(dryRun ? 'ROLLBACK' : 'COMMIT');
      } catch (error) {
        await db.query('ROLLBACK'); stats.failures++;
        // Keep the durable outbox row for the next scheduled pass; no retry loop.
        console.error(JSON.stringify({ event: 'artifact_gc_failed', error: error.code || error.name }));
      }
    }
    return stats;
  } finally { await db.query('SELECT pg_advisory_unlock(82510,621)'); }
}
if (require.main === module) {
  const db = new Client({ connectionString: process.env.DATABASE_URL });
  (async () => {
    try {
      await db.connect();
      const stats = await prune(db, process.env.SELF_HOSTED_ARTIFACT_DIR, { dryRun: process.argv.includes('--dry-run') });
      console.log(JSON.stringify({ event: 'artifact_gc_completed', ...stats }));
      if (stats.failures) process.exitCode = 1;
    } finally { await db.end(); }
  })().catch(error => { console.error(JSON.stringify({ event: 'artifact_gc_failed', error: error.code || error.name })); process.exitCode = 1; });
}
module.exports = { prune, removeFile, validateKey };
