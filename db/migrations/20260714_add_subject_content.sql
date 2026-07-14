begin;

create table if not exists subject_chapters (
  id uuid primary key default gen_random_uuid(),
  subject_key text not null,
  name text not null,
  chapter_order int not null,
  created_at timestamptz default now(),
  unique (subject_key, name)
);
create index if not exists idx_subject_chapters_subject_order
  on subject_chapters (subject_key, chapter_order);

create table if not exists subject_questions (
  id uuid primary key default gen_random_uuid(),
  chapter_id uuid not null references subject_chapters(id) on delete cascade,
  question_text text not null,
  options jsonb not null,
  correct_option text not null,
  explanation text not null,
  difficulty text default 'very_hard',
  format text check (format in ('statement','assertion_reason','negative','matching')),
  created_at timestamptz default now()
);
create index if not exists idx_subject_questions_chapter on subject_questions (chapter_id);

create table if not exists subject_breakdown_slides (
  id uuid primary key default gen_random_uuid(),
  question_id uuid not null references subject_questions(id) on delete cascade,
  slide_order int not null check (slide_order between 1 and 4),
  slide_type text not null check (slide_type in ('theory','practice')),
  concept text not null,
  content text,
  practice_question text,
  practice_options jsonb,
  practice_correct_option text,
  practice_explanation text,
  unique (question_id, slide_order)
);
create index if not exists idx_subject_slides_question on subject_breakdown_slides (question_id);

create table if not exists student_subject_attempts (
  id uuid primary key default gen_random_uuid(),
  student_id uuid not null references students(id) on delete cascade,
  question_id uuid not null references subject_questions(id) on delete cascade,
  selected_option text not null,
  is_correct boolean not null,
  attempt_number int not null default 1,
  went_through_breakdown boolean default false,
  created_at timestamptz default now()
);
create index if not exists idx_subject_attempts_student
  on student_subject_attempts (student_id, created_at desc);
create index if not exists idx_subject_attempts_question
  on student_subject_attempts (question_id);

create table if not exists student_subject_breakdown_answers (
  id uuid primary key default gen_random_uuid(),
  student_id uuid not null references students(id) on delete cascade,
  slide_id uuid not null references subject_breakdown_slides(id) on delete cascade,
  selected_option text not null,
  is_correct boolean not null,
  created_at timestamptz default now()
);
create index if not exists idx_subject_breakdown_answers_student
  on student_subject_breakdown_answers (student_id, created_at desc);

commit;
