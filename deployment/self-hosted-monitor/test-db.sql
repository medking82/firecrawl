-- psql -X -v ON_ERROR_STOP=1 -f test-db.sql
-- Run ONLY on a disposable isolated monitor database after 001-monitor.sql.
-- Default contract phase is rolled back. Concurrency fixture phases commit;
-- require an otherwise empty monitors table and use only fixed test UUIDs.
-- Real two-session SKIP LOCKED test (launch A and B concurrently, wait both):
--   psql ... -v phase=setup    -f test-db.sql
--   psql ... -v phase=worker_a -f test-db.sql   # background, holds row lock
--   psql ... -v phase=worker_b -f test-db.sql   # waits for A's pg_sleep
--   psql ... -v phase=verify   -f test-db.sql
--   psql ... -v phase=cleanup  -f test-db.sql   # also run on worker failure
-- Each command needs the SAME connection/database. No dblink extension needed.
\set ON_ERROR_STOP on
\if :{?phase}
\else
  \set phase contract
\endif
SELECT :'phase' = 'contract' AS contract, :'phase' = 'setup' AS setup,
       :'phase' = 'worker_a' AS worker_a, :'phase' = 'worker_b' AS worker_b,
       :'phase' = 'verify' AS verify, :'phase' = 'cleanup' AS cleanup,
       :'phase' IN ('contract','setup','worker_a','worker_b','verify','cleanup') AS valid_phase
\gset
\if :valid_phase
\else
  \echo 'Unknown test phase'
  \quit 1
\endif
SET statement_timeout = '20s';
SET lock_timeout = '3s';
CREATE OR REPLACE FUNCTION pg_temp.assert(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $$ BEGIN
  IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'DB contract: %', message; END IF;
END $$;

\if :contract
BEGIN;
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.monitors), 'requires disposable DB with no monitors');
-- Same projection used by initializeBlocklist. Startup also requires a global
-- row: create test policy inside this transaction only, never seed production.
SELECT * FROM public.blocklist;
INSERT INTO public.blocklist(data) VALUES ('{"blocklist":["contract.example"],"allowedKeywords":[]}');
SELECT pg_temp.assert(EXISTS (SELECT 1 FROM public.blocklist WHERE org_id IS NULL), 'global startup blocklist row');
INSERT INTO public.blocklist_hits(id, domain, url, origin)
VALUES (gen_random_uuid(), 'contract.example', 'https://contract.example', 'contract');
SELECT pg_temp.assert(EXISTS (SELECT 1 FROM public.blocklist_hits WHERE domain = 'contract.example'
  AND created_at IS NOT NULL), 'authenticated blocklist hit insert and default');
INSERT INTO public.teams(id, name) VALUES
  ('f6210000-0000-4000-8000-000000000001','contract tenant A'),
  ('f6210000-0000-4000-8000-000000000002','contract tenant B');
INSERT INTO public.api_keys(key, team_id) VALUES
  ('f6210000-0000-4000-8000-000000000003','f6210000-0000-4000-8000-000000000001'),
  ('f6210000-0000-4000-8000-000000000004','f6210000-0000-4000-8000-000000000002');
SELECT pg_temp.assert((SELECT count(*) = 1 AND bool_and(team_id = 'f6210000-0000-4000-8000-000000000001'::uuid
  AND org_id IS NULL AND NOT is_banned AND flags = '{}'::jsonb AND concurrency_limit = 2
  AND rate_limit_multiplier = 1 AND NOT is_paid_plan)
  FROM public.auth_chunk_2('f6210000-0000-4000-8000-000000000003')), 'real key and local auth return shape');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2('invalid')), 'malformed key fails closed');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2(gen_random_uuid()::text)), 'unknown key fails closed');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2(
  'f6210000-0000-4000-8000-000000000003', 'hosted_mcp_oauth')), 'purpose isolation');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2(
  'f6210000-0000-4000-8000-000000000003', NULL)), 'null purpose fails closed');
