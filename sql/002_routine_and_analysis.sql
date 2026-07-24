-- ============================================================================
-- SkinBuddy - Migration 002
-- Feature #5: Track Skincare Routine (UC-15..UC-22, +UC-28 adherence)
-- Feature #7: Weekly Skin Analysis (UC-23..UC-27)
-- LINE push notifications support
--
-- Run this once in the Supabase SQL editor. (If routine tables already existed
-- from an earlier system, run 003 afterwards to reconcile them.)
-- Assumes existing tables: users(id uuid), products(id uuid), shelf_items(id uuid)
-- ============================================================================

-- --- LINE notifications opt-in flag on the existing users table ---------------
alter table if exists users
  add column if not exists notifications_enabled boolean not null default true;

-- ============================================================================
-- FEATURE #5 : ROUTINE
-- ============================================================================

-- One active routine per user. Old ones are kept with is_active=false as a
-- snapshot when a generated routine replaces them (UC-15 A1).
create table if not exists routines (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  is_active   boolean not null default true,
  source      text not null default 'manual',      -- 'manual' | 'chatbot'
  created_at  timestamptz not null default now(),
  archived_at timestamptz
);
create index if not exists idx_routines_user_active on routines(user_id, is_active);

-- Ordered steps of a routine. added_at is critical: UC-24 uses it to decide
-- which products were "recently introduced".
create table if not exists routine_steps (
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
create index if not exists idx_routine_steps_routine on routine_steps(routine_id, step_order);

-- Completion records feed adherence (UC-22 / UC-28) and the weekly analysis.
-- period_key is the "which day/period" bucket (e.g. '2026-07-22').
create table if not exists routine_step_completions (
  id           uuid primary key default gen_random_uuid(),
  step_id      uuid not null references routine_steps(id) on delete cascade,
  user_id      uuid not null references users(id) on delete cascade,
  period_key   text not null,
  completed_at timestamptz not null default now(),
  unique (step_id, period_key)                       -- one completion per step per period
);
create index if not exists idx_step_completions_step on routine_step_completions(step_id, period_key);

-- ============================================================================
-- FEATURE #7 : WEEKLY SKIN ANALYSIS
-- ============================================================================

-- One structured self-report per user per week (UC-23). The unique constraint
-- powers the "already submitted this week -> update instead" flow (UC-23 A1).
create table if not exists skin_logs (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references users(id) on delete cascade,
  week_start     date not null,
  symptoms       jsonb not null default '[]'::jsonb,  -- [{ "symptom": "redness", "severity": 3 }]
  affected_areas jsonb not null default '[]'::jsonb,  -- ["cheeks", "chin"]
  notes          text,
  created_at     timestamptz not null default now(),
  unique (user_id, week_start)
);
create index if not exists idx_skin_logs_user_week on skin_logs(user_id, week_start desc);

-- The generated report for a log (UC-24), viewed in UC-25 / UC-26.
create table if not exists skin_analysis_reports (
  id                  uuid primary key default gen_random_uuid(),
  log_id              uuid not null references skin_logs(id) on delete cascade,
  user_id             uuid not null references users(id) on delete cascade,
  week_start          date not null,
  flagged_ingredients jsonb not null default '[]'::jsonb, -- [{ ingredient, product, reason }]
  trend_verdict       text,
  recommendations     jsonb not null default '[]'::jsonb, -- ["...", "..."]
  status              text not null default 'complete',   -- complete | incomplete
  created_at          timestamptz not null default now(),
  unique (log_id)
);
create index if not exists idx_reports_user_week on skin_analysis_reports(user_id, week_start desc);
