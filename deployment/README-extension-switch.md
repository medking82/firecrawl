# Extension Switch And Rollback

`deployment/extension_switch.py` moves an installed Browser or Monitor extension to
another reviewed release directory and rolls it back when the release does not come
up. It runs as the extension owner (`marck` on GA401) and never touches the Firecrawl
core project.

```bash
python3 -B deployment/extension_switch.py monitor --check
python3 -B deployment/extension_switch.py monitor --release /home/marck/services/firecrawl-monitor/releases/<release>
python3 -B deployment/extension_switch.py monitor --rollback /home/marck/services/firecrawl-monitor/shared/switches/<snapshot>
```

Use `browser` in place of `monitor` for Browser and Interact.

## What A Switch Does

1. Verifies the target release files and exact local image IDs with the release's own
   `--release-inputs` list. A target that changes the PostgreSQL, Redis or RabbitMQ
   image is refused before anything is touched.
   When the running API image came from the security refresh (it differs from the
   `runtime.env` pin) and the target does not contain that image, the switch is also
   refused, so a release switch cannot silently undo a security rebuild. Pass
   `--replace-refreshed-image` only when that replacement is intended.
2. Saves `runtime.env`, the user units the installer rewrites, the running Compose
   files and services, and the running API image under `shared/switches/<UTC time>/`
   (mode 0700).
3. Runs the target `deploy.py --release-images ... --switch`. In switch mode the
   installer may replace only `BROWSER_API_IMAGE`, `BROWSER_IMAGE` or `MONITOR_IMAGE`.
4. Requires readiness, an authenticated `/v2/browser` or `/v2/monitor` list and the
   target API image to be running, and every unit setting that existed before
   (including drop-ins) to still be present.

The switch and its rollback hold `~/.local/state/firecrawl-security-refresh/owner.lock`,
so they never run at the same time as a security refresh.

## Operator Unit Settings

The installers own the main unit files and rewrite them. Keep operator settings such as
`MAGPIE_JUDGE_MODEL` or extra ordering in a drop-in, for example
`~/.config/systemd/user/firecrawl-monitor-judge.service.d/operator.conf`. A setting that
exists only in the main unit would be lost by the installer, so the switch treats that
as a failure and restores the previous unit.

If step 3 or 4 fails, the snapshot is restored: `runtime.env` and units are put back,
units that were active are restarted, the saved Compose files are brought up again,
and readiness, the authenticated list and the exact previous API image are checked.

Exit status: `0` switched or restored, `1` `--check` failed, `2` the target failed and
the previous release was restored, `3` the restore itself failed. Each run writes
`receipt.json` next to its snapshot. A successful switch keeps its snapshot, so
`--rollback` can return to the previous release later. Database schema files are
additive; a rollback does not drop columns or tables added by the newer release.

## Relation To Core Updates

The core updater checks Browser, Interact and Monitor through `127.0.0.1:3003` after a
core update has been committed, and records degraded extensions without rolling the
core back. Use this tool to repair or roll back the extension itself.
