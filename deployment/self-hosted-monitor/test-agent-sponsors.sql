-- Run only on a disposable database after applying 003 twice.
BEGIN;
INSERT INTO public.agent_sponsors(email, verification_deadline, agent_name, api_key_id, verification_token)
VALUES ('fixture@example.invalid', now() + interval '1 hour', 'fixture', -987654321, 'fixture-token');
DO $$
DECLARE found_row record;
BEGIN
  SELECT status, verification_deadline, email INTO found_row FROM public.agent_sponsors
    WHERE api_key_id = -987654321 LIMIT 1;
  IF found_row.status <> 'pending' OR found_row.email <> 'fixture@example.invalid'
     OR found_row.verification_deadline IS NULL THEN
    RAISE EXCEPTION 'sponsor auth projection failed';
  END IF;
END $$;
ROLLBACK;
