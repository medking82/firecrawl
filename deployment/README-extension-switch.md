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
2. Saves `runtime.env`, the user units the installer rewrites, the running Compose
   files and services, and the running API image under `shared/switches/<UTC time>/`
   (mode 0700).
3. Runs the target `deploy.py --release-images ... --switch`. In switch mode the
   installer may replace only `BROWSER_API_IMAGE`, `BROWSER_IMAGE` or `MONITOR_IMAGE`.
4. Requires readiness, an authenticated `/v2/browser` or `/v2/monitor` list and the
   target API image to be running.

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
