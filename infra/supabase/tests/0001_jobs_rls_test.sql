-- 0001_jobs_rls_test.sql — RLS·권한 동작 테스트. 끝에서 ROLLBACK 하므로 DB 에 아무것도 남지 않는다.
-- 실행: SQL Editor(postgres) 에 통째로 붙여넣기. 쓰기가 필요하므로 MCP read_only 에서는 첫 INSERT 에서 실패한다(정상).
-- 실패하면 ASSERT/예외 메시지에 어느 검사인지 나온다. 끝까지 오면 마지막 SELECT 가 'PASS', 0, 0 을 돌려준다.
-- 중간에 실패하면 남은 문장(ROLLBACK 포함)은 건너뛰지만 트랜잭션은 중단 상태라 커밋될 수 없다 — 데이터는 남지 않는다.
-- 파일 전체를 실행한다(일부만 선택해 실행하지 않는다).
-- 사용자 A = ...0a, B = ...0b (가짜, ROLLBACK 으로 사라짐)

begin;

-- ── 픽스처 (postgres = 테이블 소유자 → RLS 미적용) ──────────────────────
insert into auth.users (id, aud, role, email) values
  ('00000000-0000-4000-8000-00000000000a', 'authenticated', 'authenticated', 'rls-a@test.invalid'),
  ('00000000-0000-4000-8000-00000000000b', 'authenticated', 'authenticated', 'rls-b@test.invalid');
insert into public.jobs (id, user_id, status, input_key) values
  ('rlsA_ok',  '00000000-0000-4000-8000-00000000000a', 'ok',      'uploads/rlsA_ok/a.mp4'),
  ('rlsA_run', '00000000-0000-4000-8000-00000000000a', 'running', 'uploads/rlsA_run/a.mp4'),
  ('rlsB_ok',  '00000000-0000-4000-8000-00000000000b', 'ok',      'uploads/rlsB_ok/b.mp4');
insert into public.copyright_matches (job_id, raw) values ('rlsA_ok', '{}'), ('rlsB_ok', '{}');
insert into public.feedback (job_id, user_id, target, verdict)
  values ('rlsB_ok', '00000000-0000-4000-8000-00000000000b', 'overall', 'wrong');
insert into public.job_steps (job_id, name, sec) values ('rlsA_ok', 'analyze', 1.0);
insert into public.labels (job_id, text) values ('rlsA_ok', 'x');

-- ── 1. anon: 다섯 테이블 모두 접근 불가 ─────────────────────────────────
set local role anon;
do $$
declare q text;
begin
  foreach q in array array[
    'select 1 from public.jobs', 'select 1 from public.job_steps', 'select 1 from public.feedback',
    'select 1 from public.copyright_matches', 'select 1 from public.labels',
    'insert into public.feedback (job_id, target, verdict) values (''rlsA_ok'', ''overall'', ''correct'')',
    'truncate public.jobs']
  loop
    begin
      execute q;
      raise exception 'anon 성공하면 안 됨: %', q using errcode = 'P0001';
    exception when insufficient_privilege then null;   -- 42501 기대
    end;
  end loop;
end $$;
reset role;

-- ── 2. authenticated(A): 자기 것만 읽고, 피드백만 쓴다 ───────────────────
set local role authenticated;
select set_config('request.jwt.claims', '{"sub":"00000000-0000-4000-8000-00000000000a","role":"authenticated"}', true);
do $$
begin
  assert (select count(*) from public.jobs) = 2, 'A: jobs 는 자기 것 2건만';
  assert not exists (select 1 from public.jobs where id = 'rlsB_ok'), 'A: B 의 작업이 보임';
  assert (select count(*) from public.copyright_matches) = 1, 'A: copyright_matches 는 자기 작업 1건만';
  assert (select count(*) from public.feedback) = 0, 'A: B 의 피드백이 보임';
  insert into public.feedback (job_id, target, verdict, comment) values ('rlsA_ok', 'overall', 'correct', 't');
  assert (select user_id from public.feedback) = '00000000-0000-4000-8000-00000000000a', 'A: user_id 기본값 auth.uid()';
