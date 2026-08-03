-- Final audit hardening: close boolean side channels and return bounded,
-- owner-scoped price history without a global per-page limit.

-- Leave the existing input parameter name untouched. Production may have
-- either the legacy name `user_id` or the canonical name `candidate`.
create or replace function public.is_active_user(uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.profiles
    where id = $1
      and status = 'active'
      and $1 = auth.uid()
  );
$$;

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
      and m.user_id = auth.uid()
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
      )
  ) ranked
  where ranked.row_number <= 12
  order by ranked.observed_at desc;
$$;

revoke execute on function public.is_active_user(uuid) from public, anon;
revoke execute on function public.match_within_monitor_budget(uuid) from public, anon;
revoke execute on function public.offer_price_history_for_user(uuid[]) from public, anon;
grant execute on function public.is_active_user(uuid) to authenticated;
grant execute on function public.match_within_monitor_budget(uuid) to authenticated;
grant execute on function public.offer_price_history_for_user(uuid[]) to authenticated;
