\set ON_ERROR_STOP on

create extension if not exists pgcrypto;
create schema if not exists auth;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role; end if;
end $$;

create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;

create table public.profiles (
  id uuid primary key,
  status text not null default 'active'
);
create function public.is_active_user(candidate uuid) returns boolean language sql stable as $$
  select exists (select 1 from public.profiles where id = candidate and status = 'active')
$$;

create table public.invites (
  id uuid primary key default gen_random_uuid(),
  expires_at timestamptz not null
);
create table public.monitors (
  id uuid primary key,
  user_id uuid not null references public.profiles(id) on delete cascade,
  status text not null default 'active',
  filters jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
create table public.flight_offers (
  id uuid primary key,
  route text not null,
  origin text not null,
  destination text not null,
  travel_date date not null,
  cabin text not null,
  airline text not null default '',
  airline_name text not null default '',
  price_pln integer,
  duration_minutes integer,
  raw jsonb not null default '{}'::jsonb,
  last_seen_at timestamptz not null default now()
);
create table public.offer_price_history (
  id bigint generated always as identity primary key,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  price_pln integer not null,
  observed_at timestamptz not null default now()
);
create table public.user_matches (
  id uuid primary key,
  user_id uuid not null references public.profiles(id) on delete cascade,
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  feedback text
);
create table public.feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  match_id uuid not null references public.user_matches(id) on delete cascade,
  verdict text not null,
  unique (user_id, match_id)
);
create table public.scan_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now()
);
create table public.telegram_auth_attempts (
  id bigint generated always as identity primary key,
  attempted_at timestamptz not null default now()
);

\ir ../supabase/migrations/20260802000500_round_trip_retention_telegram.sql
\ir ../supabase/migrations/20260802000600_durable_preferences.sql

insert into public.profiles(id) values ('00000000-0000-0000-0000-000000000001');
insert into public.monitors(id, user_id, filters) values (
  '10000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000001',
  '{"budget_pln": 5000}'
);

-- An old unrated result is disposable.
insert into public.flight_offers(id, route, origin, destination, travel_date, cabin, airline, price_pln, duration_minutes, last_seen_at)
values ('20000000-0000-0000-0000-000000000001', 'WAW → NRT', 'WAW', 'NRT', current_date - 20, 'ECONOMY', 'CA', 4500, 1140, now() - interval '50 days');
insert into public.user_matches(id, user_id, monitor_id, offer_id)
values ('30000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001');

-- A reaction creates a profile signal and protects the detailed old match.
insert into public.flight_offers(id, route, origin, destination, travel_date, cabin, airline, price_pln, duration_minutes, last_seen_at)
values ('20000000-0000-0000-0000-000000000002', 'WAW → NRT', 'WAW', 'NRT', current_date - 20, 'ECONOMY', 'CA', 4500, 1140, now() - interval '50 days');
insert into public.user_matches(id, user_id, monitor_id, offer_id)
values ('30000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000002');
insert into public.feedback(user_id, match_id, verdict)
values ('00000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000002', 'badairline');

-- Changing a verdict reverses the old contribution instead of double-counting it.
do $$ begin
  if (select score from public.user_preference_signals
      where user_id = '00000000-0000-0000-0000-000000000001'
        and dimension = 'airline' and value = 'CA' and cabin = 'ECONOMY') <> -3 then
    raise exception 'bad-airline reaction was not aggregated';
  end if;
end $$;

update public.feedback set verdict = 'buy'
where user_id = '00000000-0000-0000-0000-000000000001'
  and match_id = '30000000-0000-0000-0000-000000000002';

do $$ begin
  if (select score from public.user_preference_signals
      where user_id = '00000000-0000-0000-0000-000000000001'
        and dimension = 'airline' and value = 'CA' and cabin = 'ECONOMY') <> 1 then
    raise exception 'verdict replacement was double-counted';
  end if;
end $$;

-- Retention removes all old details, including source feedback, but not learning.
select public.cleanup_retention();

do $$ begin
  if exists (select 1 from public.user_matches where id in (
      '30000000-0000-0000-0000-000000000001',
      '30000000-0000-0000-0000-000000000002'
  )) then
    raise exception 'old detailed matches were not removed';
  end if;
  if exists (select 1 from public.flight_offers where id in (
      '20000000-0000-0000-0000-000000000001',
      '20000000-0000-0000-0000-000000000002'
  )) then
    raise exception 'orphaned old offers were not removed';
  end if;
  if exists (select 1 from public.feedback where match_id = '30000000-0000-0000-0000-000000000002') then
    raise exception 'old source feedback was not removed';
  end if;
  if not exists (select 1 from public.user_preference_signals
                 where user_id = '00000000-0000-0000-0000-000000000001'
                   and dimension = 'airline' and value = 'CA' and score = 1) then
    raise exception 'learning was lost during retention cleanup';
  end if;
end $$;

-- Removing the monitor cannot remove a profile-level preference.
delete from public.monitors where id = '10000000-0000-0000-0000-000000000001';

do $$ begin
  if not exists (select 1 from public.user_preference_signals
                 where user_id = '00000000-0000-0000-0000-000000000001'
                   and dimension = 'airline' and value = 'CA' and score = 1) then
    raise exception 'learned preference was lost with the monitor';
  end if;
end $$;
