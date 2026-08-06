-- Flight Radar by Kyudo
-- Execute once in Supabase SQL Editor.

create extension if not exists pgcrypto;

-- Keep a small migration ledger for the automated deployment workflow.
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
  version text primary key,
  name text not null,
  statements text[] not null default '{}'::text[]
);

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
  filters_changed_at timestamptz not null default now(),
  queue_generation bigint not null default 0,
  last_scanned_at timestamptz,
  next_scan_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.monitor_scan_items (
  id uuid primary key default gen_random_uuid(),
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  origin text not null,
  destination text not null,
  travel_date date not null,
  return_date date,
  trip_type text not null default 'one_way' check (trip_type in ('one_way', 'round_trip')),
  cabin text not null,
  queue_generation bigint not null default 0,
  last_scanned_at timestamptz,
  next_scan_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table public.flight_offers (
  id uuid primary key default gen_random_uuid(),
  fingerprint text not null unique,
  source text not null,
  route text not null,
  origin text not null,
  destination text not null,
  travel_date date not null,
  return_date date,
  trip_type text not null default 'one_way' check (trip_type in ('one_way', 'round_trip')),
  cabin text not null,
  airline text not null default '',
  airline_name text not null default '',
  price_pln integer,
  duration_minutes integer,
  stops integer,
  departure text,
  aircraft text,
  tags jsonb not null default '[]'::jsonb,
  verification_status text not null default 'verified' check (verification_status in ('verified', 'pending_return', 'pending_verification', 'stale')),
  verification_note text not null default '',
  link text not null default '',
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table public.offer_price_history (
  id bigint generated always as identity primary key,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  price_pln integer not null check (price_pln > 0),
  observed_at timestamptz not null default now()
);

create table public.offer_mutes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  kind text not null check (kind in ('offer', 'airline', 'route')),
  value text not null,
  label text not null default '',
  created_at timestamptz not null default now(),
  unique (user_id, kind, value)
);

create table public.user_matches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  stars integer not null default 1 check (stars between 1 and 5),
  visible boolean not null default true,
  telegram_eligible boolean not null default false,
  new_airline boolean not null default false,
  notified_at timestamptz,
  notified_generation bigint not null default 0,
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

-- Durable delivery queue.  A Google scan only creates a pending message;
-- Telegram delivery is retried independently and is acknowledged only after
-- Telegram returns ok=true.
create table public.telegram_outbox (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  match_id uuid not null references public.user_matches(id) on delete cascade,
  chat_id text not null,
  dedupe_key text not null unique,
  price_pln integer not null check (price_pln > 0),
  generation bigint not null default 0,
  message_text text not null,
  reply_markup jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending', 'sending', 'retry', 'sent', 'dead')),
  attempts integer not null default 0 check (attempts >= 0),
  available_at timestamptz not null default now(),
  sent_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.telegram_state (
  id integer primary key default 1 check (id = 1),
  update_offset bigint not null default 0
);
insert into public.telegram_state(id, update_offset) values (1, 0) on conflict (id) do nothing;

create table public.telegram_auth_attempts (
  id bigint generated always as identity primary key,
  telegram_user_id text not null,
  attempted_at timestamptz not null default now()
);

create table public.scan_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  query_count integer not null default 0,
  standard_limit integer,
  first_limit integer,
  blocked boolean not null default false,
  due_count integer not null default 0,
  due_item_count integer not null default 0,
  total_queue_count integer not null default 0,
  selected_count integer not null default 0,
  failed_count integer not null default 0,
  deferred_count integer not null default 0,
  coverage_percent numeric(5,2) not null default 0.00 check (coverage_percent >= 0 and coverage_percent <= 100),
  offer_count integer not null default 0,
  status text not null default 'running',
  error text
);

create table public.feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  match_id uuid not null references public.user_matches(id) on delete cascade,
  verdict text not null check (verdict in ('buy', 'expensive', 'skip', 'toolong', 'badairline')),
  created_at timestamptz not null default now(),
  unique (user_id, match_id)
);

create table public.user_preference_signals (
  user_id uuid not null references public.profiles(id) on delete cascade,
  dimension text not null check (dimension in ('airline', 'route', 'destination', 'duration', 'price')),
  value text not null,
  cabin text not null default '*',
  score integer not null default 0 check (score between -20 and 20),
  positive_count integer not null default 0 check (positive_count >= 0),
  negative_count integer not null default 0 check (negative_count >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, dimension, value, cabin)
);

create index monitors_queue_idx on public.monitors(status, next_scan_at);
create index monitor_scan_queue_idx on public.monitor_scan_items(next_scan_at, last_scanned_at);
create index monitor_scan_monitor_idx on public.monitor_scan_items(monitor_id);
create unique index monitor_scan_items_one_way_key on public.monitor_scan_items(monitor_id, origin, destination, travel_date, cabin) where return_date is null and trip_type = 'one_way';
create unique index monitor_scan_items_round_trip_key on public.monitor_scan_items(monitor_id, origin, destination, travel_date, return_date, cabin) where return_date is not null and trip_type = 'round_trip';

create or replace function public.sync_monitor_scan_items(
  p_monitor_id uuid,
  p_items jsonb
)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  desired_count integer;
  queue_count integer;
  current_generation bigint;
