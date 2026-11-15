# WSL image artifact cutover

The normal updater retains its source-build behavior and existing automatic policy. A controlled no-build cutover can supply a private local JSON descriptor using `FIRECRAWL_CANDIDATE_ARTIFACT` and its independently verified SHA256 in `FIRECRAWL_CANDIDATE_ARTIFACT_SHA256`. Import the WSL-built base and patched images with `docker load` before invoking the updater. Do not set these variables permanently on the timer.

The schema-1 descriptor binds `tag`, upstream `commit`, `guard_sha256`, `base_image`, `patched_image`, `qualification_host` (`PC WSL Ubuntu-26.04`), `qualification_exit` (0) and `patch_inputs` (the SHA256 of every embedded patch-context file). Its ancestry must contain no symlinks; the descriptor must be owned by the invoking UID, have mode 0600 and one hard link. The supplied SHA256 is the trust anchor for this controlled artifact, not a signature or a remote attestation authority. Keep the archive hash, complete source manifest and test logs in the release receipt.

The artifact branch skips both source and patch builds. It still checks image IDs, inherited build SHA, exact embedded patch inputs, base filesystem ancestry and runtime metadata; the normal package, transport, configuration and search checks then produce the same admission receipt. Approval, cutover, exact-image rollback and quarantine remain in the existing updater. Unknown source, guard, configuration, executable or dependency drift is refused.

For a host where builds are forbidden during acceptance, set `FIRECRAWL_SKIP_SECURITY_REFRESH=1` for this invocation. The updater explicitly reports the security refresh as deferred and preserves the separate security timer. Default behavior still performs post-cutover security refresh. This option does not establish a security refresh pass.

Qualification runs on PC WSL. `deployment.test_firecrawl_artifact` covers descriptor digest, source, guard, patch inputs, mutable image names, failed qualification and default/explicit refresh behavior; actual-image admission and hosted-shell checks remain separate required evidence.
