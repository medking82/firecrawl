const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const storage = require('./dist/src/lib/local-artifacts.js');

test('local artifacts persist across module reload, atomically replace and remove', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'monitor-store-'));
  process.env.SELF_HOSTED_ARTIFACT_DIR = root;
  try {
    await storage.writeLocalArtifact('jobs/one.json', [{ markdown: 'before' }]);
    delete require.cache[require.resolve('./dist/src/lib/local-artifacts.js')];
    const reloaded = require('./dist/src/lib/local-artifacts.js');
    assert.deepEqual(await reloaded.readLocalArtifact('jobs/one.json'), [{ markdown: 'before' }]);
    await storage.writeLocalArtifact('jobs/one.json', [{ markdown: 'after' }]);
    assert.deepEqual(await reloaded.readLocalArtifact('jobs/one.json'), [{ markdown: 'after' }]);
    assert.equal((await fs.stat(path.join(root, 'jobs/one.json'))).mode & 0o777, 0o600);
    await assert.rejects(storage.writeLocalArtifact('../escape.json', {}));
    await assert.rejects(storage.readLocalArtifact('/absolute.json'));
    await storage.removeLocalArtifact('jobs/one.json');
    assert.equal(await storage.readLocalArtifact('jobs/one.json'), null);
    await fs.writeFile(path.join(root, 'broken.json'), '{broken');
    await assert.rejects(storage.readLocalArtifact('broken.json'), SyntaxError);
  } finally {
    delete process.env.SELF_HOSTED_ARTIFACT_DIR;
    await fs.rm(root, { recursive: true, force: true });
  }
});