begin
  if jsonb_typeof(coalesce(p_items, 'null'::jsonb)) <> 'array' then
    raise exception 'Pozycje kolejki muszą być tablicą JSON';
  end if;
  desired_count := jsonb_array_length(p_items);
  if desired_count > 5000 then
    raise exception 'Kolejka monitora przekracza limit 5000 kombinacji';
  end if;
  select queue_generation into current_generation
  from public.monitors
  where id = p_monitor_id;
  if not found then
    raise exception 'Monitor nie istnieje';
  end if;
  if exists (
    select 1
    from jsonb_to_recordset(p_items) as candidate(
      origin text, destination text, travel_date date, return_date date,
      trip_type text, cabin text
    )
    where candidate.origin is null or candidate.origin !~ '^[A-Za-z]{3}$'
      or candidate.destination is null or candidate.destination !~ '^[A-Za-z]{3}$'
      or candidate.travel_date is null
      or candidate.trip_type is null or candidate.trip_type not in ('one_way', 'round_trip')
      or candidate.cabin is null or lower(replace(candidate.cabin, '_', '-')) not in ('economy', 'premium-economy', 'business', 'first')
      or (candidate.trip_type = 'one_way' and candidate.return_date is not null)
      or (candidate.trip_type = 'round_trip' and (candidate.return_date is null or candidate.return_date <= candidate.travel_date))
  ) then
    raise exception 'Kolejka zawiera nieprawidłową kombinację lotu';
  end if;
  perform pg_advisory_xact_lock(47012, hashtext(p_monitor_id::text));
  delete from public.monitor_scan_items current_item
  where current_item.monitor_id = p_monitor_id
    and not exists (
      select 1 from jsonb_to_recordset(p_items) as desired(
        origin text, destination text, travel_date date, return_date date,
        trip_type text, cabin text
      )
      where upper(trim(desired.origin)) = current_item.origin
        and upper(trim(desired.destination)) = current_item.destination
        and desired.travel_date = current_item.travel_date
        and desired.return_date is not distinct from current_item.return_date
        and desired.trip_type = current_item.trip_type
        and lower(replace(desired.cabin, '_', '-')) = current_item.cabin
    );
  insert into public.monitor_scan_items(
    monitor_id, origin, destination, travel_date, return_date, trip_type, cabin,
    queue_generation, last_scanned_at, next_scan_at
  )
  select p_monitor_id, upper(trim(desired.origin)), upper(trim(desired.destination)),
    desired.travel_date, desired.return_date, desired.trip_type,
    lower(replace(desired.cabin, '_', '-')), current_generation, null, now()
  from jsonb_to_recordset(p_items) as desired(
    origin text, destination text, travel_date date, return_date date,
    trip_type text, cabin text
  )
  on conflict do nothing;
  update public.monitor_scan_items
  set queue_generation = current_generation,
      last_scanned_at = null,
      next_scan_at = now()
  where monitor_id = p_monitor_id
    and queue_generation is distinct from current_generation;
  select count(*) into queue_count from public.monitor_scan_items where monitor_id = p_monitor_id;
  if queue_count <> desired_count then
    raise exception 'Niepełna kolejka monitora: oczekiwano %, zapisano %', desired_count, queue_count;
  end if;
  return jsonb_build_object('desired_count', desired_count, 'queue_count', queue_count, 'queue_generation', current_generation);
end;
$$;

revoke all on function public.sync_monitor_scan_items(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.sync_monitor_scan_items(uuid, jsonb) to service_role;

create or replace function public.refresh_monitor_scan_status(p_monitor_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  total_count integer;
  due_count integer;
  latest_scan timestamptz;
  earliest_next timestamptz;
begin
  select count(*)::integer,
         count(*) filter (where next_scan_at <= now())::integer,
         max(last_scanned_at),
         min(next_scan_at)
    into total_count, due_count, latest_scan, earliest_next
  from public.monitor_scan_items
  where monitor_id = p_monitor_id;

  update public.monitors
  set last_scanned_at = latest_scan,
      next_scan_at = case when due_count > 0 then now() else earliest_next end
  where id = p_monitor_id;

  return jsonb_build_object('total_count', total_count, 'due_count', due_count, 'last_scanned_at', latest_scan, 'next_scan_at', case when due_count > 0 then now() else earliest_next end);
end;
$$;

revoke all on function public.refresh_monitor_scan_status(uuid) from public, anon, authenticated;
grant execute on function public.refresh_monitor_scan_status(uuid) to service_role;

create or replace function public.get_monitor_scan_progress(p_monitor_id uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from public.monitors where id = p_monitor_id and user_id = auth.uid() and public.is_active_user(user_id)) then
    return null;
  end if;
  return (
    select jsonb_build_object(
      'total_count', count(*)::integer,
      'due_count', count(*) filter (where next_scan_at <= now())::integer,
      'scanned_count', count(*) filter (where last_scanned_at is not null)::integer
    )
    from public.monitor_scan_items
    where monitor_id = p_monitor_id
  );
end;
$$;

revoke all on function public.get_monitor_scan_progress(uuid) from public, anon;
grant execute on function public.get_monitor_scan_progress(uuid) to authenticated;

create or replace function public.scan_queue_summary()
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'due_item_count', count(*) filter (where items.next_scan_at <= now())::integer,
    'total_queue_count', count(*)::integer
  )
  from public.monitor_scan_items items
  join public.monitors monitors on monitors.id = items.monitor_id
  where monitors.status = 'active';
$$;

revoke all on function public.scan_queue_summary() from public, anon, authenticated;
grant execute on function public.scan_queue_summary() to service_role;
create index offers_route_date_idx on public.flight_offers(origin, destination, travel_date, cabin);
create index offers_round_trip_date_idx on public.flight_offers(origin, destination, travel_date, return_date, cabin);
create index flight_offers_travel_date_idx on public.flight_offers(travel_date);
create index offer_price_history_offer_idx on public.offer_price_history(offer_id, observed_at desc);
create index offer_mutes_user_idx on public.offer_mutes(user_id, kind, value);
create index matches_user_idx on public.user_matches(user_id, updated_at desc);
create index scan_runs_started_idx on public.scan_runs(started_at desc);
create index telegram_auth_attempts_lookup_idx on public.telegram_auth_attempts(telegram_user_id, attempted_at desc);
create index user_preference_signals_user_idx on public.user_preference_signals(user_id, updated_at desc);
create index telegram_outbox_delivery_idx on public.telegram_outbox(status, available_at, created_at);
create index telegram_outbox_user_idx on public.telegram_outbox(user_id, created_at desc);

create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.profiles
    where id = auth.uid() and role = 'admin' and status = 'active'
  );
