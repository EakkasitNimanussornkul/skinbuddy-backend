-- The tables migration 0014's delete_user_account() deletes from, added to the
-- replica of replica_schema.sql.
--
-- Reconstructed, not dumped: only the columns the function and the routes touch,
-- plus the foreign keys that decide its order. Every foreign key to users is
-- NO ACTION on purpose (the live delete rules are not known), so the tests prove
-- the function deletes each row itself and does not lean on a cascade.
-- routine_step_completions.step_id is ON DELETE SET NULL, as 005_preserve_completion_history.sql made it.

create table public.shelf_items (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id),
  product_id uuid references public.products(id),
  status     text not null default 'active',
  created_at timestamptz not null default now()
);

create table public.routines (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id),
  is_active  boolean not null default true,
  created_at timestamptz not null default now()
);

create table public.routine_steps (
  id            uuid primary key default gen_random_uuid(),
  routine_id    uuid not null references public.routines(id),
  shelf_item_id uuid references public.shelf_items(id),
  product_id    uuid references public.products(id),
  step_order    integer not null default 1,
  time_of_day   text
);

create table public.routine_step_completions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id),
  step_id     uuid references public.routine_steps(id) on delete set null,
  product_id  uuid references public.products(id) on delete set null,
  period_key  text not null,
  time_of_day text not null default 'both'
);

create table public.skin_logs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.users(id),
  week_start date not null,
  symptoms   jsonb,
  notes      text
);

create table public.skin_analysis_reports (
  id      uuid primary key default gen_random_uuid(),
  log_id  uuid not null references public.skin_logs(id),
  user_id uuid not null references public.users(id),
  summary text
);

create table public.quiz_results (
  id      uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id),
  answers jsonb
);
