-- Durable outbox: expired DB history can commit even if a later file unlink fails.
-- Pinned snapshots remain queued until every baseline/history reference is gone.
CREATE TABLE IF NOT EXISTS public.self_hosted_artifact_gc (
  artifact_key text PRIMARY KEY,
  monitor_id uuid NOT NULL,
  job_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  not_before timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS self_hosted_artifact_gc_due_idx
  ON public.self_hosted_artifact_gc(not_before, created_at);