$$;

create or replace function public.is_active_user(user_id uuid default auth.uid())
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.profiles
    where id = user_id
      and status = 'active'
      and user_id = auth.uid()
  );
$$;

create or replace function public.match_within_monitor_budget(p_match_id uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1
    from public.user_matches m
    join public.monitors monitor on monitor.id = m.monitor_id
    join public.flight_offers offer on offer.id = m.offer_id
    where m.id = p_match_id
      and m.visible
      and public.is_active_user(m.user_id)
      and m.user_id = auth.uid()
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((monitor.filters ->> 'budget_pln')::numeric, 0)
  );
$$;

create or replace function public.offer_price_history_for_user(p_offer_ids uuid[])
returns table(offer_id uuid, price_pln integer, observed_at timestamptz)
language sql stable security definer set search_path = public as $$
  select ranked.offer_id, ranked.price_pln, ranked.observed_at
  from (
    select history.offer_id, history.price_pln, history.observed_at,
           row_number() over (
             partition by history.offer_id
             order by history.observed_at desc, history.id desc
           ) as row_number
    from public.offer_price_history history
    where history.offer_id = any(coalesce(p_offer_ids, '{}'::uuid[]))
      and exists (
        select 1 from public.user_matches match
        where match.offer_id = history.offer_id
          and match.user_id = auth.uid()
          and match.visible
          and public.is_active_user(match.user_id)
          and public.match_within_monitor_budget(match.id)
      )
  ) ranked
  where ranked.row_number <= 30
  order by ranked.observed_at desc;
$$;

create or replace function public.can_read_flight_offer(p_offer_id uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1
    from public.user_matches m
    where m.offer_id = p_offer_id
      and m.visible
      and public.is_active_user(m.user_id)
      and m.user_id = auth.uid()
      and public.match_within_monitor_budget(m.id)
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
  if old.id = auth.uid() and (
    new.role is distinct from old.role
    or new.status is distinct from old.status
    or (
      new.telegram_user_id is distinct from old.telegram_user_id
      and new.telegram_user_id is distinct from coalesce(auth.jwt() -> 'user_metadata' ->> 'id', auth.jwt() -> 'user_metadata' ->> 'sub')
    )
  ) then
    raise exception 'Nie można samodzielnie zmienić roli ani statusu konta';
  end if;
  return new;
end;
$$;

create or replace function public.enforce_monitor_limit()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  monitor_count integer;
begin
  if new.status <> 'expired' then
    -- Blokada na poziomie użytkownika zapobiega przekroczeniu limitu przy
    -- dwóch równoczesnych żądaniach INSERT/UPDATE.
    perform pg_advisory_xact_lock(47011, hashtext(new.user_id::text));
    select count(*) into monitor_count
    from public.monitors
    where user_id = new.user_id
      and status <> 'expired'
      and id <> coalesce(new.id, '00000000-0000-0000-0000-000000000000'::uuid);
    if monitor_count >= 2 then
      raise exception 'Limit dwóch monitorów na użytkownika został osiągnięty';
    end if;
  end if;
  return new;
end;
$$;

create or replace function public.touch_updated_at()
returns trigger language plpgsql set search_path = public as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create or replace function public.mark_monitor_filters_changed()
returns trigger language plpgsql set search_path = public as $$
begin
  if new.filters is distinct from old.filters then
    new.filters_changed_at = now();
    new.queue_generation = coalesce(old.queue_generation, 0) + 1;
  end if;
  return new;
end;
$$;

create or replace function public.offer_matches_monitor_filters(
  p_offer_id uuid,
  p_filters jsonb
)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.flight_offers offer
    cross join lateral (select coalesce(offer.raw ->> 'outbound_duration_h',
                                         case when offer.duration_minutes is not null
                                              then (offer.duration_minutes::numeric / 60)::text end) as outbound_duration,
                               coalesce(offer.raw ->> 'return_duration_h', '') as return_duration,
                               coalesce(offer.raw ->> 'outbound_stops', offer.stops::text) as outbound_stops,
                               coalesce(offer.raw ->> 'return_stops', '') as return_stops) legs
    where offer.id = p_offer_id
      and offer.origin in (
        select value from jsonb_array_elements_text(coalesce(p_filters -> 'origins', '[]'::jsonb)) as origins(value)
      )
      and offer.destination in (
        select value from jsonb_array_elements_text(coalesce(p_filters -> 'destinations', '[]'::jsonb)) as destinations(value)
      )
      and offer.travel_date >= nullif(p_filters ->> 'from', '')::date
      and offer.travel_date <= nullif(p_filters ->> 'to', '')::date
      and offer.trip_type = coalesce(nullif(p_filters ->> 'trip_type', ''), 'one_way')
      and (
        offer.trip_type = 'one_way'
        or (
          offer.return_date is not null
          and offer.return_date >= nullif(p_filters ->> 'return_from', '')::date
          and offer.return_date <= nullif(p_filters ->> 'return_to', '')::date
        )
      )
      and upper(offer.cabin) in (
        select upper(value)
        from jsonb_array_elements_text(
          case when jsonb_typeof(p_filters -> 'cabins') = 'array'
               then p_filters -> 'cabins'
               else jsonb_build_array(p_filters ->> 'cabin') end
        ) as cabins(value)
      )
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((p_filters ->> 'budget_pln')::numeric, 0)
      and (nullif(trim(p_filters ->> 'max_duration_h'), '') is null
        or (legs.outbound_duration <> '' and legs.outbound_duration::numeric <= nullif(trim(p_filters ->> 'max_duration_h'), '')::numeric)
        and (offer.trip_type = 'one_way' or (legs.return_duration <> '' and legs.return_duration::numeric <= nullif(trim(p_filters ->> 'max_duration_h'), '')::numeric)))
      and (nullif(trim(p_filters ->> 'max_stops'), '') is null
        or (legs.outbound_stops <> '' and legs.outbound_stops::integer <= coalesce((p_filters ->> 'max_stops')::integer, 2))
        and (offer.trip_type = 'one_way' or (legs.return_stops <> '' and legs.return_stops::integer <= coalesce((p_filters ->> 'max_stops')::integer, 2))))
      and (coalesce((p_filters ->> 'direct_only')::boolean, false) is false
        or (legs.outbound_stops = '0' and (offer.trip_type = 'one_way' or legs.return_stops = '0')))
      and not exists (
        select 1
        from jsonb_array_elements_text(coalesce(p_filters -> 'excluded_airlines', '[]'::jsonb)) as excluded(value)
        where nullif(trim(excluded.value), '') is not null
          and (
            lower(coalesce(offer.airline_name, '')) like '%' || lower(trim(excluded.value)) || '%'
            or lower(coalesce(offer.airline, '')) = lower(trim(excluded.value))
          )
      )
  );
