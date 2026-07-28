-- Budżet jest egzekwowany również przez bazę, a nie tylko przez skaner i UI.
-- Dzięki temu stary rekord ponad limitem nie może wrócić do panelu po zmianie
-- kodu frontendu ani przez bezpośrednie zapytanie do Supabase.

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
      and (
        offer.price_pln <= coalesce((monitor.filters ->> 'budget_pln')::numeric, 0)
        or coalesce(offer.tags, '[]'::jsonb) @> '["Error Fare"]'::jsonb
        or coalesce(offer.tags, '[]'::jsonb) @> '["Mistake Fare"]'::jsonb
      )
  );
$$;

revoke execute on function public.match_within_monitor_budget(uuid) from public, anon;
grant execute on function public.match_within_monitor_budget(uuid) to authenticated;

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
      and public.match_within_monitor_budget(m.id)
      and (m.user_id = auth.uid() or public.is_admin())
  );
$$;

revoke execute on function public.can_read_flight_offer(uuid) from public, anon;
grant execute on function public.can_read_flight_offer(uuid) to authenticated;

drop policy if exists matches_owner_read on public.user_matches;
create policy matches_owner_read on public.user_matches
for select to authenticated
using (
  public.is_active_user(user_id)
  and (user_id = auth.uid() or public.is_admin())
  and public.match_within_monitor_budget(id)
);

drop policy if exists offers_matched_owner_read on public.flight_offers;
create policy offers_matched_owner_read on public.flight_offers
for select to authenticated
using (public.can_read_flight_offer(id));
