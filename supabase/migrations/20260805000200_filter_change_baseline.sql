-- Treat offers discovered after a monitor edit as a fresh comparison set.
-- Older offers remain visible when they still match, but cannot suppress a
-- newly selected date at the same price merely because it existed before the
-- filter change.

alter table public.monitors
  add column if not exists filters_changed_at timestamptz not null default now();

create or replace function public.mark_monitor_filters_changed()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.filters is distinct from old.filters then
    new.filters_changed_at = now();
  end if;
  return new;
end;
$$;

drop trigger if exists mark_monitor_filters_changed on public.monitors;
create trigger mark_monitor_filters_changed
  before update of filters on public.monitors
  for each row execute procedure public.mark_monitor_filters_changed();

revoke all on function public.mark_monitor_filters_changed() from public, anon, authenticated;