$$;

create or replace function public.hide_monitor_matches_on_filter_change()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.filters is distinct from old.filters then
    update public.user_matches matches
    set visible = decision.still_matches,
        telegram_eligible = case
          when decision.still_matches
            then matches.telegram_eligible
          else false
        end,
        updated_at = now()
    from (
      select existing.id,
             public.offer_matches_monitor_filters(existing.offer_id, new.filters) as still_matches
      from public.user_matches existing
      where existing.monitor_id = new.id
    ) decision
    where matches.id = decision.id
      and matches.visible is distinct from decision.still_matches;
  end if;
  return new;
end;
$$;

drop trigger if exists touch_monitors_updated_at on public.monitors;
create trigger touch_monitors_updated_at
  before update on public.monitors
  for each row execute procedure public.touch_updated_at();

drop trigger if exists mark_monitor_filters_changed on public.monitors;
create trigger mark_monitor_filters_changed
  before update of filters on public.monitors
  for each row execute procedure public.mark_monitor_filters_changed();

drop trigger if exists touch_matches_updated_at on public.user_matches;
create trigger touch_matches_updated_at
  before update on public.user_matches
  for each row execute procedure public.touch_updated_at();

drop trigger if exists hide_monitor_matches_on_filter_change on public.monitors;
create trigger hide_monitor_matches_on_filter_change
  after update of filters on public.monitors
  for each row execute procedure public.hide_monitor_matches_on_filter_change();

create or replace function public.valid_round_trip_pair_count(
  p_from date,
  p_to date,
  p_return_from date,
  p_return_to date
)
returns bigint
language plpgsql
immutable
set search_path = public
as $$
declare
  departure date;
  first_valid_return date;
  pair_count bigint := 0;
begin
  if p_from is null or p_to is null or p_return_from is null or p_return_to is null
     or p_to < p_from or p_return_to < p_return_from then
    return 0;
  end if;
  departure := p_from;
  while departure <= p_to loop
    first_valid_return := greatest(p_return_from, departure + 1);
    if first_valid_return <= p_return_to then
      pair_count := pair_count + (p_return_to - first_valid_return + 1);
    end if;
    departure := departure + 1;
  end loop;
  return pair_count;
end;
$$;

create or replace function public.validate_monitor_filters()
returns trigger language plpgsql set search_path = public as $$
declare
  from_date date;
  to_date date;
  return_from_date date;
  return_to_date date;
  trip text;
  origin_count integer;
  destination_count integer;
  budget numeric;
  duration numeric;
  duration_raw text;
  stops integer;
  min_stars integer;
  drop_percent numeric;
  cabin_count integer;
  combination_count bigint;
