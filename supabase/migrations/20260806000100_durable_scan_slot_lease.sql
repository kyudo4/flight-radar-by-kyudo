-- Scheduled GitHub runs must be serialized by the database, not by the
-- Actions pending queue. A normal browser scan can run longer than ten
-- minutes, so a second run must not start while its first run is active.
create or replace function public.reserve_scan_slot()
returns uuid language plpgsql security definer set search_path = public as $$
declare
  reserved_id uuid;
begin
  perform pg_advisory_xact_lock(47011);
  if exists (
    select 1
    from public.scan_runs
    where status in ('queued', 'running')
      and started_at >= now() - interval '30 minutes'
  ) then
    return null;
  end if;
  if exists (
    select 1
    from public.scan_runs
    where started_at >= now() - interval '10 minutes'
  ) then
    return null;
  end if;
  insert into public.scan_runs(status, blocked, query_count)
  values ('queued', false, 0)
  returning id into reserved_id;
  return reserved_id;
end;
$$;

revoke all on function public.reserve_scan_slot() from public, anon, authenticated;
grant execute on function public.reserve_scan_slot() to service_role;
