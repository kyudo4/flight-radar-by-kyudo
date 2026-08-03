-- Keep the database limit identical to the frontend and scanner.
-- A round-trip combination exists only when the return is after departure.

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

revoke execute on function public.valid_round_trip_pair_count(date, date, date, date) from public, anon, authenticated;

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
