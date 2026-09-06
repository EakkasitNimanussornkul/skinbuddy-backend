-- 004_completion_history.sql
-- UC-27 View Routine Completion History (SRS-103 … SRS-108)
--
-- Run this in the Supabase SQL editor. Every statement is idempotent, so
-- re-running it is safe.

-- ---------------------------------------------------------------------------
-- STEP 1 (READ FIRST — diagnostic, changes nothing)
--
-- Does deleting a routine step also delete its completion history?
-- confdeltype: 'c' = CASCADE (history is destroyed)   <-- must be fixed
--              'a' = NO ACTION      'n' = SET NULL     'r' = RESTRICT
-- ---------------------------------------------------------------------------
select
    conname            as constraint_name,
    confdeltype        as on_delete,
    pg_get_constraintdef(oid) as definition
from pg_constraint
where conrelid = 'routine_step_completions'::regclass
  and contype  = 'f';


-- ---------------------------------------------------------------------------
-- STEP 2 — keep history readable after a step or product is removed
--
-- Completions currently only reference step_id. When a step is deleted the row
-- either disappears (cascade) or survives with nothing to display. Denormalising
-- these three fields lets the per-day detail (SRS-106) still name the product,
-- and records the cadence that was in force at the time.
-- ---------------------------------------------------------------------------
alter table routine_step_completions
    add column if not exists product_id  uuid references products(id) on delete set null,
    add column if not exists time_of_day text,
    add column if not exists frequency   text;

comment on column routine_step_completions.product_id is
    'Denormalised so completion history survives the routine step being deleted (UC-27).';
comment on column routine_step_completions.frequency is
    'Cadence at the moment of completion. Not yet used: day status is computed from the '
    'step''s CURRENT frequency, so editing a frequency retroactively changes past due-counts. '
    'Stored now so that can be fixed later without another migration.';


-- ---------------------------------------------------------------------------
-- STEP 3 — history must survive a routine being replaced
--
-- GET /routine/adherence will query by (user_id, period_key) rather than by the
-- active routine's step ids, so a regenerated routine no longer wipes the
-- visible history or resets the streak (SRS-107).
-- ---------------------------------------------------------------------------
create index if not exists idx_completions_user_period
    on routine_step_completions (user_id, period_key desc);


-- ---------------------------------------------------------------------------
-- STEP 4 — backfill product_id for completions already recorded
-- ---------------------------------------------------------------------------
update routine_step_completions c
   set product_id  = s.product_id,
       time_of_day = coalesce(c.time_of_day, s.time_of_day),
       frequency   = coalesce(c.frequency,   s.frequency)
  from routine_steps s
 where s.id = c.step_id
   and c.product_id is null;


-- ---------------------------------------------------------------------------
-- STEP 5 — verify
-- ---------------------------------------------------------------------------
select count(*) filter (where product_id is null) as still_unlinked,
       count(*)                                    as total_completions
from routine_step_completions;
