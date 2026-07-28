-- Flight Radar by Kyudo
-- Execute once in Supabase SQL Editor.

create extension if not exists pgcrypto;

create type public.profile_status as enum ('pending', 'active', 'suspended', 'deleted');
create type public.profile_role as enum ('user', 'admin');

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  display_name text not null default '',
  telegram_user_id text unique,
  role public.profile_role not null default 'user',
  status public.profile_status not null default 'pending',
  quiet_from time,
  quiet_to time,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz
);

create table public.invites (
  id uuid primary key default gen_random_uuid(),
  token_hash text not null unique,
  email text,
  created_by uuid not null references public.profiles(id),
  expires_at timestamptz not null default (now() + interval '7 days'),
  claimed_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create table public.monitors (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  name text not null,
  status text not null default 'active' check (status in ('active', 'paused', 'expired')),
  filters jsonb not null default '{}'::jsonb,
  app_rules jsonb not null default '{}'::jsonb,
  telegram_rules jsonb not null default '{}'::jsonb,
  expires_at date,
  last_scanned_at timestamptz,
  next_scan_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.flight_offers (
  id uuid primary key default gen_random_uuid(),
  fingerprint text not null unique,
  source text not null,
  route text not null,
  origin text not null,
  destination text not null,
  travel_date date not null,
  cabin text not null,
  airline text not null default '',
  airline_name text not null default '',
  price_pln integer,
  duration_minutes integer,
  stops integer,
  departure text,
  aircraft text,
  seat_note text,
  tags jsonb not null default '[]'::jsonb,
  link text not null default '',
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table public.user_matches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  stars integer not null default 1 check (stars between 1 and 5),
  visible boolean not null default true,
  telegram_eligible boolean not null default false,
  notified_at timestamptz,
  last_notified_price integer,
  min_price_for_user integer,
  feedback text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, monitor_id, offer_id)
);

create table public.telegram_connections (
  user_id uuid primary key references public.profiles(id) on delete cascade,
  chat_id text not null unique,
  username text,
  linked_at timestamptz not null default now(),
  last_update_id bigint not null default 0
);

create table public.telegram_state (
  id integer primary key default 1 check (id = 1),
  update_offset bigint not null default 0
);
insert into public.telegram_state(id, update_offset) values (1, 0) on conflict (id) do nothing;

create table public.scan_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  query_count integer not null default 0,
  offer_count integer not null default 0,
  status text not null default 'running',
  error text
);

create table public.feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  match_id uuid not null references public.user_matches(id) on delete cascade,
  verdict text not null,
  created_at timestamptz not null default now()
);

create index monitors_queue_idx on public.monitors(status, next_scan_at);
create index offers_route_date_idx on public.flight_offers(origin, destination, travel_date, cabin);
create index matches_user_idx on public.user_matches(user_id, updated_at desc);
create index scan_runs_started_idx on public.scan_runs(started_at desc);

create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.profiles
    where id = auth.uid() and role = 'admin' and status = 'active'
  );
$$;

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  telegram_id text;
begin
  telegram_id := coalesce(new.raw_user_meta_data->>'id', new.raw_user_meta_data->>'sub');
  insert into public.profiles(id, email, display_name)
  values (new.id, coalesce(new.email, ''), coalesce(new.raw_user_meta_data->>'name', new.raw_user_meta_data->>'full_name', ''))
  on conflict (id) do nothing;
  update public.profiles
  set telegram_user_id = coalesce(telegram_user_id, telegram_id),
      display_name = case when display_name = '' then coalesce(new.raw_user_meta_data->>'name', new.raw_user_meta_data->>'full_name', '') else display_name end
  where id = new.id;
  return new;
end;
$$;

create or replace function public.prevent_self_privilege_change()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if old.id = auth.uid() and (new.role is distinct from old.role or new.status is distinct from old.status) then
    raise exception 'Nie można samodzielnie zmienić roli ani statusu konta';
  end if;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

