-- 0001_jobs.sql — NATAM 작업 상태 스키마 (초안, 아직 어디에도 적용 안 함)
--
-- 적용은 JH 승인 후 dev 프로젝트에만: Supabase SQL Editor 에 붙여넣거나 `supabase db push`.
-- 원칙: 쓰기는 서버(API Lambda·워커 = service_role, RLS 우회)만 한다.
--       로그인 사용자는 자기 작업만 읽고, 피드백만 직접 쓴다.
-- 저작권 감지기 SQLite(copyright_db.sqlite) 이전은 이 파일 범위 밖(별도 마이그레이션, 백업 먼저).

-- ── 작업 ─────────────────────────────────────────────────────────────
create table public.jobs (
  id                text primary key check (id ~ '^[A-Za-z0-9_-]{1,64}$'),  -- worker.py --job-id 와 같은 규칙
  user_id           uuid not null references auth.users (id) on delete cascade,
  status            text not null default 'uploading'
                    check (status in ('uploading', 'queued', 'running', 'ok', 'partial', 'failed', 'retry_later')),
  input_key         text not null,              -- S3 uploads/{id}/{파일명}
  input_name        text,                       -- 사용자가 올린 원래 파일명(표시용)
  input_bytes       bigint,
  include_copyright boolean not null default true,
  video_sha256      text,                       -- W0 캐시 조회용(해시 + 파이프라인/모델 버전)
  pipeline_version  text,
  model_version     text,
  report_prefix     text,                       -- s3://버킷/reports/{id}/
  error             text,
  llm_cost_usd      numeric(10, 6),             -- 위기 엔진 Gemini 추정치(저작권 API 비용 미포함)
  created_at        timestamptz not null default now(),
  started_at        timestamptz,
  finished_at       timestamptz
);
create index jobs_user_created_idx on public.jobs (user_id, created_at desc);   -- 목록·일일 한도
create index jobs_cache_idx on public.jobs (video_sha256, pipeline_version, model_version)
  where status in ('ok', 'partial');

-- ── 단계별 소요·메모리 (사이징 근거, 내부 전용) ─────────────────────────
create table public.job_steps (
  id         bigint generated always as identity primary key,
  job_id     text not null references public.jobs (id) on delete cascade,
  name       text not null,                     -- download / load / analyze / copyright_wait / finalize
  sec        numeric(10, 1),
  maxrss_mb  numeric(10, 1),
  created_at timestamptz not null default now()
);
create index job_steps_job_idx on public.job_steps (job_id);

-- ── 리포트 화면 피드백 (데이터 순환 T7의 라벨 원천) ───────────────────────
create table public.feedback (
  id         bigint generated always as identity primary key,
  job_id     text not null references public.jobs (id) on delete cascade,
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  target     text not null check (target in ('overall', 'segment', 'copyright_match', 'similar_case')),
  target_ref text check (char_length(target_ref) <= 200),   -- 문장 번호 / 저작권 항목 id / 사례 id
  verdict    text not null check (verdict in ('correct', 'wrong', 'unsure')),
  comment    text check (char_length(comment) <= 2000),
  created_at timestamptz not null default now()
);
create index feedback_job_idx on public.feedback (job_id);

-- ── 저작권 탐지 항목 (리포트 JSON timeline 에서 적재, 클레임 여부 피드백과 연결) ──
create table public.copyright_matches (
  id            bigint generated always as identity primary key,
  job_id        text not null references public.jobs (id) on delete cascade,
  type          text,                           -- music / video_clip / image / logo / font
  ts            text,                           -- "00:01:23"
  title         text,
  rights_holder text,
  source        text,                           -- ACRCloud / Google Vision ...
  risk_level    text,
  risk_score    numeric,
  raw           jsonb not null,
  created_at    timestamptz not null default now()
);
create index copyright_matches_job_idx on public.copyright_matches (job_id);

-- ── 사람 라벨 (골든셋·학습용, 내부 전용) ─────────────────────────────────
create table public.labels (
  id          bigint generated always as identity primary key,
  job_id      text references public.jobs (id) on delete set null,
  segment_idx int,
  text        text not null,
  labels      text[] not null default '{}',     -- L01~L12 (config/controversy_labels.yaml)
  labeler     uuid references auth.users (id),
  source      text not null default 'review',   -- review / feedback / golden
  created_at  timestamptz not null default now()
);

-- ── RLS ──────────────────────────────────────────────────────────────
-- 정책이 없는 테이블(job_steps, labels)은 로그인 사용자에게 완전히 닫힌다(service_role 만).
alter table public.jobs              enable row level security;
alter table public.job_steps         enable row level security;
alter table public.feedback          enable row level security;
alter table public.copyright_matches enable row level security;
alter table public.labels            enable row level security;

create policy jobs_select_own on public.jobs
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy copyright_matches_select_own on public.copyright_matches
  for select to authenticated
  using (exists (select 1 from public.jobs j
                 where j.id = job_id and j.user_id = (select auth.uid())));

create policy feedback_select_own on public.feedback
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy feedback_insert_own on public.feedback
  for insert to authenticated
  with check (user_id = (select auth.uid())
              and exists (select 1 from public.jobs j
                          where j.id = job_id and j.user_id = (select auth.uid())
                            and j.status in ('ok', 'partial')));

-- 웹이 폴링 대신 Realtime 으로 상태를 받으려면(RLS 가 그대로 적용된다):
-- alter publication supabase_realtime add table public.jobs;
