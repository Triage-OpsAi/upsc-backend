begin;

create sequence if not exists early_access_offer_number_seq;

alter table students
  add column if not exists trial_started_at timestamptz not null default now(),
  add column if not exists trial_ends_at timestamptz not null default (now() + interval '7 days'),
  add column if not exists subscription_status text not null default 'trial',
  add column if not exists subscription_started_at timestamptz,
  add column if not exists early_offer_number bigint default nextval('early_access_offer_number_seq'),
  add column if not exists early_offer_claimed_at timestamptz;

update students
set trial_started_at = coalesce(trial_started_at, now()),
    trial_ends_at = coalesce(trial_ends_at, now() + interval '7 days'),
    subscription_status = coalesce(subscription_status, 'trial'),
    early_offer_number = coalesce(early_offer_number, nextval('early_access_offer_number_seq'));

select setval(
  'early_access_offer_number_seq',
  coalesce((select max(early_offer_number) from students), 1),
  exists(select 1 from students where early_offer_number is not null)
);

create index if not exists idx_students_trial_ends on students (trial_ends_at);
create unique index if not exists idx_students_early_offer_number
  on students (early_offer_number)
  where early_offer_number is not null;

commit;
