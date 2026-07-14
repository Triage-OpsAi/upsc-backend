-- ============================================================================
-- UPSC Current Affairs Practice Platform — Supabase/Postgres schema
-- Run this once in the Supabase SQL editor (or `psql $DATABASE_URL -f schema.sql`)
-- Designed to scale: uuid PKs, jsonb for flexible AI-generated content,
-- indexes on every hot lookup path, generated columns kept minimal so writes stay cheap.
-- ============================================================================

create extension if not exists pgcrypto; -- for gen_random_uuid()

-- ----------------------------------------------------------------------------
-- students: anonymous-by-default. Frontend generates a device id (uuid) and
-- stores it in localStorage; email/name are optional (added later if you add
-- real auth). No password / auth fields here on purpose — keep this table
-- pure data, do auth at the edge (Supabase Auth) later if you want it.
-- ----------------------------------------------------------------------------
create table if not exists students (
  id            uuid primary key default gen_random_uuid(),
  device_id     text unique not null,
  name          text,
  email         text unique,
  target_exam   text default 'UPSC',       -- UPSC / SSC / State PSC / etc.
  created_at    timestamptz default now(),
  last_active_at timestamptz default now()
);

alter table students add column if not exists email_verified_at timestamptz;
alter table students add column if not exists profile_completed_at timestamptz;
alter table students add column if not exists avatar_url text;
alter table students add column if not exists bio text;
alter table students add column if not exists city text;
alter table students add column if not exists suspended_until timestamptz;
alter table students add column if not exists suspension_reason text;
alter table students add column if not exists active_device_id text;
alter table students add column if not exists last_login_at timestamptz;
create sequence if not exists early_access_offer_number_seq;
alter table students add column if not exists trial_started_at timestamptz not null default now();
alter table students add column if not exists trial_ends_at timestamptz not null default (now() + interval '7 days');
alter table students add column if not exists subscription_status text not null default 'trial';
alter table students add column if not exists subscription_started_at timestamptz;
alter table students add column if not exists early_offer_number bigint default nextval('early_access_offer_number_seq');
alter table students add column if not exists early_offer_claimed_at timestamptz;
create index if not exists idx_students_email on students (email);
create index if not exists idx_students_active_device on students (active_device_id);
create index if not exists idx_students_trial_ends on students (trial_ends_at);
create unique index if not exists idx_students_early_offer_number on students (early_offer_number) where early_offer_number is not null;

-- ----------------------------------------------------------------------------
-- Email OTP + JWT session auth. One account keeps only one active session.
-- A third distinct device within the configured switch window suspends the
-- account for three days from the backend.
-- ----------------------------------------------------------------------------
create table if not exists auth_otp_codes (
  id          uuid primary key default gen_random_uuid(),
  email       text not null,
  otp_hash    text not null,
  purpose     text not null default 'login',
  attempts    int not null default 0,
  expires_at  timestamptz not null,
  consumed_at timestamptz,
  request_ip  text,
  user_agent  text,
  created_at  timestamptz default now()
);
create index if not exists idx_auth_otp_email_created on auth_otp_codes (email, created_at desc);
create index if not exists idx_auth_otp_unconsumed on auth_otp_codes (email, purpose, consumed_at);

create table if not exists auth_sessions (
  id              uuid primary key default gen_random_uuid(),
  student_id      uuid not null references students(id) on delete cascade,
  device_id       text not null,
  token_hash      text,
  expires_at      timestamptz not null,
  revoked_at      timestamptz,
  revoked_reason  text,
  request_ip      text,
  user_agent      text,
  created_at      timestamptz default now(),
  last_seen_at    timestamptz default now()
);
create index if not exists idx_auth_sessions_student_active on auth_sessions (student_id, revoked_at, expires_at);
create index if not exists idx_auth_sessions_device on auth_sessions (device_id);

create table if not exists auth_device_events (
  id          uuid primary key default gen_random_uuid(),
  student_id  uuid not null references students(id) on delete cascade,
  email       text not null,
  device_id   text not null,
  event_type  text not null,
  request_ip  text,
  user_agent  text,
  created_at  timestamptz default now()
);
create index if not exists idx_auth_device_events_student_created on auth_device_events (student_id, created_at desc);
create index if not exists idx_auth_device_events_email_created on auth_device_events (email, created_at desc);

