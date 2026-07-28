-- Flight Radar by Kyudo: migracja dla projektu, w którym schema.sql był już wykonany.
-- Uruchom po schema.sql albo na istniejącej bazie przed kolejnym skanem.

alter table if exists public.flight_offers drop column if exists seat_note;

alter table if exists public.user_matches
  add column if not exists telegram_eligible boolean not null default false,
  add column if not exists new_airline boolean not null default false,
  add column if not exists updated_at timestamptz not null default now();

create table if not exists public.monitor_scan_items (
  id uuid primary key default gen_random_uuid(),
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  origin text not null,
  destination text not null,
  travel_date date not null,
  cabin text not null,
  last_scanned_at timestamptz,
  next_scan_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (monitor_id, origin, destination, travel_date, cabin)
);

create index if not exists monitor_scan_queue_idx
  on public.monitor_scan_items(next_scan_at, last_scanned_at);
create index if not exists monitor_scan_monitor_idx
  on public.monitor_scan_items(monitor_id);

create or replace function public.enforce_monitor_limit()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  monitor_count integer;
begin
  if new.status <> 'expired' then
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

drop trigger if exists enforce_monitor_limit on public.monitors;
create trigger enforce_monitor_limit
  before insert or update of user_id, status on public.monitors
  for each row execute procedure public.enforce_monitor_limit();

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists touch_monitors_updated_at on public.monitors;
create trigger touch_monitors_updated_at
  before update on public.monitors
  for each row execute procedure public.touch_updated_at();

drop trigger if exists touch_matches_updated_at on public.user_matches;
create trigger touch_matches_updated_at
  before update on public.user_matches
  for each row execute procedure public.touch_updated_at();

alter table public.monitor_scan_items enable row level security;
