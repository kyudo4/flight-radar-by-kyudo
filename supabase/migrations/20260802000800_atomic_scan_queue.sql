-- Atomic and idempotent reconciliation of a monitor's persistent scan queue.
-- Prevents false HTTP 409 errors when a monitor edit and a scanner run overlap.

create or replace function public.sync_monitor_scan_items(
  p_monitor_id uuid,
  p_items jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  desired_count integer;
  queue_count integer;
begin
  if jsonb_typeof(coalesce(p_items, 'null'::jsonb)) <> 'array' then
    raise exception 'Pozycje kolejki muszą być tablicą JSON';
  end if;
  desired_count := jsonb_array_length(p_items);
  if desired_count > 5000 then
    raise exception 'Kolejka monitora przekracza limit 5000 kombinacji';
  end if;
  if not exists (select 1 from public.monitors where id = p_monitor_id) then
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
      select 1
      from jsonb_to_recordset(p_items) as desired(
        origin text,
        destination text,
        travel_date date,
        return_date date,
        trip_type text,
        cabin text
      )
      where upper(trim(desired.origin)) = current_item.origin
        and upper(trim(desired.destination)) = current_item.destination
        and desired.travel_date = current_item.travel_date
        and desired.return_date is not distinct from current_item.return_date
        and desired.trip_type = current_item.trip_type
        and lower(replace(desired.cabin, '_', '-')) = current_item.cabin
    );

  insert into public.monitor_scan_items(
    monitor_id, origin, destination, travel_date, return_date, trip_type, cabin
  )
  select
    p_monitor_id,
    upper(trim(desired.origin)),
    upper(trim(desired.destination)),
    desired.travel_date,
    desired.return_date,
    desired.trip_type,
    lower(replace(desired.cabin, '_', '-'))
  from jsonb_to_recordset(p_items) as desired(
    origin text,
    destination text,
    travel_date date,
    return_date date,
    trip_type text,
    cabin text
  )
  on conflict do nothing;

  select count(*) into queue_count
  from public.monitor_scan_items
  where monitor_id = p_monitor_id;

  return jsonb_build_object(
    'desired_count', desired_count,
    'queue_count', queue_count
  );
end;
$$;

revoke all on function public.sync_monitor_scan_items(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.sync_monitor_scan_items(uuid, jsonb) to service_role;