UPDATE public.teams SET banned = true WHERE id = 'f6210000-0000-4000-8000-000000000001';
SELECT pg_temp.assert((SELECT is_banned FROM public.auth_chunk_2('f6210000-0000-4000-8000-000000000003')),
  'banned is surfaced for native HTTP 403');
UPDATE public.teams SET banned = false WHERE id = 'f6210000-0000-4000-8000-000000000001';
SELECT pg_temp.assert((SELECT team_id = 'f6210000-0000-4000-8000-000000000001'::uuid AND org_id IS NULL
  FROM public.auth_chunk_2_from_team('f6210000-0000-4000-8000-000000000001')), 'team auth return shape');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2_from_team('missing')), 'unknown team fails closed');
INSERT INTO public.self_hosted_team_policy(team_id, flags, concurrency_limit, rate_limit_multiplier)
VALUES ('f6210000-0000-4000-8000-000000000001', '{"keyRestriction":"allowed"}', 3, 2);
SELECT pg_temp.assert((SELECT concurrency_limit = 3 AND rate_limit_multiplier = 2
  AND flags = '{"keyRestriction":"allowed"}'::jsonb FROM public.auth_chunk_2_from_team(
  'f6210000-0000-4000-8000-000000000001')), 'locally configured SQL policy (HTTP limits still JS-owned)');
SELECT * FROM public.bill_team_7('f6210000-0000-4000-8000-000000000001', NULL, 3,
  (SELECT id FROM public.api_keys WHERE key = 'f6210000-0000-4000-8000-000000000003'), false);
SELECT * FROM public.bill_team_7('f6210000-0000-4000-8000-000000000001', NULL, 2, NULL, false);
SELECT pg_temp.assert((SELECT count(*) = 2 AND sum(credits) = 5 FROM public.self_hosted_usage_ledger
  WHERE team_id = 'f6210000-0000-4000-8000-000000000001'), 'durable actual usage ledger');
DO $$ BEGIN
  BEGIN
    PERFORM public.bill_team_7('f6210000-0000-4000-8000-000000000001', NULL, 1,
      (SELECT id FROM public.api_keys WHERE key = 'f6210000-0000-4000-8000-000000000004'), false);
    RAISE EXCEPTION 'accepted cross-tenant billing';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;
  BEGIN
    PERFORM public.bill_team_7('f6210000-0000-4000-8000-000000000001', NULL, -1, NULL, false);
    RAISE EXCEPTION 'accepted negative usage';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
DELETE FROM public.api_keys WHERE key = 'f6210000-0000-4000-8000-000000000003';
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.auth_chunk_2('f6210000-0000-4000-8000-000000000003')),
  'deleted key revoked without deleting usage history');
SELECT pg_temp.assert((SELECT sum(credits) = 5 FROM public.self_hosted_usage_ledger
  WHERE team_id = 'f6210000-0000-4000-8000-000000000001'), 'usage survives key revocation');

INSERT INTO public.monitors(id, team_id, name, schedule_cron, next_run_at, status, deleted_at, locked_until, current_check_id)
SELECT ('f6211000-0000-4000-8000-' || lpad(n::text, 12, '0'))::uuid,
  'f6210000-0000-4000-8000-000000000001', 'contract ' || n, '0 * * * *',
  CASE WHEN n = 9 THEN NULL WHEN n = 4 THEN now() + interval '1 day' ELSE now() - make_interval(hours => 10-n) END,
  CASE WHEN n = 5 THEN 'paused' WHEN n = 6 THEN 'deleted' ELSE 'active' END,
  CASE WHEN n = 7 THEN now() END,
  CASE WHEN n = 2 THEN now() - interval '1 second' WHEN n = 8 THEN now() + interval '1 hour' END,
  CASE WHEN n = 3 THEN 'f6212000-0000-4000-8000-000000000001'::uuid END
