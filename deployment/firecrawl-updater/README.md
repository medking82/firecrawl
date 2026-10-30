# Standalone Firecrawl updater

GA401 owns this updater independently of other applications. The existing
`firecrawl-update.service` and timer run `firecrawl_update.sh`; install a reviewed
revision with `install_firecrawl_update.py` and the exact installed preimage hash.
The installer keeps a private preimage and coordinates with the updater lock.

Each new upstream candidate is built from its source commit in an isolated
worktree, patched using integrity-checked package inputs, and audited before
cutover. The transport guard accepts only complete previously reviewed profiles;
unknown transport changes stop for review. The updater verifies that all exact
rollback images exist and preserves the running PostgreSQL and Playwright images.
The timer updates the core API only; monitor and browser overlays have separate
owners and require their own compatibility review and deployment.

The package policy covers the listed advisory ranges, not an automatic feed of
all new vulnerabilities. Reassess advisories and patch inputs for new incidents.
The current inputs cover [CVE-2026-118664](https://github.com/advisories/GHSA-qr28-fpjm-wq7f)
and [CVE-2026-116576](https://github.com/advisories/GHSA-3f7j-7jwc-fxfj).

On GA401, run the isolated tests from this directory with
`python3 -B -m unittest deployment.test_firecrawl_candidate deployment.test_firecrawl_patch deployment.test_research_update`
and check shell syntax with `bash -n deployment/firecrawl_update.sh`. After an
installation, `firecrawl-update --check-only` exercises the installed guard plus
real search and scrape without initiating an update.
