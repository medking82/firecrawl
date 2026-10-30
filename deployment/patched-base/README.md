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
