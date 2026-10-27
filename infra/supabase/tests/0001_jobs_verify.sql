-- 0001_jobs_verify.sql — 적용 후 카탈로그 점검. 읽기 전용(SELECT 만)이라 MCP read_only 상태에서도 실행 가능.
-- 모든 행의 ok 가 true 여야 한다. false 면 detail 이 실제 값.
with t(name) as (values ('jobs'), ('job_steps'), ('feedback'), ('copyright_matches'), ('labels')),
cls as (
  select c.oid, c.relname, c.relrowsecurity, c.relacl
  from pg_class c join t on t.name = c.relname
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
),
client_privs as (     -- anon/authenticated 의 테이블 권한
  select string_agg(a.grantee::regrole || ':' || c.relname || ':' || a.privilege_type, ','
                    order by c.relname, a.privilege_type) v
  from cls c cross join aclexplode(c.relacl) a
  where a.grantee in ('anon'::regrole, 'authenticated'::regrole)
),
col_privs as (        -- 컬럼 단위 권한
  select string_agg(a.grantee::regrole || ':' || c.relname || '.' || att.attname || ':' || a.privilege_type, ','
                    order by att.attname) v
  from cls c join pg_attribute att on att.attrelid = c.oid and att.attacl is not null
  cross join aclexplode(att.attacl) a
  where a.grantee in ('anon'::regrole, 'authenticated'::regrole)
),
seq_privs as (
  select count(*) n
  from pg_class s cross join aclexplode(s.relacl) a
  where s.relnamespace = 'public'::regnamespace and s.relkind = 'S'
    and s.relname in ('job_steps_id_seq', 'feedback_id_seq', 'copyright_matches_id_seq', 'labels_id_seq')
    and a.grantee in ('anon'::regrole, 'authenticated'::regrole)
),
svc as (              -- service_role 은 다섯 테이블 모두 읽기·쓰기
  select count(*) filter (where has_table_privilege('service_role', c.oid, 'SELECT,INSERT,UPDATE,DELETE')) n
  from cls c
),
pol as (
  select string_agg(tablename || ':' || policyname || ':' || cmd || ':' || array_to_string(roles, '+'), ','
                    order by tablename, policyname) v
  from pg_policies where schemaname = 'public'
)
select 'tables: 5개 존재' as check, (select count(*) from cls) = 5 as ok, (select count(*) from cls)::text as detail
union all
select 'RLS: 5개 모두 켜짐', (select bool_and(relrowsecurity) from cls),
       (select string_agg(relname, ',') from cls where not relrowsecurity)
union all
select 'policies: 정확히 4개', (select v from pol) =
       'copyright_matches:copyright_matches_select_own:SELECT:authenticated,'
       'feedback:feedback_insert_own:INSERT:authenticated,'
       'feedback:feedback_select_own:SELECT:authenticated,'
       'jobs:jobs_select_own:SELECT:authenticated', (select v from pol)
union all
select 'anon/authenticated 테이블 권한: SELECT 3개만', (select v from client_privs) =
       'authenticated:copyright_matches:SELECT,authenticated:feedback:SELECT,authenticated:jobs:SELECT',
       (select v from client_privs)
union all
select 'authenticated feedback INSERT 컬럼 5개만', (select v from col_privs) =
       'authenticated:feedback.comment:INSERT,authenticated:feedback.job_id:INSERT,'
       'authenticated:feedback.target:INSERT,authenticated:feedback.target_ref:INSERT,'
       'authenticated:feedback.verdict:INSERT', (select v from col_privs)
union all
select 'anon/authenticated 시퀀스 권한 없음', (select n from seq_privs) = 0, (select n from seq_privs)::text
union all
select 'service_role 5개 테이블 읽기·쓰기', (select n from svc) = 5, (select n from svc)::text
union all
select 'service_role BYPASSRLS', (select rolbypassrls from pg_roles where rolname = 'service_role'), null;
