-- Durable repair for the round-trip queue.
-- The previous migration used an untruncated PostgreSQL constraint name, so
-- the old unique key survived and collapsed different return dates.

do $$
declare
  old_constraint record;
begin
  for old_constraint in
    select c.conname
    from pg_constraint c
    where c.conrelid = 'public.monitor_scan_items'::regclass
      and c.contype = 'u'
      and (
        select array_agg(a.attname order by key_position)
        from unnest(c.conkey) with ordinality as key_columns(attnum, key_position)
        join pg_attribute a
          on a.attrelid = c.conrelid
         and a.attnum = key_columns.attnum
      ) = array['monitor_id', 'origin', 'destination', 'travel_date', 'cabin']::name[]
  loop
    execute format('alter table public.monitor_scan_items drop constraint %I', old_constraint.conname);
  end loop;
end;
$$;

-- Cover the concrete PostgreSQL-truncated name observed in production.
drop index if exists public.monitor_scan_items_monitor_id_origin_destination_travel_dat_key;

-- Also remove a manually-created old unique index, if one exists without a
-- backing constraint. This makes the repair independent of object naming.
do $$
declare
  old_index record;
begin
  for old_index in
    select index_class.relname as index_name
    from pg_index index_definition
    join pg_class index_class on index_class.oid = index_definition.indexrelid
    where index_definition.indrelid = 'public.monitor_scan_items'::regclass
      and index_definition.indisunique
      and index_definition.indpred is null
      and not exists (
        select 1
        from pg_constraint constraint_definition
        where constraint_definition.conindid = index_definition.indexrelid
      )
      and (
        select array_agg(a.attname order by key_position)
        from unnest(index_definition.indkey) with ordinality as key_columns(attnum, key_position)
        join pg_attribute a
          on a.attrelid = index_definition.indrelid
         and a.attnum = key_columns.attnum
      ) = array['monitor_id', 'origin', 'destination', 'travel_date', 'cabin']::name[]
  loop
    execute format('drop index public.%I', old_index.index_name);
  end loop;
end;
$$;

create index if not exists monitor_scan_queue_idx
  on public.monitor_scan_items(next_scan_at, last_scanned_at);
create index if not exists monitor_scan_monitor_idx
  on public.monitor_scan_items(monitor_id);

create unique index if not exists monitor_scan_items_one_way_key
  on public.monitor_scan_items(monitor_id, origin, destination, travel_date, cabin)
  where return_date is null and trip_type = 'one_way';

create unique index if not exists monitor_scan_items_round_trip_key
  on public.monitor_scan_items(monitor_id, origin, destination, travel_date, return_date, cabin)
  where return_date is not null and trip_type = 'round_trip';

-- Keep atomic reconciliation available when an older deployment skipped the
-- historical queue migration.
create or replace function public.sync_monitor_scan_items(
  p_monitor_id uuid,
  p_items jsonb
)
returns jsonb language plpgsql security definer set search_path = public as $$
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
    monitor_id, origin, destination, travel_date, return_date, trip_type, cabin
  )
  select p_monitor_id, upper(trim(desired.origin)), upper(trim(desired.destination)),
    desired.travel_date, desired.return_date, desired.trip_type,
    lower(replace(desired.cabin, '_', '-'))
  from jsonb_to_recordset(p_items) as desired(
    origin text, destination text, travel_date date, return_date date,
    trip_type text, cabin text
  )
  on conflict do nothing;

  select count(*) into queue_count
  from public.monitor_scan_items
  where monitor_id = p_monitor_id;

  if queue_count <> desired_count then
    raise exception 'Niepełna kolejka monitora: oczekiwano %, zapisano %', desired_count, queue_count;
  end if;

  return jsonb_build_object('desired_count', desired_count, 'queue_count', queue_count);
end;
$$;

revoke all on function public.sync_monitor_scan_items(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.sync_monitor_scan_items(uuid, jsonb) to service_role;

alter table public.scan_runs
  add column if not exists due_count integer not null default 0,
  add column if not exists selected_count integer not null default 0,
  add column if not exists failed_count integer not null default 0,
  add column if not exists deferred_count integer not null default 0,
  add column if not exists coverage_percent numeric(5,2) not null default 0.00;

alter table public.scan_runs
  drop constraint if exists scan_runs_coverage_percent_check;
alter table public.scan_runs
  add constraint scan_runs_coverage_percent_check
  check (coverage_percent >= 0 and coverage_percent <= 100);

do $$
begin
  if exists (
    select 1
    from pg_constraint c
    where c.conrelid = 'public.monitor_scan_items'::regclass
      and c.contype = 'u'
      and (
        select array_agg(a.attname order by key_position)
        from unnest(c.conkey) with ordinality as key_columns(attnum, key_position)
        join pg_attribute a
          on a.attrelid = c.conrelid
         and a.attnum = key_columns.attnum
      ) = array['monitor_id', 'origin', 'destination', 'travel_date', 'cabin']::name[]
  ) then
    raise exception 'Stare ograniczenie kolejki nadal istnieje';
  end if;
  if not exists (select 1 from pg_class where relname = 'monitor_scan_items_one_way_key')
     or not exists (select 1 from pg_class where relname = 'monitor_scan_items_round_trip_key') then
    raise exception 'Brak wymaganych indeksów kolejki';
  end if;
end;
$$;
