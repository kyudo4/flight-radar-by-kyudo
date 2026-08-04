-- Limit each outbound and return date window to 14 calendar days.
-- Both windows multiply round-trip queries, so the same bound applies to each.

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
