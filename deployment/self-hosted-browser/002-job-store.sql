-- Apply only to the NEW isolated browser application's DATABASE_URL database.
-- Operational ownership/replay state; no production or monitor schema changes.
BEGIN;
CREATE TABLE IF NOT EXISTS public.self_hosted_browser_jobs (
  kind text NOT NULL CHECK (kind IN ('access', 'scrape_state')),
  id uuid NOT NULL,
  value jsonb NOT NULL CHECK (jsonb_typeof(value) = 'object'),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS self_hosted_browser_jobs_expiry_idx
  ON public.self_hosted_browser_jobs (expires_at);
REVOKE ALL ON public.self_hosted_browser_jobs FROM PUBLIC;
-- The isolated API connects as owner. If a distinct API role is used, grant
-- SELECT/INSERT/UPDATE explicitly. Readers filter expiration; physical deletion
-- requires a separate retention policy and is not delegated to monitor pruning.
COMMIT;