begin
  if length(trim(coalesce(new.name, ''))) < 1 or length(new.name) > 120 then
    raise exception 'Nazwa monitora musi mieć od 1 do 120 znaków';
  end if;
  if jsonb_typeof(coalesce(new.filters -> 'origins', 'null'::jsonb)) <> 'array'
     or jsonb_typeof(coalesce(new.filters -> 'destinations', 'null'::jsonb)) <> 'array' then
    raise exception 'Lotniska muszą być zapisane jako listy';
  end if;
  from_date := nullif(new.filters ->> 'from', '')::date;
  to_date := (new.filters ->> 'to')::date;
  return_from_date := nullif(new.filters ->> 'return_from', '')::date;
  return_to_date := nullif(new.filters ->> 'return_to', '')::date;
  trip := coalesce(nullif(new.filters ->> 'trip_type', ''), 'one_way');
  origin_count := jsonb_array_length(coalesce(new.filters -> 'origins', '[]'::jsonb));
  destination_count := jsonb_array_length(coalesce(new.filters -> 'destinations', '[]'::jsonb));
  budget := coalesce((new.filters ->> 'budget_pln')::numeric, 0);
  duration_raw := nullif(trim(new.filters ->> 'max_duration_h'), '');
  duration := case when duration_raw is null then null else duration_raw::numeric end;
  stops := coalesce((new.filters ->> 'max_stops')::integer, 2);
  if origin_count < 1 or origin_count > 5 or destination_count < 1 or destination_count > 5 then
    raise exception 'Monitor może zawierać maksymalnie 5 lotnisk wylotu i 5 celów';
  end if;
  if exists (
    select 1 from jsonb_array_elements_text(new.filters -> 'origins') as x(value)
    where x.value !~ '^[A-Z]{3}$'
  ) or exists (
    select 1 from jsonb_array_elements_text(new.filters -> 'destinations') as x(value)
    where x.value !~ '^[A-Z]{3}$'
  ) then
    raise exception 'Kod lotniska musi mieć dokładnie trzy wielkie litery';
  end if;
  if from_date is null or to_date is null or to_date < from_date or to_date - from_date > 31 then
    raise exception 'Zakres dat monitora może mieć maksymalnie 32 dni';
  end if;
  if trip not in ('one_way', 'round_trip') then
    raise exception 'Nieprawidłowy typ podróży';
  end if;
  if trip = 'round_trip' and (return_from_date is null or return_to_date is null or return_to_date < return_from_date or return_to_date - return_from_date > 31) then
    raise exception 'Zakres dat powrotu może mieć maksymalnie 32 dni';
  end if;
  if trip = 'round_trip' and return_to_date <= from_date then
    raise exception 'Zakres dat powrotu nie tworzy żadnej prawidłowej pary z wylotem';
  end if;
  cabin_count := case when new.filters ? 'cabins' then jsonb_array_length(new.filters -> 'cabins') else 1 end;
  combination_count := origin_count * destination_count * (to_date - from_date + 1) * cabin_count;
  if trip = 'round_trip' then
    combination_count := origin_count * destination_count * cabin_count
      * public.valid_round_trip_pair_count(from_date, to_date, return_from_date, return_to_date);
  end if;
  if combination_count > 5000 then
    raise exception 'Monitor ma zbyt wiele kombinacji do bezpiecznego skanowania (maksymalnie 5000)';
  end if;
  if budget <= 0 or budget > 1000000 or (duration is not null and duration <= 0) or stops < 0 or stops > 9 then
    raise exception 'Nieprawidłowy limit czasu lub przesiadek';
  end if;
  if new.filters ? 'cabins' then
    if jsonb_typeof(new.filters -> 'cabins') <> 'array'
       or jsonb_array_length(new.filters -> 'cabins') < 1
       or jsonb_array_length(new.filters -> 'cabins') > 4
       or exists (
         select 1 from jsonb_array_elements_text(new.filters -> 'cabins') as x(value)
         where x.value not in ('BUSINESS', 'FIRST', 'PREMIUM_ECONOMY', 'ECONOMY')
       ) then
      raise exception 'Monitor musi mieć od 1 do 4 prawidłowych klas lotu';
    end if;
  elsif coalesce(new.filters ->> 'cabin', '') not in ('BUSINESS', 'FIRST', 'PREMIUM_ECONOMY', 'ECONOMY') then
    raise exception 'Nieprawidłowa klasa lotu';
  end if;
  min_stars := coalesce((new.telegram_rules ->> 'min_stars')::integer, 4);
  drop_percent := coalesce((new.telegram_rules ->> 'drop_percent')::numeric, 10);
  if min_stars < 3 or min_stars > 5 or drop_percent < 1 or drop_percent > 50 then
    raise exception 'Nieprawidłowe zasady alertów Telegram';
  end if;
  return new;
end;
$$;

create or replace function public.reserve_scan_slot()
returns uuid language plpgsql security definer set search_path = public as $$
declare
  reserved_id uuid;
begin
  perform pg_advisory_xact_lock(47011);
  if exists (select 1 from public.scan_runs where started_at >= now() - interval '10 minutes') then
    return null;
  end if;
  insert into public.scan_runs(status, blocked, query_count)
  values ('queued', false, 0)
  returning id into reserved_id;
  return reserved_id;
end;
$$;

create or replace function public.claim_telegram_outbox(p_limit integer default 50)
returns setof public.telegram_outbox
language plpgsql security definer set search_path = public as $$
begin
  return query
  with candidates as (
    select id
    from public.telegram_outbox
    where (
        status in ('pending', 'retry') and available_at <= now()
      ) or (
        status = 'sending' and updated_at <= now() - interval '15 minutes'
      )
    order by available_at asc, created_at asc, id asc
    limit greatest(1, least(coalesce(p_limit, 50), 200))
    for update skip locked
  )
  update public.telegram_outbox outbox
  set status = 'sending',
      attempts = outbox.attempts + 1,
      updated_at = now()
  from candidates
  where outbox.id = candidates.id
  returning outbox.*;
end;
$$;

create or replace function public.admin_delivery_summary()
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'pending', count(*) filter (where status in ('pending', 'retry', 'sending')),
    'sent_24h', count(*) filter (where status = 'sent' and sent_at >= now() - interval '24 hours'),
    'failed', count(*) filter (where status = 'dead'),
    'last_sent_at', max(sent_at)
  )
  from public.telegram_outbox
  where public.is_admin();
$$;

create or replace function public.cleanup_retention()
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  history_deleted bigint := 0;
  matches_deleted bigint := 0;
  monitors_deleted bigint := 0;
  offers_deleted bigint := 0;
  runs_deleted bigint := 0;
  auth_attempts_deleted bigint := 0;
  invites_deleted bigint := 0;
begin
  delete from public.offer_price_history
  where id in (
    select id from (
      select id, observed_at,
             row_number() over (partition by offer_id order by observed_at desc, id desc) as row_number
      from public.offer_price_history
    ) ranked
    where row_number > 30 or observed_at < now() - interval '30 days'
  );
  get diagnostics history_deleted = row_count;
  delete from public.user_matches match
  using public.flight_offers offer
  where match.offer_id = offer.id
    and offer.travel_date < current_date - 7;
  get diagnostics matches_deleted = row_count;
  delete from public.monitors where status = 'expired' and updated_at < now() - interval '30 days';
  get diagnostics monitors_deleted = row_count;
  delete from public.flight_offers offer
  where (offer.last_seen_at < now() - interval '45 days'
         or offer.travel_date < current_date - 7)
    and not exists (select 1 from public.user_matches match where match.offer_id = offer.id);
  get diagnostics offers_deleted = row_count;
  delete from public.scan_runs where started_at < now() - interval '30 days';
  get diagnostics runs_deleted = row_count;
  delete from public.telegram_auth_attempts where attempted_at < now() - interval '2 days';
  get diagnostics auth_attempts_deleted = row_count;
  delete from public.invites where expires_at < now() - interval '30 days';
  get diagnostics invites_deleted = row_count;
  return jsonb_build_object(
    'history_deleted', history_deleted,
    'matches_deleted', matches_deleted,
    'monitors_deleted', monitors_deleted,
    'offers_deleted', offers_deleted,
    'runs_deleted', runs_deleted,
    'auth_attempts_deleted', auth_attempts_deleted,
    'invites_deleted', invites_deleted
  );
