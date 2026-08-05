-- Make monitor edits immediately scannable and make the edit a distinct
-- notification scope without weakening the normal duplicate/10% safeguards.

alter table public.monitors
  add column if not exists queue_generation bigint not null default 0;

alter table public.monitor_scan_items
  add column if not exists queue_generation bigint not null default 0;

alter table public.user_matches
  add column if not exists notified_generation bigint not null default 0;

create or replace function public.mark_monitor_filters_changed()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.filters is distinct from old.filters then
    new.filters_changed_at = now();
    new.queue_generation = coalesce(old.queue_generation, 0) + 1;
  end if;
  return new;
end;
$$;

drop trigger if exists mark_monitor_filters_changed on public.monitors;
create trigger mark_monitor_filters_changed
  before update of filters on public.monitors
  for each row execute procedure public.mark_monitor_filters_changed();

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
      or candidate.cabin is null
      or lower(replace(candidate.cabin, '_', '-')) not in ('economy', 'premium-economy', 'business', 'first')
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

  -- Existing rows keep their history on ordinary syncs. After an edit, the
  -- generation changes and every retained combination becomes due now.
  update public.monitor_scan_items
  set queue_generation = current_generation,
      last_scanned_at = null,
      next_scan_at = now()
  where monitor_id = p_monitor_id
    and queue_generation is distinct from current_generation;

  select count(*) into queue_count
  from public.monitor_scan_items
  where monitor_id = p_monitor_id;

  if queue_count <> desired_count then
    raise exception 'Niepełna kolejka monitora: oczekiwano %, zapisano %', desired_count, queue_count;
  end if;

  return jsonb_build_object('desired_count', desired_count, 'queue_count', queue_count, 'queue_generation', current_generation);
end;
$$;

-- Keep the monitor summary truthful: next_scan_at is the earliest queue item,
-- not the timestamp of whichever shared query happened to finish first.
create or replace function public.refresh_monitor_scan_status(p_monitor_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
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

  return jsonb_build_object(
    'total_count', total_count,
    'due_count', due_count,
    'last_scanned_at', latest_scan,
    'next_scan_at', case when due_count > 0 then now() else earliest_next end
  );
end;
$$;

revoke all on function public.refresh_monitor_scan_status(uuid) from public, anon, authenticated;
grant execute on function public.refresh_monitor_scan_status(uuid) to service_role;

create or replace function public.get_monitor_scan_progress(p_monitor_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  result jsonb;
begin
  if not exists (
    select 1 from public.monitors
    where id = p_monitor_id and user_id = auth.uid()
      and public.is_active_user(user_id)
  ) then
    return null;
  end if;

  select jsonb_build_object(
    'total_count', count(*)::integer,
    'due_count', count(*) filter (where next_scan_at <= now())::integer,
    'scanned_count', count(*) filter (where last_scanned_at is not null)::integer
  ) into result
  from public.monitor_scan_items
  where monitor_id = p_monitor_id;
  return result;
end;
$$;

revoke all on function public.get_monitor_scan_progress(uuid) from public, anon;
grant execute on function public.get_monitor_scan_progress(uuid) to authenticated;
