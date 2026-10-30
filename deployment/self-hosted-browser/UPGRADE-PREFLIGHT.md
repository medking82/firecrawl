# Browser v2.11.629 candidate preflight for parent review

This is a preparation record. `upgrade-v629.py` is the browser-only candidate
owner and must be independently reviewed before its `--execute` mode is used.
The existing `deployment/patched-base/promote.py` accepts only a patch whose base is
the currently running browser image. It must not be bypassed or presented as a
v2.11.629 upgrade mechanism.

## Inputs to revalidate immediately before any approved cutover

- Candidate browser image inspected on GA401: `sha256:e97dde38a74efbbbcc487a4d99e90a12bab8f8000c4c7759d6cbeabc1e9a409d`; embedded `BUILD_SHA` is `74f81da6935bcbf581480a888c2ed3fd81fc2d85` (v2.11.629). Its overlay was rebuilt from the exact `d90ce0dd` Git blobs, with LF bytes.
- Candidate base ID `sha256:b967b93629d2a1c2a46ab160058f29f5d23ae67e6e8b3880656ec4d0f8d78cfd`. The candidate's layers extend this base; package audit and transport checks remain execution gates.
- Browser overlay source must include the reviewed `hangar-compat.cjs`, `compile.cjs`, generated file list, original source modules, schemas, and the exact manager release if changed. Record hashes and compare the image's compiled bytes against those inputs.
- GA401 owner: `firecrawl-browser` Compose project, `/home/marck/services/firecrawl-browser/shared/runtime.env`, immutable release under `/home/marck/services/firecrawl-browser/releases/`, and the `firecrawl-browser-manager.service` / `firecrawl-browser-llm.service` user units. The parent coordinates core and monitor owners.

## Mandatory design and review gates

1. Prove the candidate descends from the exact admitted latest patched core ID, with source revision, full image IDs, layer sequence, actual package manifests, transport admission and isolated acceptance evidence. Do not infer these from mutable tags or labels.
2. Use the existing updater `run.lock` and `update.lock`, and browser `manager-activation.lock`, in the established order. Capture exact current image IDs, effective Compose config, runtime.env and manager unit preimages. Abort on drift or non-idle locks.
3. Require a fresh, verified private logical-backup receipt using `recovery_backup.verify_backup`, and establish a tested recovery/rollback plan for DB and image references. Check browser and queue workloads for quiescence; prevent new admissions with the existing exclusive marker and freeze/recheck manager protocol, retaining gate on ambiguous failure.
4. The upgrade owner must atomically coordinate reviewed immutable release, `BASE_API`, `BROWSER_API_IMAGE`, manager/bridge paths if applicable, and effective Compose images, without changing unrelated projects. Revalidate conditions immediately before transition. Preserve volumes, credentials, preimages and receipts.
5. Independent review must cover the new upgrade-specific admission and rollback implementation, fixture isolation, schema compatibility, candidate lineage, and concurrency. Passing syntax or mocks is not runtime acceptance. Only the parent can integrate the browser work with the monitor owner and review.

## Evidence gap

The isolated candidate API passed authentication/tenant isolation, create,
execute, CDP, delete, ZDR fail-closed, and seeded scrape replay against a
synthetic fixture page. This does not test a real scrape, natural-language
bridge, external network, every timeout or post-cutover behavior. No production
cutover was attempted. The immutable source/compiled manifest is
`candidate-v629.manifest.json` (SHA-256 `4552752c5584bc9835eb87e46926f31a8c3c561e4663d835786c69d4911528a0`);
all 14 transpiled Git blob sources matched the candidate compiled hashes. The
packaged source must use those exact LF Git blob bytes, not a CRLF checkout.
