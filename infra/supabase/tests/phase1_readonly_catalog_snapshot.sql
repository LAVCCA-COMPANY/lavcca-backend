-- LAVCCA Phase 1: READ-ONLY dev catalog snapshot.
-- Run only with an authorized read-only SQL session against the intended dev project.
-- Do not paste customer rows, secrets or credential values into the report.
-- This script does not change data or schema.
BEGIN READ ONLY;
SELECT current_database() AS database_name, current_user AS connected_role, current_timestamp AS captured_at;

-- Current table inventory, RLS state, ownership and type.
SELECT n.nspname AS schema_name, c.relname AS object_name, c.relkind AS kind,
       c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS force_rls,
       pg_get_userbyid(c.relowner) AS owner_role
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m')
ORDER BY n.nspname, c.relname;

-- Columns and nullability/types.
SELECT table_schema, table_name, ordinal_position, column_name, data_type,
       udt_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

-- PK/FK/UQ/CHECK constraints with actual PostgreSQL definitions.
SELECT n.nspname AS schema_name, c.relname AS table_name, co.conname, co.contype,
       pg_get_constraintdef(co.oid, true) AS definition
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
ORDER BY c.relname, co.contype, co.conname;

-- Effective RLS policies.
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;

-- Explicit grants (information_schema may omit grants not visible to current role).
SELECT table_schema, table_name, grantee, privilege_type, is_grantable
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
ORDER BY table_name, grantee, privilege_type;

-- Effective authorization checks for baseline tables and roles.
-- No secrets or user rows are accessed.
WITH tbl(name) AS (VALUES ('jobs'), ('job_steps'), ('feedback'), ('copyright_matches'), ('labels')),
     roles(role_name) AS (VALUES ('anon'), ('authenticated'), ('service_role'))
SELECT t.name AS table_name, r.role_name,
       has_table_privilege(r.role_name, format('public.%I', t.name), 'SELECT') AS can_select,
       has_table_privilege(r.role_name, format('public.%I', t.name), 'INSERT') AS can_insert,
       has_table_privilege(r.role_name, format('public.%I', t.name), 'UPDATE') AS can_update,
       has_table_privilege(r.role_name, format('public.%I', t.name), 'DELETE') AS can_delete,
       has_table_privilege(r.role_name, format('public.%I', t.name), 'TRUNCATE') AS can_truncate
FROM tbl t CROSS JOIN roles r
ORDER BY t.name, r.role_name;

COMMIT;
