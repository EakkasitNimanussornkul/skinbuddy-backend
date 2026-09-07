-- 006_session_completions.sql
-- UC-22 / UC-27 — per-session (AM/PM) routine completions.
--
-- PROBLEM
--   A product set to "both" is ONE routine step, but it is two daily tasks:
--   a morning application and an evening one. Completions were unique on
--   (step_id, period_key), so a single tick marked the whole day done and there
--   was no way to record "did the morning, skipped the evening" — the history
--   showed the product as fully done.
--
-- FIX
--   Store the ticked session in routine_step_completions.time_of_day ("AM" or
--   "PM") and widen the uniqueness to (step_id, period_key, time_of_day). A
--   "both" step can then hold one AM row and one PM row for the same day, and
--   the adherence view counts it as two tasks (so a half-done day reads as
--   "partial"). The time_of_day column already exists (added in 004); this only
--   changes what goes in it and the uniqueness around it.
--
-- Run this in the Supabase SQL editor. Idempotent.

begin;

-- 1. A completion must always name its session. Legacy rows (whole-day ticks)
--    keep the "both" marker, which the backend treats as satisfying AM and PM.
update routine_step_completions
   set time_of_day = 'both'
 where time_of_day is null;

alter table routine_step_completions
    alter column time_of_day set default 'both';

-- 2. Drop the old (step_id, period_key) uniqueness — whatever it is named, and
--    whether it is a constraint or a bare unique index.
do $$
declare r record;
begin
  for r in
    select c.conname
      from pg_constraint c
     where c.conrelid = 'routine_step_completions'::regclass
       and c.contype  = 'u'
       and (
         select array_agg(a.attname order by a.attname)
           from unnest(c.conkey) k
           join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k
       ) = array['period_key', 'step_id']
  loop
    execute format('alter table routine_step_completions drop constraint %I', r.conname);
  end loop;

  for r in
    select i.relname
      from pg_index x
      join pg_class i on i.oid = x.indexrelid
     where x.indrelid = 'routine_step_completions'::regclass
       and x.indisunique
       and not x.indisprimary
       and (
         select array_agg(a.attname order by a.attname)
           from unnest(x.indkey) k
           join pg_attribute a on a.attrelid = x.indrelid and a.attnum = k
       ) = array['period_key', 'step_id']
  loop
    execute format('drop index if exists %I', r.relname);
  end loop;
end $$;

-- 3. New per-session uniqueness. The upsert targets
--    on_conflict="step_id,period_key,time_of_day", which needs a UNIQUE
--    CONSTRAINT (not just an index).
alter table routine_step_completions
    drop constraint if exists routine_step_completions_step_period_session_key;
alter table routine_step_completions
    add constraint routine_step_completions_step_period_session_key
    unique (step_id, period_key, time_of_day);

comment on column routine_step_completions.time_of_day is
    'The session this tick was for: "AM" or "PM" (or legacy "both" = whole day). '
    'Part of the uniqueness so a "both" step can be completed per session (UC-22).';

commit;

-- Verify: no day should have two rows for the same step and session.
select step_id, period_key, time_of_day, count(*)
  from routine_step_completions
 group by step_id, period_key, time_of_day
having count(*) > 1;