end;
$$;

create or replace function public.preference_verdict_delta(verdict_value text, dimension_value text)
returns integer language sql immutable parallel safe as $$
  select case
    when verdict_value = 'buy' then 1
    when verdict_value = 'badairline' and dimension_value = 'airline' then -3
    when verdict_value = 'toolong' and dimension_value = 'duration' then -3
    when verdict_value = 'expensive' and dimension_value = 'price' then -3
    when verdict_value = 'skip' and dimension_value in ('route', 'destination') then -2
    else 0
  end;
$$;

create or replace function public.preference_signal_score(
  dimension_value text, positive_value integer, negative_value integer
)
returns integer language sql immutable parallel safe as $$
  select greatest(-20, least(20,
    greatest(0, coalesce(positive_value, 0)) -
    greatest(0, coalesce(negative_value, 0)) *
      case when dimension_value in ('airline', 'duration', 'price') then 3 else 2 end
  ))::integer;
$$;

create or replace function public.capture_feedback_preference()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  context_row record;
  signal_row record;
  old_delta integer;
  new_delta integer;
  score_delta integer;
  positive_delta integer;
  negative_delta integer;
begin
  select match.user_id,
         offer.route,
         upper(offer.destination) as destination,
         upper(replace(offer.cabin, '-', '_')) as cabin,
         case
           when nullif(trim(offer.airline), '') is not null then upper(trim(offer.airline))
           else lower(regexp_replace(trim(offer.airline_name), '\s+', ' ', 'g'))
         end as airline,
         case when greatest(
                    coalesce(offer.duration_minutes, 0),
                    coalesce((offer.raw ->> 'return_duration_h')::numeric * 60, 0)
                  ) > 0
              then (ceil(greatest(
                     coalesce(offer.duration_minutes, 0),
                     coalesce((offer.raw ->> 'return_duration_h')::numeric * 60, 0)
                   ) / 120) * 2)::integer end as duration_bucket,
         case when offer.price_pln > 0 and nullif((monitor.filters ->> 'budget_pln')::numeric, 0) is not null
              then greatest(10, least(200,
                   (ceil((offer.price_pln::numeric / (monitor.filters ->> 'budget_pln')::numeric) * 10) * 10)::integer))
              end as price_bucket
  into context_row
  from public.user_matches match
  join public.flight_offers offer on offer.id = match.offer_id
  join public.monitors monitor on monitor.id = match.monitor_id
  where match.id = new.match_id;

  if context_row.user_id is null or context_row.user_id <> new.user_id then
    raise exception 'Feedback does not belong to this user';
  end if;

  for signal_row in
    select dimension, value
    from (values
      ('airline'::text, context_row.airline::text),
      ('route'::text, context_row.route::text),
      ('destination'::text, context_row.destination::text),
      ('duration'::text, context_row.duration_bucket::text),
      ('price'::text, context_row.price_bucket::text)
    ) as signals(dimension, value)
    where nullif(value, '') is not null
  loop
    new_delta := public.preference_verdict_delta(new.verdict, signal_row.dimension);
    old_delta := case when tg_op = 'UPDATE'
                      then public.preference_verdict_delta(old.verdict, signal_row.dimension)
                      else 0 end;
    score_delta := new_delta - old_delta;
    positive_delta := (case when new_delta > 0 then 1 else 0 end)
                    - (case when old_delta > 0 then 1 else 0 end);
    negative_delta := (case when new_delta < 0 then 1 else 0 end)
                    - (case when old_delta < 0 then 1 else 0 end);

    if score_delta <> 0 or positive_delta <> 0 or negative_delta <> 0 then
      insert into public.user_preference_signals(
        user_id, dimension, value, cabin, score, positive_count, negative_count, updated_at
      ) values (
        new.user_id, signal_row.dimension, signal_row.value, context_row.cabin,
        public.preference_signal_score(
          signal_row.dimension, greatest(0, positive_delta), greatest(0, negative_delta)
        ),
        greatest(0, positive_delta), greatest(0, negative_delta), now()
      )
      on conflict (user_id, dimension, value, cabin) do update set
        score = public.preference_signal_score(
          signal_row.dimension,
          greatest(0, public.user_preference_signals.positive_count + positive_delta),
          greatest(0, public.user_preference_signals.negative_count + negative_delta)
        ),
        positive_count = greatest(0, public.user_preference_signals.positive_count + positive_delta),
        negative_count = greatest(0, public.user_preference_signals.negative_count + negative_delta),
        updated_at = now();
    end if;
  end loop;
  return new;
end;
$$;

revoke all on function public.reserve_scan_slot() from public, anon, authenticated;
grant execute on function public.reserve_scan_slot() to service_role;
revoke all on function public.cleanup_retention() from public, anon, authenticated;
grant execute on function public.cleanup_retention() to service_role;
revoke all on function public.claim_telegram_outbox(integer) from public, anon, authenticated;
grant execute on function public.claim_telegram_outbox(integer) to service_role;
revoke all on function public.admin_delivery_summary() from public, anon;
grant execute on function public.admin_delivery_summary() to authenticated;

create or replace function public.complete_telegram_outbox(p_outbox_id uuid)
returns boolean
language plpgsql security definer set search_path = public as $$
declare
  completed boolean := false;
begin
  update public.user_matches matches
  set notified_at = now(),
      last_notified_price = outbox.price_pln,
      notified_generation = outbox.generation,
      updated_at = now()
  from public.telegram_outbox outbox
  where outbox.id = p_outbox_id
    and outbox.status = 'sending'
    and matches.id = outbox.match_id;

  if not found then
    return false;
  end if;

  update public.telegram_outbox
  set status = 'sent',
      sent_at = now(),
      last_error = null,
      updated_at = now()
  where id = p_outbox_id
    and status = 'sending';
  completed := found;
  return completed;
