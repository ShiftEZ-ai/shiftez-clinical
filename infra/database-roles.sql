-- REVIEWED OPERATOR TEMPLATE. NOT EXECUTED IN THE DELIVERY ENVIRONMENT.
-- Apply only in a dedicated rota database, under institutional change control.
-- Provision rota_migrator (schema owner), rota_app (API) and rota_worker (worker) separately
-- using approved credentials/IAM. No passwords are stored in this file.
-- Migrations run as rota_migrator. API connects as rota_app; provision the worker's
-- separate rota_worker login before applying these grants, never superuser.
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rota_app, rota_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rota_app, rota_worker;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM rota_app, rota_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rota_app, rota_worker;
REVOKE UPDATE, DELETE ON audit_events, definition_history, approvals, command_receipts, review_decisions, swap_events FROM rota_app, rota_worker;
-- Upgrade rehearsals also apply these grants to the previous release's schema.
DO $$ BEGIN
    -- Only the owner-defined, person-scoped private-history redaction is callable.
    IF to_regprocedure('rota_redact_person_history(text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION rota_redact_person_history(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION rota_redact_person_history(text) TO rota_app, rota_worker;
    END IF;
    IF to_regclass('auth_login_events') IS NOT NULL THEN
        REVOKE UPDATE, DELETE ON auth_login_events FROM rota_app, rota_worker;
    END IF;
    IF to_regclass('record_insertions') IS NOT NULL THEN
        REVOKE UPDATE, DELETE ON record_insertions FROM rota_app, rota_worker;
    END IF;
    IF to_regprocedure('prune_auth_login_events(integer,integer)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION prune_auth_login_events(integer, integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION prune_auth_login_events(integer, integer) FROM rota_app, rota_worker;
        GRANT EXECUTE ON FUNCTION prune_auth_login_events(integer, integer) TO rota_worker;
    END IF;
END $$;
REVOKE UPDATE, DELETE ON publications FROM rota_app, rota_worker;
REVOKE UPDATE, DELETE ON reference_imports, source_mappings FROM rota_app, rota_worker;
DO $$ BEGIN
    IF to_regclass('public.policy_seed_lines') IS NOT NULL THEN
        REVOKE UPDATE, DELETE ON policy_seed_lines FROM rota_app, rota_worker;
    END IF;
END $$;
GRANT UPDATE (current, effective_until) ON publications TO rota_app, rota_worker;
REVOKE ALL ON alembic_version FROM rota_app, rota_worker;
GRANT SELECT ON alembic_version TO rota_app, rota_worker;
-- Reapply reviewed grants after future migrations; do not automatically grant
-- mutation rights on new evidence tables through blanket default privileges.
-- Authority/consent rows are retired, revoked or closed, never deleted by the runtime.
REVOKE DELETE ON review_routes, review_delegations, swap_requests FROM rota_app, rota_worker;
COMMIT;
