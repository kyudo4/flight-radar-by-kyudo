-- Fix the RLS lookup used when the dashboard reads an offer through a match.
-- The security-definer helper avoids the policy recursion that made the
-- user_matches row visible while its related flight_offers row was hidden.

create or replace function public.can_read_flight_offer(p_offer_id uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1
    from public.user_matches m
    where m.offer_id = p_offer_id
      and m.visible
      and public.is_active_user(m.user_id)
      and (m.user_id = auth.uid() or public.is_admin())
  );
$$;

revoke execute on function public.can_read_flight_offer(uuid) from public, anon;
grant execute on function public.can_read_flight_offer(uuid) to authenticated;

drop policy if exists offers_matched_owner_read on public.flight_offers;
create policy offers_matched_owner_read on public.flight_offers
for select to authenticated
using (public.can_read_flight_offer(id));
