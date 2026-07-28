-- Ograniczenie rozmiaru pojedynczego monitora, aby kontrolować liczbę zapytań.
create or replace function public.enforce_monitor_airport_limit()
returns trigger language plpgsql set search_path = public as $$
declare
  origin_count integer;
  destination_count integer;
begin
  origin_count := jsonb_array_length(coalesce(new.filters -> 'origins', '[]'::jsonb));
  destination_count := jsonb_array_length(coalesce(new.filters -> 'destinations', '[]'::jsonb));
  if origin_count > 5 or destination_count > 5 then
    raise exception 'Monitor może zawierać maksymalnie 5 lotnisk wylotu i 5 celów';
  end if;
  return new;
end;
$$;

drop trigger if exists enforce_monitor_airport_limit on public.monitors;
create trigger enforce_monitor_airport_limit
  before insert or update of filters on public.monitors
  for each row execute procedure public.enforce_monitor_airport_limit();

revoke execute on function public.enforce_monitor_airport_limit() from public, anon, authenticated;
