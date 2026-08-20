-- A cancelled GitHub Actions job cannot run the scanner's Python finalizer,
-- so its queued/running scan row may remain until the normal 30-minute lease
-- expires. Only an explicit administrator override may reclaim such a row.

create or replace function public.force_reserve_scan_slot()
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  reserved_id uuid;
begin
  perform pg_advisory_xact_lock(47011);

  update public.scan_runs
  set status = 'error',
      finished_at = now(),
      error = coalesce(error, 'Slot odzyskany po anulowanym skanie administratora')
  where status in ('queued', 'running')
    and started_at < now() - interval '10 minutes';

  insert into public.scan_runs(status, blocked, query_count)
  values ('queued', false, 0)
  returning id into reserved_id;
  return reserved_id;
end;
$$;

revoke all on function public.force_reserve_scan_slot() from public, anon, authenticated;
grant execute on function public.force_reserve_scan_slot() to service_role;