FROM generate_series(1,9) n;
CREATE TEMP TABLE claimed AS SELECT * FROM public.monitoring_claim_due_monitors('worker-a', 2, 60);
SELECT pg_temp.assert((SELECT count(*) = 2 AND bool_and(id IN
  ('f6211000-0000-4000-8000-000000000001','f6211000-0000-4000-8000-000000000002')) FROM claimed),
  'due ordering, limit and expired lease recovery');
SELECT pg_temp.assert((SELECT bool_and(locked_at IS NOT NULL AND locked_until = locked_at + interval '60 seconds'
  AND next_run_at < locked_at AND current_check_id IS NULL AND schedule_timezone = 'UTC'
  AND retention_days = 30 AND NOT judge_enabled AND targets = '[]'::jsonb) FROM claimed),
  'lease timestamps, unchanged schedule, exact defaults and full monitor return shape');
CREATE TEMP TABLE overlap_claim AS SELECT * FROM public.monitoring_claim_due_monitors('worker-b', 10, 60);
SELECT pg_temp.assert((SELECT count(*) = 1 AND bool_and(id = 'f6211000-0000-4000-8000-000000000003'::uuid
  AND current_check_id IS NOT NULL) FROM overlap_claim), 'scheduler receives current check for overlap reconciliation');
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.monitoring_claim_due_monitors('worker-c', 10, 60)),
  'live lease, future, paused, deleted status/deleted_at and null schedule all excluded');
UPDATE public.monitors SET locked_until = statement_timestamp() - interval '1 second'
  WHERE id = 'f6211000-0000-4000-8000-000000000001';
SELECT pg_temp.assert((SELECT count(*) = 1 FROM public.monitoring_claim_due_monitors('recovery-worker', 10, 30)),
  'expired claimed lease can be recovered');
DO $$ BEGIN
  BEGIN
    PERFORM public.monitoring_claim_due_monitors('worker', 0, 60);
    RAISE EXCEPTION 'accepted zero claim limit';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM public.monitoring_claim_due_monitors('', 1, 60);
    RAISE EXCEPTION 'accepted empty worker';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM public.monitoring_claim_due_monitors('worker', 1, -1);
    RAISE EXCEPTION 'accepted negative lease';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;

INSERT INTO public.monitor_checks(id, monitor_id, team_id, trigger) VALUES
  ('f6212000-0000-4000-8000-000000000001','f6211000-0000-4000-8000-000000000001',
   'f6210000-0000-4000-8000-000000000001','manual');
SELECT pg_temp.assert((SELECT status = 'queued' AND billing_status = 'not_applicable' AND total_pages = 0
  AND same_count = 0 AND error_count = 0 FROM public.monitor_checks
  WHERE id = 'f6212000-0000-4000-8000-000000000001'), 'check defaults');
INSERT INTO public.monitor_pages(monitor_id, team_id, target_id, url, url_hash, source, last_status)
VALUES ('f6211000-0000-4000-8000-000000000001','f6210000-0000-4000-8000-000000000001',
  'target', 'https://example.com', decode(repeat('01',32),'hex'), 'explicit', 'new');
INSERT INTO public.monitor_pages(monitor_id, team_id, target_id, url, url_hash, source, last_status)
VALUES ('f6211000-0000-4000-8000-000000000001','f6210000-0000-4000-8000-000000000001',
  'target', 'https://example.com', decode(repeat('01',32),'hex'), 'explicit', 'same')
ON CONFLICT (monitor_id, target_id, url_hash) DO UPDATE SET last_status = excluded.last_status;
SELECT pg_temp.assert((SELECT count(*) = 1 AND bool_and(last_status = 'same') FROM public.monitor_pages
  WHERE monitor_id = 'f6211000-0000-4000-8000-000000000001'), 'native bulk upsert unique conflict target');
DO $$ BEGIN
  BEGIN
    INSERT INTO public.monitor_checks(id, monitor_id, team_id, trigger) VALUES
      (gen_random_uuid(),'f6211000-0000-4000-8000-000000000001','f6210000-0000-4000-8000-000000000002','manual');
    RAISE EXCEPTION 'accepted cross-tenant check';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;