drop trigger if exists protect_profile_privileges on public.profiles;
create trigger protect_profile_privileges
  before update on public.profiles
  for each row execute procedure public.prevent_self_privilege_change();

create or replace function public.claim_invite(invite_token text)
returns boolean language plpgsql security definer set search_path = public as $$
declare
  invite_row public.invites;
  active_count integer;
begin
  -- Jednoznaczny limit dziesięciu aktywnych miejsc także przy dwóch
  -- jednoczesnych kliknięciach w linki zaproszeń.
  perform pg_advisory_xact_lock(47010);
  select * into invite_row from public.invites
  where token_hash = encode(digest(invite_token, 'sha256'), 'hex')
    and claimed_at is null and revoked_at is null and expires_at > now()
    and (email is null or lower(email) = lower((select email from auth.users where id = auth.uid())))
  for update;
  if not found then return false; end if;
  select count(*) into active_count from public.profiles where status = 'active';
  if active_count >= 10 then return false; end if;
  update public.profiles set status = 'active' where id = auth.uid();
  update public.invites set claimed_at = now() where id = invite_row.id;
  return true;
end;
$$;

create or replace function public.sync_telegram_connection(telegram_chat_id text, telegram_username text default '')
returns boolean language plpgsql security definer set search_path = public as $$
declare
  expected_id text;
begin
  if auth.uid() is null then return false; end if;
  select telegram_user_id into expected_id from public.profiles where id = auth.uid();
  expected_id := coalesce(expected_id, auth.jwt() -> 'user_metadata' ->> 'id', auth.jwt() -> 'user_metadata' ->> 'sub');
  if expected_id is null or expected_id <> telegram_chat_id then return false; end if;
  insert into public.telegram_connections(user_id, chat_id, username)
  values (auth.uid(), telegram_chat_id, nullif(telegram_username, ''))
  on conflict (user_id) do update set chat_id = excluded.chat_id, username = excluded.username;
  update public.profiles set telegram_user_id = telegram_chat_id where id = auth.uid();
  return true;
exception when unique_violation then
  return false;
end;
$$;
grant execute on function public.sync_telegram_connection(text, text) to authenticated;

alter table public.profiles enable row level security;
alter table public.invites enable row level security;
alter table public.monitors enable row level security;
alter table public.flight_offers enable row level security;
alter table public.user_matches enable row level security;
alter table public.telegram_connections enable row level security;
alter table public.telegram_state enable row level security;
alter table public.scan_runs enable row level security;
alter table public.feedback enable row level security;

create policy profiles_self_or_admin on public.profiles for select using (id = auth.uid() or public.is_admin());
create policy profiles_self_update on public.profiles for update using (id = auth.uid() or public.is_admin());
create policy profiles_admin_delete on public.profiles for delete using (public.is_admin() and id <> auth.uid());

create policy invites_admin_all on public.invites for all using (public.is_admin()) with check (public.is_admin());
create policy monitors_owner_all on public.monitors for all using (user_id = auth.uid() or public.is_admin()) with check (user_id = auth.uid() or public.is_admin());
create policy offers_matched_owner_read on public.flight_offers for select using (
  exists (select 1 from public.user_matches m where m.offer_id = id and (m.user_id = auth.uid() or public.is_admin()))
);
create policy matches_owner_read on public.user_matches for select using (user_id = auth.uid() or public.is_admin());
create policy matches_owner_update on public.user_matches for update using (user_id = auth.uid() or public.is_admin()) with check (user_id = auth.uid() or public.is_admin());
create policy connections_self_read on public.telegram_connections for select using (user_id = auth.uid() or public.is_admin());
create policy connections_self_delete on public.telegram_connections for delete using (user_id = auth.uid() or public.is_admin());
create policy telegram_state_admin_read on public.telegram_state for select using (public.is_admin());
create policy scan_runs_admin_read on public.scan_runs for select using (public.is_admin());
create policy feedback_owner_all on public.feedback for all using (user_id = auth.uid()) with check (user_id = auth.uid());
