-- 0001_jobs_down.sql — 0001_jobs.sql 되돌리기 (PM 승인 후, dev 전용)
--
-- migrations/ 밖에 둔다(`supabase db push` 가 적용 대상으로 집지 않게).
-- 데이터가 하나라도 있으면 멈춘다 — 작업·피드백·라벨은 되살릴 수 없다. 지워야 하면 먼저 백업.
-- 정책·인덱스·권한은 테이블과 함께 사라진다. 한 트랜잭션으로 실행된다(SQL Editor/MCP).
-- `supabase db push` 로 적용했다면 이력도 지운다: delete from supabase_migrations.schema_migrations where version = '<0001 버전>';

do $$
begin
  if exists (select 1 from public.jobs) or exists (select 1 from public.labels) then
    raise exception '0001 되돌리기 중단: jobs/labels 에 데이터가 있음';
  end if;
end $$;

drop table public.labels;
drop table public.copyright_matches;
drop table public.feedback;
drop table public.job_steps;
drop table public.jobs;
