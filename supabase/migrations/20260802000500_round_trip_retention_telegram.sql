-- Complete bounded retention after the round-trip reliability audit.

create index if not exists flight_offers_travel_date_idx
  on public.flight_offers(travel_date);

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
    where row_number > 20 or observed_at < now() - interval '30 days'
  );
  get diagnostics history_deleted = row_count;

  -- A past flight cannot become a useful alert again. Remove its detailed
  -- match after one week, but retain every explicit user reaction so the
  -- preference-learning signal remains available.
  delete from public.user_matches match
  using public.flight_offers offer
  where match.offer_id = offer.id
    and offer.travel_date < current_date - 7
    and match.feedback is null
    and not exists (
      select 1 from public.feedback saved_feedback
      where saved_feedback.match_id = match.id
    );
  get diagnostics matches_deleted = row_count;

  delete from public.monitors
  where status = 'expired' and updated_at < now() - interval '30 days';
  get diagnostics monitors_deleted = row_count;

  delete from public.flight_offers offer
  where (offer.last_seen_at < now() - interval '45 days'
         or offer.travel_date < current_date - 7)
    and not exists (
      select 1 from public.user_matches match
      where match.offer_id = offer.id
    );
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

revoke all on function public.cleanup_retention() from public, anon, authenticated;
grant execute on function public.cleanup_retention() to service_role;
