begin;

alter table student_attempts
  add column if not exists question_text_snapshot text,
  add column if not exists topic_title_snapshot text,
  add column if not exists subject_tags_snapshot text[] not null default '{}',
  add column if not exists content_changed boolean not null default false,
  add column if not exists content_changed_at timestamptz,
  add column if not exists content_change_notice text;

update student_attempts a
set question_text_snapshot = coalesce(a.question_text_snapshot, q.question_text),
    topic_title_snapshot = coalesce(a.topic_title_snapshot, t.title),
    subject_tags_snapshot = case
      when cardinality(a.subject_tags_snapshot) = 0 then coalesce(t.subject_tags, '{}')
      else a.subject_tags_snapshot
    end
from ca_questions q
join ca_topics t on t.id = q.topic_id
where a.question_id = q.id;

alter table student_attempts alter column question_id drop not null;
alter table student_attempts drop constraint if exists student_attempts_question_id_fkey;
alter table student_attempts
  add constraint student_attempts_question_id_fkey
  foreign key (question_id) references ca_questions(id) on delete set null;

alter table student_breakdown_answers
  add column if not exists practice_question_snapshot text,
  add column if not exists subject_snapshot text,
  add column if not exists content_changed boolean not null default false,
  add column if not exists content_changed_at timestamptz;

update student_breakdown_answers ba
set practice_question_snapshot = coalesce(ba.practice_question_snapshot, s.practice_question),
    subject_snapshot = coalesce(ba.subject_snapshot, s.subject)
from breakdown_slides s
where ba.slide_id = s.id;

alter table student_breakdown_answers alter column slide_id drop not null;
alter table student_breakdown_answers drop constraint if exists student_breakdown_answers_slide_id_fkey;
alter table student_breakdown_answers
  add constraint student_breakdown_answers_slide_id_fkey
  foreign key (slide_id) references breakdown_slides(id) on delete set null;

commit;
