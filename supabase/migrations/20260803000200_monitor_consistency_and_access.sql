-- Keep visible results aligned with the current monitor filters and make the
-- owner-scoped history RPC use the same visibility and budget rules as RLS.

create or replace function public.hide_monitor_matches_on_filter_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.filters is distinct from old.filters then
    update public.user_matches
    set visible = false,
        telegram_eligible = false,
        updated_at = now()
    where monitor_id = new.id
      and visible;
  end if;
  return new;
end;
$$;

drop trigger if exists hide_monitor_matches_on_filter_change on public.monitors;
create trigger hide_monitor_matches_on_filter_change
  after update of filters on public.monitors
  for each row execute procedure public.hide_monitor_matches_on_filter_change();

create or replace function public.match_within_monitor_budget(p_match_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.user_matches m
    join public.monitors monitor on monitor.id = m.monitor_id
    join public.flight_offers offer on offer.id = m.offer_id
    where m.id = p_match_id
      and m.visible
      and m.user_id = auth.uid()
      and public.is_active_user(m.user_id)
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((monitor.filters ->> 'budget_pln')::numeric, 0)
  );
$$;

create or replace function public.offer_price_history_for_user(p_offer_ids uuid[])
returns table(offer_id uuid, price_pln integer, observed_at timestamptz)
language sql
stable
security definer
set search_path = public
as $$
  select ranked.offer_id, ranked.price_pln, ranked.observed_at
  from (
    select history.offer_id, history.price_pln, history.observed_at,
           row_number() over (
             partition by history.offer_id
             order by history.observed_at desc, history.id desc
           ) as row_number
    from public.offer_price_history history
    where history.offer_id = any(coalesce(p_offer_ids, '{}'::uuid[]))
      and exists (
        select 1
        from public.user_matches match
        where match.offer_id = history.offer_id
          and match.user_id = auth.uid()
          and match.visible
          and public.is_active_user(match.user_id)
          and public.match_within_monitor_budget(match.id)
      )
  ) ranked
  where ranked.row_number <= 12
  order by ranked.observed_at desc;
$$;

revoke execute on function public.hide_monitor_matches_on_filter_change() from public, anon, authenticated;
revoke execute on function public.match_within_monitor_budget(uuid) from public, anon;
revoke execute on function public.offer_price_history_for_user(uuid[]) from public, anon;
grant execute on function public.match_within_monitor_budget(uuid) to authenticated;
grant execute on function public.offer_price_history_for_user(uuid[]) to authenticated;
