-- A local replica of the live tables migration 0013 and its functions touch.
--
-- Reconstructed, not dumped: column types, NOT NULLs and defaults come from the
-- live PostgREST OpenAPI read on 2026-10-04; keys, checks and indexes come from
-- migrations 0001-0012. Only the tables 0013 reads or writes are here.
--
-- Supabase specifics that matter to 0013 are reproduced on purpose:
--   * the anon, authenticated and service_role roles, service_role with BYPASSRLS;
--   * Supabase's default privileges, which grant ALL on new tables and EXECUTE on
--     new functions in public to all three roles. 0013 has to revoke EXECUTE from
--     anon and authenticated; without these defaults a test of that would pass
--     whether or not it did.
--   * ingredients.benefits is NOT NULL, as live (PostgREST lists it as required).

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end $$;

grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables    to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;

-- users (live columns; role from 0006)
create table public.users (
  id                    uuid primary key default gen_random_uuid(),
  line_id               text not null,
  display_name          text,
  picture_url           text,
  created_at            timestamptz default now(),
  updated_at            timestamptz default now(),
  skin_type             text,
  notifications_enabled boolean not null default true,
  role                  text not null default 'user' check (role in ('user', 'admin'))
);

-- products (live columns; slug and its unique index from 0002; source_url from 0009)
create table public.products (
  id          uuid primary key default gen_random_uuid(),
  brand       text not null,
  name        text not null,
  category    text not null,
  image_url   text,
  description text,
  price_thb   integer default 450,
  price_usd   numeric default 14.0,
  slug        text not null,
  source_url  text
);
create unique index products_slug_key on public.products (slug);

-- ingredients (live columns; source from 0003)
create table public.ingredients (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,
  benefits         text not null,
  good_for         text,
  bad_for          text,
  functional_group text,
  awareness_tier   text default 'medium',
  source           text
);

-- product_ingredients (live: the FK columns still carry the gen_random_uuid()
-- defaults 0001 removed from other tables)
create table public.product_ingredients (
  id            uuid primary key default gen_random_uuid(),
  product_id    uuid not null default gen_random_uuid() references public.products(id),
  ingredient_id uuid not null default gen_random_uuid() references public.ingredients(id)
);

-- sources and ingredient_sources (0009)
create table public.sources (
  id          uuid primary key default gen_random_uuid(),
  title       text not null,
  publisher   text,
  url         text,
  source_type text not null check (source_type in (
                'regulatory_register', 'safety_review', 'chemical_database',
                'peer_reviewed', 'reference_book', 'product_database')),
  accessed_on date,
  notes       text,
  created_at  timestamptz not null default now()
);
create unique index sources_url_key on public.sources (url);

create table public.ingredient_sources (
  ingredient_id uuid not null references public.ingredients(id) on delete cascade,
  source_id     uuid not null references public.sources(id)     on delete cascade,
  claim         text not null check (claim in ('function', 'benefits', 'good_for', 'bad_for')),
  primary key (ingredient_id, source_id, claim)
);

-- product_sources (0010)
create table public.product_sources (
  product_id uuid not null references public.products(id) on delete cascade,
  source_id  uuid not null references public.sources(id)  on delete cascade,
  claim      text not null check (claim in ('listing', 'price', 'image', 'description')),
  primary key (product_id, source_id, claim)
);

-- product_submissions (0007)
create table public.product_submissions (
  id           uuid not null default gen_random_uuid() primary key,
  submitted_by uuid not null references public.users(id),
  status       text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  payload      jsonb not null,
  review_notes text,
  reviewed_by  uuid references public.users(id),
  reviewed_at  timestamptz,
  created_at   timestamptz not null default now()
);