create table if not exists auth_device_email_locks (
  device_id            text primary key,
  email                text not null,
  student_id           uuid references students(id) on delete set null,
  locked_at            timestamptz default now(),
  blocked_attempts     int not null default 0,
  last_attempted_email text,
  last_blocked_at      timestamptz,
  last_seen_at         timestamptz default now()
);
create index if not exists idx_auth_device_email_locks_email on auth_device_email_locks (email);

-- ----------------------------------------------------------------------------
-- ca_topics: one row per current-affairs event, grouped by month/year so the
-- home screen can list Jan 2025 -> present month-wise.
-- ----------------------------------------------------------------------------
create table if not exists ca_topics (
  id            uuid primary key default gen_random_uuid(),
  month         int not null check (month between 1 and 12),
  year          int not null check (year >= 2025),
  title         text not null,
  summary       text,
  subject_tags  text[] default '{}',        -- {'polity','economy','ecology',...}
  source_date   date,
  status        text default 'published',   -- draft | published
  created_at    timestamptz default now(),
  unique (year, month, title)
);
create index if not exists idx_topics_month_year on ca_topics (year, month);
create index if not exists idx_topics_status on ca_topics (status);

-- ----------------------------------------------------------------------------
-- ca_questions: the main MCQ per topic (usually 1, schema allows more).
-- ----------------------------------------------------------------------------
create table if not exists ca_questions (
  id              uuid primary key default gen_random_uuid(),
  topic_id        uuid not null references ca_topics(id) on delete cascade,
  question_text   text not null,
  options         jsonb not null,   -- [{"key":"A","text":"..."}, ...]
  correct_option  text not null,
  explanation     text,
  difficulty      text default 'medium',
  created_at      timestamptz default now()
);
create index if not exists idx_questions_topic on ca_questions (topic_id);

-- ----------------------------------------------------------------------------
-- breakdown_slides: exactly 6 per question — 3 theory + 3 practice, generated
-- once (bulk or daily cron) and reused by every student who gets it wrong.
-- ----------------------------------------------------------------------------
create table if not exists breakdown_slides (
  id                    uuid primary key default gen_random_uuid(),
  question_id           uuid not null references ca_questions(id) on delete cascade,
  slide_order           int not null check (slide_order between 1 and 6),
  slide_type            text not null check (slide_type in ('theory','practice')),
  subject               text not null,      -- economy | polity | history | geography | ethics | science | other
  content               text,               -- markdown theory explanation (theory slides)
  practice_question     text,               -- (practice slides)
  practice_options      jsonb,
  practice_correct_option text,
  practice_explanation  text,
  created_at            timestamptz default now(),
  unique (question_id, slide_order)
);
create index if not exists idx_slides_question on breakdown_slides (question_id);

-- ----------------------------------------------------------------------------
-- Static subject content is chapter-based and deliberately separate from the
-- month/year current-affairs tables above.
-- ----------------------------------------------------------------------------
create table if not exists subject_chapters (
  id            uuid primary key default gen_random_uuid(),
  subject_key   text not null,
  name          text not null,
  chapter_order int not null,
  created_at    timestamptz default now(),
  unique (subject_key, name)
);
create index if not exists idx_subject_chapters_subject_order
  on subject_chapters (subject_key, chapter_order);

create table if not exists subject_questions (
  id             uuid primary key default gen_random_uuid(),
  chapter_id     uuid not null references subject_chapters(id) on delete cascade,
  question_text  text not null,
  options        jsonb not null,
  correct_option text not null,
  explanation    text not null,
  difficulty     text default 'very_hard',
  format         text check (format in ('statement','assertion_reason','negative','matching')),
  created_at     timestamptz default now()
);
create index if not exists idx_subject_questions_chapter on subject_questions (chapter_id);

create table if not exists subject_breakdown_slides (
  id                      uuid primary key default gen_random_uuid(),
  question_id             uuid not null references subject_questions(id) on delete cascade,
  slide_order             int not null check (slide_order between 1 and 4),
  slide_type              text not null check (slide_type in ('theory','practice')),
  concept                 text not null,
  content                 text,
  practice_question       text,
  practice_options        jsonb,
  practice_correct_option text,
  practice_explanation    text,
  unique (question_id, slide_order)
);
create index if not exists idx_subject_slides_question on subject_breakdown_slides (question_id);

create table if not exists student_subject_attempts (
  id                     uuid primary key default gen_random_uuid(),
  student_id             uuid not null references students(id) on delete cascade,
  question_id            uuid not null references subject_questions(id) on delete cascade,
  selected_option        text not null,
  is_correct             boolean not null,
  attempt_number         int not null default 1,
  went_through_breakdown boolean default false,
  created_at             timestamptz default now()
);
create index if not exists idx_subject_attempts_student
  on student_subject_attempts (student_id, created_at desc);
