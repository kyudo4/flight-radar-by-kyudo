-- Scan controls, Google-only Telegram alerts and a durable filter-edit repair.

create or replace function public.hide_monitor_matches_on_filter_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.filters is distinct from old.filters then
    -- Re-evaluate every old match, including rows hidden by the previous
    -- all-or-none trigger. Matching offers return to the panel; offers that
    -- no longer fit stay hidden and cannot trigger an alert.
    update public.user_matches matches
    set visible = decision.still_matches,
        telegram_eligible = case
          when decision.still_matches
            then matches.telegram_eligible
          else false
        end,
        updated_at = now()
    from (
      select existing.id,
             public.offer_matches_monitor_filters(existing.offer_id, new.filters) as still_matches
      from public.user_matches existing
      where existing.monitor_id = new.id
    ) decision
    where matches.id = decision.id
      and matches.visible is distinct from decision.still_matches;
  end if;
  return new;
end;
$$;

-- With scans every six hours, 30 changed-price points provide a useful trend
-- without turning a 30-day retention policy into an unbounded history table.
create or replace function public.cleanup_retention()
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  history_deleted bigint := 0;
  matches_deleted bigint := 0;
  monitors_deleted bigint := 0;
  offers_deleted bigint := 0;
  runs_deleted bigint := 0;
  auth_attempts_deleted bigint := 0;
  invites_deleted bigint := 0;
begin
  delete from public.offer_price_history
  where id in (
    select id from (
      select id, observed_at,
             row_number() over (partition by offer_id order by observed_at desc, id desc) as row_number
      from public.offer_price_history
    ) ranked
    where row_number > 30 or observed_at < now() - interval '30 days'
  );
  get diagnostics history_deleted = row_count;

  delete from public.user_matches match
  using public.flight_offers offer
  where match.offer_id = offer.id
    and offer.travel_date < current_date - 7;
  get diagnostics matches_deleted = row_count;

  delete from public.monitors where status = 'expired' and updated_at < now() - interval '30 days';
  get diagnostics monitors_deleted = row_count;

  delete from public.flight_offers offer
  where (offer.last_seen_at < now() - interval '45 days'
         or offer.travel_date < current_date - 7)
    and not exists (select 1 from public.user_matches match where match.offer_id = offer.id);
  get diagnostics offers_deleted = row_count;

  delete from public.scan_runs where started_at < now() - interval '30 days';
  get diagnostics runs_deleted = row_count;
  delete from public.telegram_auth_attempts where attempted_at < now() - interval '2 days';
  get diagnostics auth_attempts_deleted = row_count;
  delete from public.invites where expires_at < now() - interval '30 days';
  get diagnostics invites_deleted = row_count;

  return jsonb_build_object(
    'history_deleted', history_deleted,
    'matches_deleted', matches_deleted,
    'monitors_deleted', monitors_deleted,
    'offers_deleted', offers_deleted,
    'runs_deleted', runs_deleted,
    'auth_attempts_deleted', auth_attempts_deleted,
    'invites_deleted', invites_deleted
  );
end;
$$;

create or replace function public.offer_price_history_for_user(p_offer_ids uuid[])
returns table(offer_id uuid, price_pln integer, observed_at timestamptz)
language sql stable security definer set search_path = public as $$
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
        select 1 from public.user_matches match
        where match.offer_id = history.offer_id
          and match.user_id = auth.uid()
          and match.visible
          and public.is_active_user(match.user_id)
          and public.match_within_monitor_budget(match.id)
      )
  ) ranked
  where ranked.row_number <= 30
  order by ranked.observed_at desc;
$$;
