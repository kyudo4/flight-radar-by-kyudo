-- Round-trip monitors plus persistent, adaptive Google query limits.
-- Additive migration: existing one-way monitors keep working unchanged.

alter table public.monitor_scan_items
  add column if not exists return_date date,
  add column if not exists trip_type text not null default 'one_way';

alter table public.flight_offers
  add column if not exists return_date date,
  add column if not exists trip_type text not null default 'one_way';

alter table public.scan_runs
  add column if not exists standard_limit integer,
  add column if not exists first_limit integer,
  add column if not exists blocked boolean not null default false;

alter table public.monitor_scan_items
  drop constraint if exists monitor_scan_items_monitor_id_origin_destination_travel_date_cabin_key;

alter table public.monitor_scan_items
  drop constraint if exists monitor_scan_items_trip_type_check;
alter table public.monitor_scan_items
  add constraint monitor_scan_items_trip_type_check check (trip_type in ('one_way', 'round_trip'));

alter table public.flight_offers
  drop constraint if exists flight_offers_trip_type_check;
alter table public.flight_offers
  add constraint flight_offers_trip_type_check check (trip_type in ('one_way', 'round_trip'));

create unique index if not exists monitor_scan_items_one_way_key
  on public.monitor_scan_items(monitor_id, origin, destination, travel_date, cabin)
  where return_date is null and trip_type = 'one_way';

create unique index if not exists monitor_scan_items_round_trip_key
  on public.monitor_scan_items(monitor_id, origin, destination, travel_date, return_date, cabin)
  where return_date is not null and trip_type = 'round_trip';

create index if not exists monitor_scan_items_round_trip_queue_idx
  on public.monitor_scan_items(monitor_id, travel_date, return_date, cabin, next_scan_at);
create index if not exists offers_round_trip_date_idx
  on public.flight_offers(origin, destination, travel_date, return_date, cabin);

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
begin
  if length(trim(coalesce(new.name, ''))) < 1 or length(new.name) > 120 then
    raise exception 'Nazwa monitora musi mieć od 1 do 120 znaków';
  end if;
  if jsonb_typeof(coalesce(new.filters -> 'origins', 'null'::jsonb)) <> 'array'
     or jsonb_typeof(coalesce(new.filters -> 'destinations', 'null'::jsonb)) <> 'array' then
    raise exception 'Lotniska muszą być zapisane jako listy';
  end if;
  from_date := nullif(new.filters ->> 'from', '')::date;
  to_date := nullif(new.filters ->> 'to', '')::date;
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
  if exists (select 1 from jsonb_array_elements_text(new.filters -> 'origins') as x(value) where x.value !~ '^[A-Z]{3}$')
     or exists (select 1 from jsonb_array_elements_text(new.filters -> 'destinations') as x(value) where x.value !~ '^[A-Z]{3}$') then
    raise exception 'Kod lotniska musi mieć dokładnie trzy wielkie litery';
  end if;
  if from_date is null or to_date is null or to_date < from_date or to_date - from_date > 31 then
    raise exception 'Zakres dat wylotu może mieć maksymalnie 32 dni';
  end if;
  if trip not in ('one_way', 'round_trip') then
    raise exception 'Nieprawidłowy typ podróży';
  end if;
  if trip = 'round_trip' and (return_from_date is null or return_to_date is null or return_to_date < return_from_date or return_to_date - return_from_date > 31) then
    raise exception 'Zakres dat powrotu może mieć maksymalnie 32 dni';
  end if;
  if budget <= 0 or budget > 1000000 or (duration is not null and duration <= 0) or stops < 0 or stops > 9 then
    raise exception 'Nieprawidłowy limit czasu lub przesiadek';
  end if;
  if new.filters ? 'cabins' then
    if jsonb_typeof(new.filters -> 'cabins') <> 'array'
       or jsonb_array_length(new.filters -> 'cabins') < 1
       or jsonb_array_length(new.filters -> 'cabins') > 4
       or exists (select 1 from jsonb_array_elements_text(new.filters -> 'cabins') as x(value) where x.value not in ('BUSINESS', 'FIRST', 'PREMIUM_ECONOMY', 'ECONOMY')) then
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
