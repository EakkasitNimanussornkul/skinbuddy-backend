-- 005_preserve_completion_history.sql
-- UC-27 View Routine Completion History
--
-- PROBLEM
--   routine_step_completions.step_id was FOREIGN KEY ... ON DELETE CASCADE.
--   Removing a product from the routine (UC-18) therefore deleted every completion
--   ever recorded for it. A user's past adherence, day statuses and streak (SRS-107)
--   would silently change to reflect a routine they no longer have — the history
--   would be telling them something untrue.
--
-- FIX
--   Switch to ON DELETE SET NULL, matching the product_id constraint added in 004.
--   The completion row survives with step_id = NULL, and product_id (backfilled in
--   004) still names the product for the per-day detail view (SRS-106).
--
-- NOT CHANGED
--   user_id ... ON DELETE CASCADE is correct and stays. Deleting a user should
--   remove their completion history.
--
-- Run this in the Supabase SQL editor. Idempotent.

begin;

-- SET NULL requires the column to be nullable.
alter table routine_step_completions
    alter column step_id drop not null;

alter table routine_step_completions
    drop constraint if exists routine_step_completions_step_id_fkey;

alter table routine_step_completions
    add constraint routine_step_completions_step_id_fkey
    foreign key (step_id) references routine_steps(id) on delete set null;

comment on column routine_step_completions.step_id is
    'Null once the routine step has been deleted. The completion is still a factual '
    'record of what the user did that day; use product_id to identify the product.';

commit;