end;
$$;
revoke all on function public.complete_telegram_outbox(uuid) from public, anon, authenticated;
grant execute on function public.complete_telegram_outbox(uuid) to service_role;
revoke all on function public.preference_verdict_delta(text, text) from public, anon, authenticated;
revoke all on function public.preference_signal_score(text, integer, integer) from public, anon, authenticated;
revoke all on function public.capture_feedback_preference() from public, anon, authenticated;
revoke all on function public.hide_monitor_matches_on_filter_change() from public, anon, authenticated;
revoke all on function public.offer_matches_monitor_filters(uuid, jsonb) from public, anon, authenticated;

-- Telegram alert level is kept only for backwards compatibility with old rows.
-- Every offer that passes the monitor filters is eligible for Telegram.
create or replace function public.normalize_telegram_rules()
returns trigger language plpgsql set search_path = public as $$
begin
  new.telegram_rules := jsonb_set(
    coalesce(new.telegram_rules, '{}'::jsonb),
    '{min_stars}',
    '3'::jsonb,
    true
  );
  new.telegram_rules := jsonb_set(
    new.telegram_rules,
    '{immediate_new_low}',
    'true'::jsonb,
    true
  );
  return new;
end;
$$;

revoke all on function public.normalize_telegram_rules() from public, anon, authenticated;

drop trigger if exists normalize_telegram_rules on public.monitors;
create trigger normalize_telegram_rules
  before insert or update of telegram_rules on public.monitors
  for each row execute procedure public.normalize_telegram_rules();

drop trigger if exists validate_monitor_filters on public.monitors;
create trigger validate_monitor_filters
  before insert or update of name, filters, telegram_rules on public.monitors
  for each row execute procedure public.validate_monitor_filters();

create or replace function public.validate_monitor_date_windows()
returns trigger language plpgsql set search_path = public as $$
declare
  from_date date;
  to_date date;
  return_from_date date;
  return_to_date date;
  trip text;
begin
  from_date := nullif(new.filters ->> 'from', '')::date;
  to_date := nullif(new.filters ->> 'to', '')::date;
  return_from_date := nullif(new.filters ->> 'return_from', '')::date;
  return_to_date := nullif(new.filters ->> 'return_to', '')::date;
  trip := coalesce(nullif(new.filters ->> 'trip_type', ''), 'one_way');
  if from_date is null or to_date is null or to_date < from_date or to_date - from_date > 13 then
    raise exception 'Zakres dat wylotu może mieć maksymalnie 14 dni';
  end if;
  if trip = 'round_trip' and (return_from_date is null or return_to_date is null or return_to_date < return_from_date or return_to_date - return_from_date > 13) then
    raise exception 'Zakres dat powrotu może mieć maksymalnie 14 dni';
  end if;
  return new;
end;
$$;

revoke execute on function public.validate_monitor_date_windows() from public, anon, authenticated;
drop trigger if exists validate_monitor_date_windows on public.monitors;
create trigger validate_monitor_date_windows
  before insert or update of filters on public.monitors
  for each row execute procedure public.validate_monitor_date_windows();

drop trigger if exists capture_feedback_preference on public.feedback;
create trigger capture_feedback_preference
  after insert or update of verdict on public.feedback
  for each row execute procedure public.capture_feedback_preference();

drop trigger if exists enforce_monitor_limit on public.monitors;
create trigger enforce_monitor_limit
  before insert or update of user_id, status on public.monitors
  for each row execute procedure public.enforce_monitor_limit();

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
  if auth.uid() is null then return false; end if;
  -- Jednoznaczny limit dziesięciu aktywnych miejsc także przy dwóch
  -- jednoczesnych kliknięciach w linki zaproszeń.
  perform pg_advisory_xact_lock(47010);
  select * into invite_row from public.invites
  where token_hash = encode(digest(invite_token, 'sha256'), 'hex')
    and claimed_at is null and revoked_at is null and expires_at > now()
    and (email is null or lower(email) = lower((select email from auth.users where id = auth.uid())))
  for update;
  if not found then return false; end if;
  if not exists (select 1 from public.profiles where id = auth.uid() and status = 'pending') then return false; end if;
  select count(*) into active_count from public.profiles where status = 'active';
  if active_count >= 10 then return false; end if;
  update public.profiles set status = 'active' where id = auth.uid();
  update public.invites set claimed_at = now() where id = invite_row.id;
  return true;
end;
$$;

create or replace function public.create_invite(p_token_hash text, p_email text default null)
returns boolean language plpgsql security definer set search_path = public as $$
declare
  active_count integer;
begin
  if not public.is_admin() then raise exception 'Brak uprawnień administratora'; end if;
  perform pg_advisory_xact_lock(47010);
  select count(*) into active_count from public.profiles where status = 'active';
  if active_count >= 10 then raise exception 'Limit dziesięciu aktywnych miejsc został osiągnięty'; end if;
  insert into public.invites(token_hash, email, created_by)
  values (p_token_hash, nullif(p_email, ''), auth.uid());
  return true;
end;
$$;
grant execute on function public.create_invite(text, text) to authenticated;

create or replace function public.set_profile_status(target_id uuid, next_status public.profile_status)
returns boolean language plpgsql security definer set search_path = public as $$
declare
  active_count integer;
begin
  if not public.is_admin() or target_id = auth.uid() then return false; end if;
  if next_status = 'active' then
    perform pg_advisory_xact_lock(47010);
    select count(*) into active_count from public.profiles where status = 'active';
    if active_count >= 10 then raise exception 'Limit dziesięciu aktywnych miejsc został osiągnięty'; end if;
  end if;
  update public.profiles set status = next_status where id = target_id;
  return found;
end;
$$;
grant execute on function public.set_profile_status(uuid, public.profile_status) to authenticated;