create index if not exists idx_subject_attempts_question
  on student_subject_attempts (question_id);

create table if not exists student_subject_breakdown_answers (
  id              uuid primary key default gen_random_uuid(),
  student_id      uuid not null references students(id) on delete cascade,
  slide_id        uuid not null references subject_breakdown_slides(id) on delete cascade,
  selected_option text not null,
  is_correct      boolean not null,
  created_at      timestamptz default now()
);
create index if not exists idx_subject_breakdown_answers_student
  on student_subject_breakdown_answers (student_id, created_at desc);

-- ----------------------------------------------------------------------------
-- student_attempts: every time a student answers the MAIN question.
-- attempt_number 1 = first try, 2 = retry after going through the breakdown.
-- ----------------------------------------------------------------------------
create table if not exists student_attempts (
  id                     uuid primary key default gen_random_uuid(),
  student_id             uuid not null references students(id) on delete cascade,
  question_id            uuid references ca_questions(id) on delete set null,
  selected_option        text not null,
  is_correct             boolean not null,
  attempt_number         int not null default 1,
  went_through_breakdown boolean default false,
  question_text_snapshot text,
  topic_title_snapshot   text,
  subject_tags_snapshot  text[] not null default '{}',
  content_changed        boolean not null default false,
  content_changed_at     timestamptz,
  content_change_notice  text,
  created_at             timestamptz default now()
);
create index if not exists idx_attempts_student on student_attempts (student_id, created_at desc);
create index if not exists idx_attempts_question on student_attempts (question_id);

-- ----------------------------------------------------------------------------
-- student_breakdown_answers: answers to the 3 practice slides inside a breakdown.
-- ----------------------------------------------------------------------------
create table if not exists student_breakdown_answers (
  id              uuid primary key default gen_random_uuid(),
  student_id      uuid not null references students(id) on delete cascade,
  slide_id        uuid references breakdown_slides(id) on delete set null,
  selected_option text not null,
  is_correct      boolean not null,
  practice_question_snapshot text,
  subject_snapshot text,
  content_changed boolean not null default false,
  content_changed_at timestamptz,
  created_at      timestamptz default now()
);
create index if not exists idx_breakdown_answers_student on student_breakdown_answers (student_id);

-- ----------------------------------------------------------------------------
-- daily_reports: generated once per student per night by the 1am cron job.
-- ----------------------------------------------------------------------------
create table if not exists daily_reports (
  id                     uuid primary key default gen_random_uuid(),
  student_id             uuid not null references students(id) on delete cascade,
  report_date            date not null,
  total_attempted        int default 0,
  total_correct          int default 0,
  accuracy               numeric default 0,
  percentile             numeric default 0,   -- vs all active students that day
  subject_breakdown      jsonb default '{}',  -- {"economy":{"correct":3,"total":5}, ...}
  exam_wise_readiness    jsonb default '{}',  -- {"UPSC":72,"SSC_CGL":65,...} (0-100 score)
  ai_feedback            text,
  created_at             timestamptz default now(),
  unique (student_id, report_date)
);
create index if not exists idx_reports_student_date on daily_reports (student_id, report_date desc);

-- ----------------------------------------------------------------------------
-- generation_log: idempotency + observability for bulk seed and daily crons.
-- ----------------------------------------------------------------------------
create table if not exists generation_log (
  id          uuid primary key default gen_random_uuid(),
  run_type    text not null,   -- bulk_seed | daily_content_cron | daily_report_cron
  run_date    date not null,
  status      text not null default 'started', -- started | success | failed
  details     jsonb default '{}',
  created_at  timestamptz default now(),
  unique (run_type, run_date)
);

-- ----------------------------------------------------------------------------
-- Convenience view: leaderboard-ready aggregate accuracy per student (all-time)
-- ----------------------------------------------------------------------------
create or replace view student_overall_stats as
select
  s.id as student_id,
  count(a.id) as total_attempted,
  count(a.id) filter (where a.is_correct) as total_correct,
  case when count(a.id) = 0 then 0
       else round(100.0 * count(a.id) filter (where a.is_correct) / count(a.id), 2)
  end as accuracy_pct
from students s
left join student_attempts a on a.student_id = s.id and a.attempt_number = 1
group by s.id;
