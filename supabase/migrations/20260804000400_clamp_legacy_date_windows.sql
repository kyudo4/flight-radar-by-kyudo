-- Bring monitors created before the 14-day rule into the new safe range.
-- The outbound and return starts stay unchanged; only an over-wide end date
-- is shortened to the first 14 calendar days.

drop trigger if exists validate_monitor_date_windows on public.monitors;

with source as (
  select
    id,
    filters,
    nullif(filters ->> 'from', '')::date as from_date,
    nullif(filters ->> 'to', '')::date as to_date,
    nullif(filters ->> 'return_from', '')::date as return_from_date,
    nullif(filters ->> 'return_to', '')::date as return_to_date,
    coalesce(nullif(filters ->> 'trip_type', ''), 'one_way') as trip
  from public.monitors
), clamped as (
  select
    id,
    case
      when to_date - from_date > 13 then
        jsonb_set(filters, '{to}', to_jsonb((from_date + 13)::text), true)
      else filters
    end as outbound_filters,
    return_from_date,
    return_to_date,
    trip
  from source
  where from_date is not null
    and to_date is not null
    and to_date >= from_date
    and (to_date - from_date > 13
         or (trip = 'round_trip' and return_from_date is not null
             and return_to_date is not null
             and return_to_date - return_from_date > 13))
)
update public.monitors monitor
set filters = case
  when clamped.trip = 'round_trip'
       and clamped.return_from_date is not null
       and clamped.return_to_date is not null
       and clamped.return_to_date - clamped.return_from_date > 13
    then jsonb_set(clamped.outbound_filters, '{return_to}',
                   to_jsonb((clamped.return_from_date + 13)::text), true)
  else clamped.outbound_filters
end
from clamped
where monitor.id = clamped.id;

create trigger validate_monitor_date_windows
  before insert or update of filters on public.monitors
  for each row execute procedure public.validate_monitor_date_windows();