create or replace function public.admin_delete_profile(target_id uuid)
returns boolean language plpgsql security definer set search_path = public, auth as $$
begin
  if not public.is_admin() or target_id = auth.uid() then return false; end if;
  delete from auth.users where id = target_id;
  return found;
end;
$$;
grant execute on function public.admin_delete_profile(uuid) to authenticated;

create or replace function public.sync_telegram_connection(telegram_chat_id text, telegram_username text default '')
returns boolean language plpgsql security definer set search_path = public as $$
declare
  expected_id text;
begin
  if auth.uid() is null or not public.is_active_user() then return false; end if;
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

revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.prevent_self_privilege_change() from public, anon, authenticated;
revoke execute on function public.enforce_monitor_limit() from public, anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;
revoke execute on function public.validate_monitor_filters() from public, anon, authenticated;
revoke execute on function public.valid_round_trip_pair_count(date, date, date, date) from public, anon;
grant execute on function public.valid_round_trip_pair_count(date, date, date, date) to authenticated;

revoke execute on function public.is_admin() from public;
revoke execute on function public.is_active_user(uuid) from public;
revoke execute on function public.match_within_monitor_budget(uuid) from public;
revoke execute on function public.can_read_flight_offer(uuid) from public;
revoke execute on function public.claim_invite(text) from public;
revoke execute on function public.create_invite(text, text) from public;
revoke execute on function public.set_profile_status(uuid, public.profile_status) from public;
revoke execute on function public.admin_delete_profile(uuid) from public;
revoke execute on function public.sync_telegram_connection(text, text) from public;
revoke execute on function public.is_admin() from anon;
revoke execute on function public.is_active_user(uuid) from anon;
revoke execute on function public.match_within_monitor_budget(uuid) from anon;
revoke execute on function public.can_read_flight_offer(uuid) from anon;
revoke execute on function public.claim_invite(text) from anon;
revoke execute on function public.create_invite(text, text) from anon;
revoke execute on function public.set_profile_status(uuid, public.profile_status) from anon;
revoke execute on function public.admin_delete_profile(uuid) from anon;
revoke execute on function public.sync_telegram_connection(text, text) from anon;
grant execute on function public.is_admin() to authenticated;
grant execute on function public.is_active_user(uuid) to authenticated;
grant execute on function public.match_within_monitor_budget(uuid) to authenticated;
grant execute on function public.offer_price_history_for_user(uuid[]) to authenticated;
grant execute on function public.can_read_flight_offer(uuid) to authenticated;
grant execute on function public.claim_invite(text) to authenticated;
grant execute on function public.create_invite(text, text) to authenticated;
grant execute on function public.set_profile_status(uuid, public.profile_status) to authenticated;
grant execute on function public.admin_delete_profile(uuid) to authenticated;
grant execute on function public.sync_telegram_connection(text, text) to authenticated;

alter table public.profiles enable row level security;
alter table public.invites enable row level security;
alter table public.monitors enable row level security;
alter table public.monitor_scan_items enable row level security;
alter table public.flight_offers enable row level security;
alter table public.offer_price_history enable row level security;
alter table public.offer_mutes enable row level security;
alter table public.user_matches enable row level security;
alter table public.telegram_connections enable row level security;
alter table public.telegram_outbox enable row level security;
alter table public.telegram_state enable row level security;
alter table public.telegram_auth_attempts enable row level security;
revoke all on table public.telegram_auth_attempts from public, anon, authenticated;
alter table public.scan_runs enable row level security;
alter table public.feedback enable row level security;
alter table public.user_preference_signals enable row level security;

create policy profiles_self_or_admin on public.profiles for select to authenticated using (id = auth.uid() or public.is_admin());
create policy profiles_self_update on public.profiles for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
create policy profiles_admin_delete on public.profiles for delete to authenticated using (public.is_admin() and id <> auth.uid());

create policy invites_admin_all on public.invites for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy monitors_owner_all on public.monitors for all to authenticated using (public.is_active_user(user_id) and user_id = auth.uid()) with check (public.is_active_user(user_id) and user_id = auth.uid());
create policy offers_matched_owner_read on public.flight_offers for select to authenticated using (public.can_read_flight_offer(id));
create policy offer_price_history_owner_read on public.offer_price_history for select to authenticated using (public.can_read_flight_offer(offer_id));
create policy offer_mutes_owner_all on public.offer_mutes for all to authenticated using (public.is_active_user(user_id) and user_id = auth.uid()) with check (public.is_active_user(user_id) and user_id = auth.uid());
create policy matches_owner_read on public.user_matches for select to authenticated using (public.is_active_user(user_id) and user_id = auth.uid() and public.match_within_monitor_budget(id));
create policy matches_owner_update on public.user_matches for update to authenticated using (public.is_active_user(user_id) and user_id = auth.uid() and public.match_within_monitor_budget(id)) with check (public.is_active_user(user_id) and user_id = auth.uid() and public.match_within_monitor_budget(id));
create policy connections_self_read on public.telegram_connections for select to authenticated using (public.is_active_user(user_id) and user_id = auth.uid());
create policy connections_self_delete on public.telegram_connections for delete to authenticated using (public.is_active_user(user_id) and user_id = auth.uid());
create policy telegram_outbox_admin_read on public.telegram_outbox for select to authenticated using (public.is_admin());
create policy telegram_state_admin_read on public.telegram_state for select to authenticated using (public.is_admin());
create policy scan_runs_admin_read on public.scan_runs for select to authenticated using (public.is_admin());
create policy feedback_owner_all on public.feedback for all to authenticated using (public.is_active_user(user_id) and user_id = auth.uid()) with check (
  public.is_active_user(user_id) and user_id = auth.uid()
  and exists (select 1 from public.user_matches m where m.id = match_id and m.user_id = auth.uid())
);
create policy preference_signals_owner_read on public.user_preference_signals for select to authenticated using (
  public.is_active_user(user_id) and user_id = auth.uid()
);
