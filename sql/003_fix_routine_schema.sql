-- ============================================================================
-- SkinBuddy - Migration 003 (corrective)
--
-- Symptom: /routine/apply and /routine/analyze return 500 with
--   "Could not find the 'added_at' column of 'routine_steps'"      (PGRST204)
--   "Could not find a relationship between 'routine_steps' and 'products'" (PGRST200)
--
-- Cause: an OLDER routine system already existed (public.user_routines + a
--   public.routine_steps that had shelf_item_id but NO product_id / added_at /
--   frequency / time_of_day, and whose routine_id pointed at user_routines).
--   So migration 002's `create table if not exists routine_steps` was skipped,
--   and the app kept reading the old, incompatible table.
--
-- Fix: drop the legacy user_routines table and recreate the routine_* tables
--   with the correct structure, then reload the PostgREST schema cache. Safe in
--   development — routine data is transient. skin_logs / skin_analysis_reports
--   and migration 002's `routines` table shape are preserved/recreated identically.
--
-- Run this once in the Supabase SQL editor.
-- ============================================================================

-- Legacy routine system (unused by the current backend) — remove it so there is
-- only one routine model. CASCADE also removes the old routine_steps.
drop table if exists routine_step_completions cascade;
drop table if exists routine_steps cascade;
drop table if exists user_routines cascade;
drop table if exists routines cascade;

-- One active routine per user; replaced routines kept as snapshots (UC-15 A1).
create table routines (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  is_active   boolean not null default true,
  source      text not null default 'manual',      -- 'manual' | 'chatbot'
  created_at  timestamptz not null default now(),
  archived_at timestamptz
);
create index idx_routines_user_active on routines(user_id, is_active);

-- Ordered steps. The foreign keys to products / shelf_items are what let
-- PostgREST embed products(...) in the API selects. added_at drives the
-- "recently introduced" window used by the weekly analysis.
create table routine_steps (
  id            uuid primary key default gen_random_uuid(),
  routine_id    uuid not null references routines(id) on delete cascade,
  product_id    uuid not null references products(id),
  shelf_item_id uuid references shelf_items(id) on delete set null,
  step_order    integer not null default 1,
  frequency     text not null default 'daily',      -- daily | 3x_week | 2x_week | weekly
  time_of_day   text not null default 'both',        -- AM | PM | both
  added_at      timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
create index idx_routine_steps_routine on routine_steps(routine_id, step_order);

-- Completion records (UC-22). period_key = the day bucket, e.g. '2026-07-23'.
create table routine_step_completions (
  id           uuid primary key default gen_random_uuid(),
  step_id      uuid not null references routine_steps(id) on delete cascade,
  user_id      uuid not null references users(id) on delete cascade,
  period_key   text not null,
  completed_at timestamptz not null default now(),
  unique (step_id, period_key)
);
create index idx_step_completions_step on routine_step_completions(step_id, period_key);

-- Safety: POST /analysis/log upserts on (user_id, week_start), which needs a
-- composite UNIQUE constraint on skin_logs. Add it only if it isn't already there.
do $$
begin
  if not exists (
    select 1
    from pg_constraint c
    where c.conrelid = 'public.skin_logs'::regclass
      and c.contype = 'u'
      and c.conkey @> (
        select array_agg(a.attnum)
        from pg_attribute a
        where a.attrelid = 'public.skin_logs'::regclass
          and a.attname in ('user_id', 'week_start')
      )
  ) then
    alter table public.skin_logs
      add constraint skin_logs_user_id_week_start_key unique (user_id, week_start);
  end if;
end $$;

-- Force PostgREST (Supabase's API layer) to pick up the new columns / relationships.
notify pgrst, 'reload schema';