end $$;
do $$
declare q text;
begin
  foreach q in array array[
    'select 1 from public.job_steps',                                   -- 내부 전용
    'select 1 from public.labels',                                      -- 내부 전용
    'insert into public.jobs (id, user_id, input_key) values (''x'', auth.uid(), ''k'')',
    'update public.jobs set status = ''ok'' where id = ''rlsA_run''',   -- 상태는 서버만
    'delete from public.jobs where id = ''rlsA_ok''',
    'truncate public.jobs',
    'insert into public.feedback (job_id, target, verdict) values (''rlsA_run'', ''overall'', ''correct'')',  -- 끝나지 않은 작업
    'insert into public.feedback (job_id, target, verdict) values (''rlsB_ok'', ''overall'', ''correct'')',   -- 남의 작업
    'insert into public.feedback (job_id, user_id, target, verdict) values (''rlsA_ok'', ''00000000-0000-4000-8000-00000000000b'', ''overall'', ''correct'')',  -- user_id 위조
    'update public.feedback set verdict = ''wrong''',
    'delete from public.feedback',
    'insert into public.copyright_matches (job_id, raw) values (''rlsA_ok'', ''{}'')']
  loop
    begin
      execute q;
      raise exception 'authenticated 성공하면 안 됨: %', q using errcode = 'P0001';
    exception when insufficient_privilege then null;   -- 42501: 권한 없음 또는 RLS 위반
    end;
  end loop;
end $$;
reset role;

-- ── 3. authenticated(B): A 가 방금 쓴 피드백이 안 보인다 ─────────────────
set local role authenticated;
select set_config('request.jwt.claims', '{"sub":"00000000-0000-4000-8000-00000000000b","role":"authenticated"}', true);
do $$
begin
  assert (select count(*) from public.jobs) = 1, 'B: jobs 1건만';
  assert (select count(*) from public.feedback) = 1, 'B: 자기 피드백 1건만';
end $$;
reset role;

-- ── 4. service_role: RLS 우회, 워커·Lambda 쓰기 경로 ─────────────────────
set local role service_role;
select set_config('request.jwt.claims', '', true);
do $$
begin
  assert (select count(*) from public.jobs) = 3, 'service_role: jobs 전체';
  assert (select count(*) from public.feedback) = 2, 'service_role: feedback 전체';
  assert (select count(*) from public.job_steps) = 1 and (select count(*) from public.labels) = 1, 'service_role: 내부 테이블';
  -- api Lambda: uploading → queued 조건부 갱신 / dispatcher·워커: 상태 갱신
  insert into public.jobs (id, user_id, input_key)
    values ('rlsSvc', '00000000-0000-4000-8000-00000000000a', 'uploads/rlsSvc/a.mp4');
  update public.jobs set status = 'queued' where id = 'rlsSvc' and status = 'uploading';
  assert found, 'service_role: 조건부 갱신';
  update public.jobs set status = 'failed', error = 'test' where id = 'rlsA_run' and status in ('queued', 'running');
  assert found, 'service_role: task_stopped 갱신';
  insert into public.job_steps (job_id, name, sec, maxrss_mb) values ('rlsSvc', 'download', 0.5, 100.0);
  insert into public.copyright_matches (job_id, raw) values ('rlsSvc', '{}');
  begin
    update public.jobs set status = 'bogus' where id = 'rlsSvc';
    raise exception 'CHECK 제약이 status 를 막아야 함' using errcode = 'P0001';
  exception when check_violation then null;
  end;
end $$;
reset role;

rollback;

-- ROLLBACK 뒤(트랜잭션 밖)에서 실행된다. SQL Editor 는 마지막 결과만 보여 주므로 여기서 판정한다.
-- 앞에서 오류가 나면 이 문장까지 오지 않고 오류가 표시된다.
select 'PASS' as result,
       (select count(*) from auth.users where email like 'rls-_@test.invalid') as leftover_users,
       (select count(*) from public.jobs where id like 'rls%') as leftover_jobs;
