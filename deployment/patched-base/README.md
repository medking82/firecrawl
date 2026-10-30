# Patched Runtime Images

This narrowly scoped process repairs selected dependency packages in an existing API image while preserving its application and overlay bytes. It is a temporary security release, not a substitute for upgrading the source and rebuilding normally. The patch list is driven by advisory ranges and exact targets in the reviewed API lockfile. Reassess advisories and update both inputs for each future incident; this is not an automatic feed of new vulnerabilities.

For each current API image, record and independently verify its immutable image ID. Tag that ID with a unique local tag, then verify `docker image inspect` returns the recorded ID immediately before building. For example:

```sh
docker tag "$BASE_IMAGE_ID" "$BASE_TAG"
test "$(docker image inspect --format '{{.Id}}' "$BASE_TAG")" = "$BASE_IMAGE_ID"
docker build --pull=false -f deployment/patched-base/Dockerfile \
  --build-arg BASE_API="$BASE_TAG" --build-arg BASE_IMAGE_ID="$BASE_IMAGE_ID" \
  -t "$PATCHED_TAG" .
```

Build from a private immutable release directory containing the exact reviewed lockfile and patch files. BuildKit reports the resolved base ID; compare it with the recorded ID as well as the labels and result digest. Labels alone are not proof of the base. The patcher checks registry tarballs against the lockfile SHA-512, checks package names, versions and unchanged dependency declarations, replaces matching pnpm packages under `/app`, and rejects unexpected layouts or affected copies. It does not execute package install scripts. The pnpm locator retains its old directory name, so inventory tools that infer versions from paths or an old lockfile may report a false positive; inspect package manifests and actual resolved links. Other filesystem roots and OS packages are outside the scan.

The installed transport guard must accept each candidate image before promotion. Run the project's real functional checks after promotion and check actual container image IDs and package manifests. `node --test deployment/patched-base/patch.test.cjs` exercises fixture behavior; it does not prove runtime compatibility. Keep the automatic updater's coordination locks and admission rules intact, retain the preimage and existing volumes, and do not assume that its future upgrades automatically inherit these patches. A future candidate needs an advisory audit and transport review before it can replace a patched image.

## Recovery cutover

`recovery_cutover.py` is a separate, high-risk host operation. It requires a reviewed release and explicit authority to interrupt the services; it is not run by these tests. The script verifies a private eight-file backup receipt with a caller-supplied maximum age, locks the updater and browser manager, waits for queues and sessions to drain, gates and freezes the browser manager, and handles each project individually. It stops the existing API, rechecks queues and database activity, then recreates only the database using the immutable image ID already in the effective Compose configuration. It checks the exact PostgreSQL volume name, pg_cron, schemas and data readability before restarting the same API container. Core also recreates only its Playwright service. Busy or unverified activity restarts the same API and fails; a failure after database recreation begins leaves the API stopped and admission gate in place for manual intervention. There is no automatic data rollback or volume deletion.

Before stopping services, it compares hashes of selected PostgreSQL binaries, extensions and SQL, and Playwright application, browser and Node files between the running containers and the recovery images. This is a bounded comparison, not a proof of every image byte or runtime behavior. A private receipt under the updater state records identifiers, volume names and configuration hashes; it does not contain the interpolated environment. Do not remove the retained evidence or another operation's admission gate. The existing `SHA256SUMS` must contain exactly the eight backups and no self-entry.

Mock tests: `python3 -B -m unittest -v test_recovery_cutover test_recovery_backup test_recovery_volume` from this directory. They do not perform a cutover or validate a real Docker/Compose environment. Linux fixture execution and the required independent review remain release gates.
