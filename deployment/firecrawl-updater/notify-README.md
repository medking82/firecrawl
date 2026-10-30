# Local Advisory Report Notifications

The deployed GA401 service uses `--record-only --updater-report ~/.local/state/firecrawl-security-refresh/latest.json`, per the user's record-only preference. This persists deduplicated events with `delivery: recorded` and never invokes `notify-send`. The delivery code below describes the optional desktop mode, not the deployed configuration.

`notify.py` is a standalone, one-shot consumer of the existing `advisory_owner.py` report. It reads `~/.local/state/firecrawl-advisory/reports/latest.json` and writes only its own state and events under `~/.local/state/firecrawl-advisory/notifications`. It does not scan, contact feeds, change reports, invoke Docker, change updater admission, install timers, or deploy. No email, Slack, webhook, or other external messaging is implemented. The only notification command is the local `notify-send` executable from the invoking user's trusted PATH.

Run manually in the scan user's Linux desktop session:

```sh
python3 -B deployment/firecrawl-updater/notify.py
# Optional explicit paths, with trusted parent directories:
python3 -B deployment/firecrawl-updater/notify.py --report /private/reports/latest.json --state-dir /private/notifications
```

No scheduling or owner-service integration is included. The consumer must be invoked again to observe a new report or retry delivery. The desktop account needs access to the report and notification daemon/session bus. A headless system service usually lacks that desktop session. Windows runtime storage is deliberately rejected: this implementation enforces POSIX ownership and permissions, not Windows ACLs. Keep the report, notification state and event directory under trusted ancestors owned by the account; root and processes running as that same account remain trusted. Leaf directories must be owned by the user and mode 0700 (or stricter), and input/state files must be private regular files with no group/other access. Leaf symlinks are refused. This is not protection against hostile concurrent mutation of ancestor directories by the same user.

## Optional Security Refresh Report

```sh
python3 -B deployment/firecrawl-updater/notify.py --updater-report "$HOME/.local/state/firecrawl-security-refresh/latest.json"
```

This option additionally reads the security refresh owner's report; without it, the updater path is never accessed. Only `status: error`, `updated`, and `already-current` are accepted. An initial success silently establishes a baseline; failures notify, repeated failures deduplicate, and recovery notifies. `updated` and `already-current` both normalize to healthy, so switching between them creates neither notifications nor events. Arbitrary error text, checks and transaction paths are excluded from fingerprints, events and desktop copy. Missing, unreadable, malformed or unknown-status reports are distinct fixed error reasons and exit 2; recovery from these errors also notifies. Like advisory errors, repeated updater errors still emit stderr and exit 2 even when deduplicated.

Updater state and private events live separately in `notifications/updater/state.json` and `notifications/updater/events/`, under the same invocation lock. Each source is processed independently after locking: failure in one source does not silence the other. Aggregate exit status is 2 if either source has a report/delivery/consumer error, otherwise 1 for advisory findings, otherwise 0. The updater uses the same retry and crash semantics described below. No updater owner or report is modified. Its `latest.json` has no required completion timestamp: this option cannot establish freshness or detect an owner that stops publishing while its previous success remains on disk. It does not observe preparation progress or running processes.

## Transitions And Deduplication

The consumer expects the owner's three exact container names (`firecrawl-api-1`, `firecrawl-monitor-api-1`, `firecrawl-browser-api-1`), immutable image IDs, image statuses, HIGH/CRITICAL findings fields, matching aggregate status/count and a UTC `completed_at`. It is not a consumer of the standalone OSV prototype or raw Trivy output. It does not independently verify the scanner, DB freshness, report authenticity, image binding or feed coverage. Unknown extra report fields are ignored. Read size is capped at 8 MB; duplicate JSON keys and nonfinite numbers are rejected.

The first valid snapshot, including a clean snapshot, produces one event and desktop notification. The fingerprint includes status, container/image identities, and sorted findings (including package, installed/fixed versions, target, type, advisory ID and severity). Changed findings with the same count and changed images therefore notify. Image/finding order, scan timestamps, DB metadata and unrelated fields do not. Duplicate finding rows retain their multiplicity. A return to a previous snapshot after an intervening state is a new transition. This is consecutive-state deduplication, not permanent suppression of a previously seen vulnerability.

Missing, unreadable, malformed, partial or inconsistent reports become error snapshots. Reports older than 48 hours or more than five minutes in the future are errors too; age is checked against `completed_at`, not file mtime. A reported scan error maps to the fixed `scan_failed` reason; arbitrary upstream error text is not copied. Repeated identical error reasons deduplicate, even if upstream error text changes. Different error reasons and recovery notify. An unchanged error still prints a diagnostic and exits 2 on every invocation. Without another invocation, a stopped scanner cannot be detected. A scanner failure that fails to replace an existing recent report cannot be detected until that report expires; no service exit status is observed.

## Events And Failure Handling

`state.json` and `events/<random-event-id>.json` are atomically replaced using private temporary files, mode 0600, file fsync and directory fsync. The event holds version, event ID, creation time, fingerprint, normalized snapshot, and delivery state (`pending`, `failed`, `accepted`). Normalized findings and image identities remain private on disk; desktop content contains only fixed text and finding counts. Events do not include raw reports, DB data, arbitrary error strings, credentials or process output. Event history has no automatic retention/deletion; plan disk management separately.

A private `flock` serializes invocations using the same state directory; contention exits 2. Pending state is saved before the event, and the private event must be published before attempting desktop notification. `notify-send` has a 15-second timeout; missing executable, timeout and nonzero exit retain pending state and mark the same event failed. The next invocation retries that event without creating another one. A changed snapshot supersedes a pending delivery; the previous event stays in history and is not retried. The script never resets corrupt state silently: it exits 2 and leaves it for manual inspection. Unwritable state/events, failed acknowledgement or lock failure also exit 2 with a sanitized stderr diagnostic. If private storage itself fails, an event cannot be guaranteed; stderr and the exit status are the remaining signals and must be observed by the caller.

`accepted` means `notify-send` exited successfully, not that a person saw the notification. Notifications and filesystem updates cannot form one transaction: a crash or write failure after desktop acceptance but before persisted acknowledgement can cause a duplicate on retry. Delivery is at least once on retry, not exactly once. Manually removing state also resets deduplication. Atomic replacement prevents partial JSON, but multiple state/event files are not a single atomic transaction; interrupted temporary files can remain after process termination. No automatic retry loop, daemon, desktop click action or external fallback exists.

Exit 0 means a clean snapshot was handled or already acknowledged; 1 means a findings snapshot was handled or already acknowledged; 2 means scan/report error or consumer/delivery failure. A successfully notified scan error still exits 2. The caller must accept exit 1 as findings and monitor exit 2 and stderr. A successful later retry returns the snapshot's corresponding code.

## Validation

```sh
python -B -m unittest discover -s deployment/firecrawl-updater -p test_notify.py
python -B -m unittest discover -s deployment/firecrawl-updater -p 'test_advisory*.py'
```

Tests use temporary reports/events and mock desktop execution; they send no real notifications and make no network calls. On Windows, consumer tests replace only the POSIX directory guard to exercise real JSON persistence and transitions; a separate test checks that production storage fails closed. The POSIX permissions/symlink/flock test requires Linux and is skipped on Windows. Native desktop rendering/session-bus delivery, Linux durability and POSIX security are not established by a passing Windows suite. Run those checks in the intended Linux environment before operational use; this change does not install or run anything there.
