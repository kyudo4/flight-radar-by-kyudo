-- Final RLS state: every radar reads only its owner's monitors, matches,
-- offers and Telegram connection. Administrative account management keeps
-- using the dedicated security-definer RPCs and never needs cross-user radar data.

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
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((monitor.filters ->> 'budget_pln')::numeric, 0)
  );
$$;

create or replace function public.can_read_flight_offer(p_offer_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.user_matches m
    where m.offer_id = p_offer_id
      and m.visible
      and public.is_active_user(m.user_id)
      and m.user_id = auth.uid()
      and public.match_within_monitor_budget(m.id)
  );
$$;

revoke execute on function public.match_within_monitor_budget(uuid) from public, anon;
revoke execute on function public.can_read_flight_offer(uuid) from public, anon;
grant execute on function public.match_within_monitor_budget(uuid) to authenticated;
grant execute on function public.can_read_flight_offer(uuid) to authenticated;

drop policy if exists profiles_self_update on public.profiles;
create policy profiles_self_update on public.profiles
for update to authenticated
using (id = auth.uid())
with check (id = auth.uid());

drop policy if exists monitors_owner_all on public.monitors;
create policy monitors_owner_all on public.monitors
for all to authenticated
using (public.is_active_user(user_id) and user_id = auth.uid())
with check (public.is_active_user(user_id) and user_id = auth.uid());

drop policy if exists offers_matched_owner_read on public.flight_offers;
create policy offers_matched_owner_read on public.flight_offers
for select to authenticated
using (public.can_read_flight_offer(id));

drop policy if exists matches_owner_read on public.user_matches;
create policy matches_owner_read on public.user_matches
for select to authenticated
using (
  public.is_active_user(user_id)
  and user_id = auth.uid()
  and public.match_within_monitor_budget(id)
);

drop policy if exists matches_owner_update on public.user_matches;
create policy matches_owner_update on public.user_matches
for update to authenticated
using (
  public.is_active_user(user_id)
  and user_id = auth.uid()
  and public.match_within_monitor_budget(id)
)
with check (
  public.is_active_user(user_id)
  and user_id = auth.uid()
  and public.match_within_monitor_budget(id)
);

drop policy if exists connections_self_read on public.telegram_connections;
create policy connections_self_read on public.telegram_connections
for select to authenticated
using (public.is_active_user(user_id) and user_id = auth.uid());

drop policy if exists connections_self_delete on public.telegram_connections;
create policy connections_self_delete on public.telegram_connections
for delete to authenticated
using (public.is_active_user(user_id) and user_id = auth.uid());