END $$;
ROLLBACK;
\echo 'PASS: transactional auth, usage, schema and lease contract (rolled back)'
\endif

\if :setup
BEGIN;
SELECT pg_temp.assert(NOT EXISTS (SELECT 1 FROM public.monitors), 'concurrency fixture requires empty monitors');
CREATE TABLE public.self_hosted_monitor_contract_claims(worker text PRIMARY KEY, monitor_id uuid NOT NULL UNIQUE);
INSERT INTO public.teams(id, name) VALUES ('f6210000-0000-4000-8000-000000000001','concurrency contract');
INSERT INTO public.monitors(id, team_id, name, schedule_cron, next_run_at)
SELECT ('f6211000-0000-4000-8000-' || lpad(n::text,12,'0'))::uuid,
  'f6210000-0000-4000-8000-000000000001', 'concurrency ' || n, '0 * * * *', now() - make_interval(hours => 3-n)
FROM generate_series(1,2) n;
COMMIT;
\endif

\if :worker_a
BEGIN;
SET LOCAL application_name = 'fc_monitor_contract_worker_a';
INSERT INTO public.self_hosted_monitor_contract_claims
  SELECT 'worker-a', id FROM public.monitoring_claim_due_monitors('worker-a', 1, 60);
SELECT pg_temp.assert((SELECT count(*) = 1 FROM public.self_hosted_monitor_contract_claims WHERE worker = 'worker-a'), 'worker A claimed one');
-- Signal via pg_stat_activity only after the claim and its row lock exist.
SELECT pg_sleep(5);
COMMIT;
\endif

\if :worker_b
-- READ COMMITTED snapshots, bounded wait; do not confuse a sequential run with
-- a concurrency pass. A must be holding its row lock inside pg_sleep.
DO $$ DECLARE ready boolean := false; BEGIN
  FOR i IN 1..100 LOOP
    PERFORM pg_stat_clear_snapshot();
    SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database()
      AND application_name = 'fc_monitor_contract_worker_a' AND wait_event = 'PgSleep') INTO ready;
    EXIT WHEN ready;
    PERFORM pg_sleep(0.05);
  END LOOP;
  IF NOT ready THEN RAISE EXCEPTION 'worker A must be running concurrently in pg_sleep'; END IF;
END $$;
BEGIN;
SET LOCAL statement_timeout = '2s';
INSERT INTO public.self_hosted_monitor_contract_claims
  SELECT 'worker-b', id FROM public.monitoring_claim_due_monitors('worker-b', 1, 60);
SELECT pg_temp.assert((SELECT count(*) = 1 FROM public.self_hosted_monitor_contract_claims WHERE worker = 'worker-b'), 'worker B skips row locked by A without blocking');
COMMIT;
\endif

\if :verify
BEGIN;
SELECT pg_temp.assert((SELECT count(*) = 2 AND count(DISTINCT monitor_id) = 2
  FROM public.self_hosted_monitor_contract_claims), 'two workers claimed distinct monitors');
SELECT pg_temp.assert((SELECT monitor_id = 'f6211000-0000-4000-8000-000000000001'::uuid
  FROM public.self_hosted_monitor_contract_claims WHERE worker = 'worker-a'), 'A took oldest due row');
SELECT pg_temp.assert((SELECT monitor_id = 'f6211000-0000-4000-8000-000000000002'::uuid
  FROM public.self_hosted_monitor_contract_claims WHERE worker = 'worker-b'), 'B skipped A locked row');
ROLLBACK;
\echo 'PASS: two concurrent PostgreSQL sessions, distinct claims, SKIP LOCKED'
\endif

\if :cleanup
BEGIN;
DELETE FROM public.monitors WHERE team_id = 'f6210000-0000-4000-8000-000000000001';
DELETE FROM public.teams WHERE id = 'f6210000-0000-4000-8000-000000000001';
DROP TABLE IF EXISTS public.self_hosted_monitor_contract_claims;
COMMIT;
\endif
