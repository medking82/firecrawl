const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { randomUUID } = require('node:crypto');
const path = require('node:path');
const { existsSync } = require('node:fs');
const { createRequire } = require('node:module');
const packaged = existsSync(path.join(__dirname, 'monitor-prune.cjs'));
const apiRoot = packaged ? __dirname : path.resolve(__dirname, '../../apps/api');
const { Client } = createRequire(path.join(apiRoot, 'package.json'))('pg');
const { prune } = require(packaged ? './monitor-prune.cjs' : './prune.cjs');
const { localJobArtifactKey } = require(path.join(apiRoot, 'dist/src/lib/local-artifacts.js'));

test('real PostgreSQL/filesystem retention preserves baselines, active checks and references', { timeout: 60_000 }, async () => {
  const url = new URL(process.env.DATABASE_URL);
  const name = 'retention_test_' + randomUUID().replaceAll('-', '');
  const admin = new Client({ connectionString: url.href });
  await admin.connect();
  await admin.query(`CREATE DATABASE ${name}`);
  url.pathname = '/' + name;
  const db = new Client({ connectionString: url.href });
  const other = new Client({ connectionString: url.href });
  const root = await fs.mkdtemp('/tmp/monitor-retention-');
  const old = new Date(Date.now() - 40 * 86400_000);
  async function artifact(id, value = [{ markdown: id }]) {
    const key = localJobArtifactKey(id);
    await fs.mkdir(path.join(root, 'jobs'), { recursive: true });
    await fs.writeFile(path.join(root, key), JSON.stringify(value));
    await fs.utimes(path.join(root, key), old, old);
    return key;
  }
  async function exists(key) { try { await fs.stat(path.join(root, key)); return true; } catch (e) { if (e.code === 'ENOENT') return false; throw e; } }
  async function fixture(status = 'active') {
    const m = { team: randomUUID(), id: randomUUID(), oldCheck: randomUUID(), lastCheck: randomUUID(), baseline: randomUUID(), obsolete: randomUUID(), recent: randomUUID(), page: randomUUID() };
    await db.query('INSERT INTO teams(id,name) VALUES($1,$2)', [m.team, 'retention contract']);
    await db.query(`INSERT INTO monitors(id,team_id,name,status,schedule_cron,retention_days,last_check_id,deleted_at)
      VALUES($1,$2,'retention contract',$3,'0 0 * * *',1,$4,$5)`, [m.id,m.team,status,m.lastCheck,status === 'deleted' ? old : null]);
    await db.query(`INSERT INTO monitor_checks(id,monitor_id,team_id,trigger,status,created_at,finished_at)
      VALUES($1,$3,$4,'manual','completed',$5,$5),($2,$3,$4,'manual','failed',now(),now())`, [m.oldCheck,m.lastCheck,m.id,m.team,old]);
    await db.query(`INSERT INTO monitor_pages(monitor_id,team_id,target_id,url,url_hash,source,last_scrape_id,last_status)
      VALUES($1,$2,'target','https://example.com',decode('ab','hex'),'explicit',$3,'same')`, [m.id,m.team,m.baseline]);
    const diff = `monitors/${m.team}/${m.id}/${m.oldCheck}/${m.page}.diff.json`;
    await fs.mkdir(path.dirname(path.join(root, diff)), { recursive: true });
    await fs.writeFile(path.join(root, diff), JSON.stringify({ text: 'obsolete diff' }));
    await db.query(`INSERT INTO monitor_check_pages(id,check_id,monitor_id,team_id,target_id,url,url_hash,status,previous_scrape_id,current_scrape_id,diff_gcs_key)
      VALUES($1,$2,$3,$4,'target','https://example.com',decode('ab','hex'),'changed',$5,$6,$7)`,
      [m.page,m.oldCheck,m.id,m.team,m.obsolete,m.baseline,diff]);
    await db.query(`INSERT INTO monitor_check_pages(id,check_id,monitor_id,team_id,target_id,url,url_hash,status,current_scrape_id)
      VALUES($1,$2,$3,$4,'target','https://example.com',decode('ab','hex'),'error',$5)`, [randomUUID(),m.lastCheck,m.id,m.team,m.recent]);
    m.baselineKey = await artifact(m.baseline); m.obsoleteKey = await artifact(m.obsolete); m.recentKey = await artifact(m.recent); m.diff = diff;
    return m;
  }
  try {
    await db.connect(); await other.connect();
    const sqlRoot = packaged ? path.join(__dirname, 'monitor-db') : __dirname;
    await db.query(await fs.readFile(path.join(sqlRoot, '001-monitor.sql'), 'utf8'));
    await db.query(await fs.readFile(path.join(sqlRoot, '002-artifact-gc.sql'), 'utf8'));
    const m = await fixture();
    const baselineBytes = await fs.readFile(path.join(root, m.baselineKey));
    const dry = await prune(db, root, { dryRun: true });
    assert.equal(dry.expired_checks, 1);
    assert.equal((await db.query('SELECT 1 FROM monitor_checks WHERE id=$1', [m.oldCheck])).rowCount, 1);
    assert.equal(await exists(m.obsoleteKey), true);
    const first = await prune(db, root);
    assert.equal(first.expired_checks, 1); assert.equal(first.artifacts_deleted, 2);
    assert.equal(first.protected_artifacts, 1);
    assert.equal(await exists(m.obsoleteKey), false); assert.equal(await exists(m.diff), false);
    assert.deepEqual(await fs.readFile(path.join(root, m.baselineKey)), baselineBytes);
    assert.equal(await exists(m.recentKey), true);
    assert.equal((await db.query('SELECT 1 FROM monitor_checks WHERE id=$1', [m.lastCheck])).rowCount, 1);
    // The pin is re-evaluated later: advancing the baseline releases its former snapshot.
    await db.query('UPDATE monitor_pages SET last_scrape_id=$1 WHERE monitor_id=$2', [m.recent,m.id]);
    await db.query("UPDATE self_hosted_artifact_gc SET not_before=now()-interval '1 second'");
    assert.equal((await prune(db, root)).artifacts_deleted, 1);
    assert.equal(await exists(m.baselineKey), false); assert.equal(await exists(m.recentKey), true);

    const busy = await fixture('paused');
    const open = randomUUID();
    await db.query(`INSERT INTO monitor_checks(id,monitor_id,team_id,trigger,status) VALUES($1,$2,$3,'manual','queued')`, [open,busy.id,busy.team]);
    assert.equal((await prune(db, root)).monitors_busy, 1);
    assert.equal(await exists(busy.obsoleteKey), true);
    await db.query("UPDATE monitor_checks SET status='completed',finished_at=now() WHERE id=$1", [open]);
    // A row lock acquired by another native writer is skipped, not waited through.
    await other.query('BEGIN'); await other.query('SELECT id FROM monitors WHERE id=$1 FOR UPDATE', [busy.id]);
    assert.equal((await prune(db, root)).monitors_busy, 1);
    await other.query('ROLLBACK');

    // Reproduce the insertion race: FK KEY SHARE must wait for the pruning row lock.
    let release; const gate = new Promise(r => { release = r; });
    let reached; const locked = new Promise(r => { reached = r; });
    const wrapped = { query: async (...args) => {
      const result = await db.query(...args);
      if (String(args[0]).startsWith('SELECT *, status=') && args[1][0] === busy.id) { reached(); await gate; }
      return result;
    }};
    const deleting = prune(wrapped, root);
    await locked;
    const newCheck = randomUUID(); let inserted = false;
    const writing = other.query(`INSERT INTO monitor_checks(id,monitor_id,team_id,trigger,status) VALUES($1,$2,$3,'manual','queued')`, [newCheck,busy.id,busy.team]).then(() => { inserted = true; });
    await new Promise(r => setTimeout(r, 100)); assert.equal(inserted, false);
    release(); await deleting; await writing;
    assert.equal(await exists(busy.baselineKey), true);
    await db.query("UPDATE monitor_checks SET status='completed',finished_at=now() WHERE id=$1", [newCheck]);

    // Failed unlink is durable and retried on the next pass; symlinks are never followed.
    const badJob = randomUUID(), badKey = localJobArtifactKey(badJob);
    const outside = path.join(root, 'outside-kept.json'); await fs.writeFile(outside, 'keep');
    await fs.symlink(outside, path.join(root, badKey));
    await db.query('INSERT INTO self_hosted_artifact_gc(artifact_key,monitor_id,job_id) VALUES($1,$2,$3)', [badKey,m.id,badJob]);
    assert.equal((await prune(db, root)).failures, 1);
    assert.equal(await fs.readFile(outside, 'utf8'), 'keep');
    assert.equal((await db.query('SELECT 1 FROM self_hosted_artifact_gc WHERE artifact_key=$1', [badKey])).rowCount, 1);
    await fs.unlink(path.join(root,badKey)); await fs.writeFile(path.join(root,badKey),'retry');
    assert.equal((await prune(db, root)).artifacts_deleted, 1);
    const retired = await fixture('deleted');
    await db.query('UPDATE monitor_checks SET created_at=$1,finished_at=$1 WHERE monitor_id=$2', [old,retired.id]);
    const removed = await prune(db, root);
    assert.equal(removed.failures, 0);
    assert.equal(await exists(retired.baselineKey), false);
    assert.equal((await db.query('SELECT 1 FROM monitor_pages WHERE monitor_id=$1', [retired.id])).rowCount, 0);
    assert.equal((await db.query('SELECT 1 FROM monitor_checks WHERE monitor_id=$1', [retired.id])).rowCount, 0);
    assert.equal(await exists(m.recentKey), true);
    console.log('PASS: expiry, old baseline pin, recent history, busy checks, FK insertion race, retry, symlink rejection, deleted-monitor grace');
  } finally {
    await other.end(); await db.end();
    await admin.query(`DROP DATABASE ${name} WITH (FORCE)`); await admin.end();
    await fs.rm(root, { recursive: true, force: true });
  }
});
