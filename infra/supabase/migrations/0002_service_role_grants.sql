-- 0002_service_role_grants.sql — 서버(service_role) 쓰기 경로 권한 (PM 승인 후 dev 에 SQL Editor 로 실행)
--
-- 이 프로젝트에서 postgres 가 만든 public 테이블의 기본 권한은 anon/authenticated/service_role 모두
-- MAINTAIN·REFERENCES·TRIGGER·TRUNCATE 뿐이다(SELECT/INSERT/UPDATE/DELETE 없음). BYPASSRLS 는 RLS 만 건너뛰고
-- GRANT 는 대신하지 않으므로, 0001 만으로는 API·dispatcher Lambda 와 워커의 PostgREST 호출이 42501 로 실패한다.
-- 시퀀스 GRANT 는 넣지 않는다: id 4개는 GENERATED ALWAYS AS IDENTITY 라 INSERT 시 시퀀스 권한을 검사하지 않는다.
-- USAGE 를 주면 nextval() 직접 호출만 추가로 열린다(코드에서 쓰지 않음). serial 로 바꾸면 그때 USAGE 가 필요하다.
-- GRANT 는 재실행해도 결과가 같다. 되돌리기: revoke select, insert, update, delete on <같은 5개> from service_role;

grant select, insert, update, delete
  on table public.jobs, public.job_steps, public.feedback, public.copyright_matches, public.labels
  to service_role;
