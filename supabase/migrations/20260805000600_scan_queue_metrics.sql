-- Make scan history distinguish unique Google queries from stored monitor
-- queue rows. The latter naturally changes as rows become due, are retried,
-- or are shared by several monitors.

alter table public.scan_runs
  add column if not exists due_item_count integer not null default 0,
  add column if not exists total_queue_count integer not null default 0;

create or replace function public.scan_queue_summary()
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'due_item_count', count(*) filter (where items.next_scan_at <= now())::integer,
    'total_queue_count', count(*)::integer
  )
  from public.monitor_scan_items items
  join public.monitors monitors on monitors.id = items.monitor_id
  where monitors.status = 'active';
$$;

revoke all on function public.scan_queue_summary() from public, anon, authenticated;
grant execute on function public.scan_queue_summary() to service_role;
