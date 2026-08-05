-- Reconcile already stored offers with every active monitor after filter edits.
-- A shared offer must not wait for its original queue item to be scanned again
-- before it becomes visible to a monitor whose filters still match it.

create or replace function public.reconcile_monitor_offers(p_monitor_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  result jsonb;
begin
  with candidates as (
    select offer.id, monitor.user_id, monitor.id as monitor_id
    from public.monitors monitor
    join public.flight_offers offer
      on offer.verification_status <> 'stale'
     and offer.last_seen_at >= now() - interval '7 days'
     and public.offer_matches_monitor_filters(offer.id, monitor.filters)
    where monitor.id = p_monitor_id
      and monitor.status = 'active'
  ), existing as (
    select candidates.*, matches.id as match_id, matches.visible as was_visible
    from candidates
    left join public.user_matches matches
      on matches.monitor_id = candidates.monitor_id
     and matches.offer_id = candidates.id
  ), upserted as (
    insert into public.user_matches(
      user_id, monitor_id, offer_id, stars, visible,
      telegram_eligible, new_airline
    )
    select user_id, monitor_id, id, 3, true, true, false
    from candidates
    on conflict (user_id, monitor_id, offer_id) do update
      set visible = true,
          telegram_eligible = case
            when public.user_matches.visible is distinct from true
              then true
            else public.user_matches.telegram_eligible
          end,
          updated_at = now()
    returning id, offer_id
  )
  select jsonb_build_object(
    'matched_count', count(*)::integer,
    'new_offer_ids', coalesce(
      jsonb_agg(upserted.offer_id) filter (where existing.match_id is null),
      '[]'::jsonb
    ),
    'reactivated_offer_ids', coalesce(
      jsonb_agg(upserted.offer_id) filter (where existing.was_visible is distinct from true and existing.match_id is not null),
      '[]'::jsonb
    )
  )
  into result
  from upserted
  left join existing
    on existing.match_id = upserted.id;

  return coalesce(result, '{"matched_count":0,"new_offer_ids":[],"reactivated_offer_ids":[]}'::jsonb);
end;
$$;

create or replace function public.hide_monitor_matches_on_filter_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.filters is distinct from old.filters then
    update public.user_matches matches
    set visible = public.offer_matches_monitor_filters(matches.offer_id, new.filters),
        telegram_eligible = case
          when public.offer_matches_monitor_filters(matches.offer_id, new.filters)
            then matches.telegram_eligible
          else false
        end,
        updated_at = now()
    where matches.monitor_id = new.id;

    -- This also creates a missing assignment when the offer was found for a
    -- different monitor but already exists in the shared offer table.
    perform public.reconcile_monitor_offers(new.id);
  end if;
  return new;
end;
$$;

drop trigger if exists hide_monitor_matches_on_filter_change on public.monitors;
create trigger hide_monitor_matches_on_filter_change
  after update of filters on public.monitors
  for each row execute procedure public.hide_monitor_matches_on_filter_change();

-- Repair existing production rows once, without deleting any offer history.
do $$
declare
  monitor_row record;
begin
  for monitor_row in
    select id from public.monitors where status = 'active'
  loop
    perform public.reconcile_monitor_offers(monitor_row.id);
  end loop;
end;
$$;

-- Return only current, owner-scoped offers after monitor filtering. This keeps
-- pagination from dropping valid rows after a page has already been fetched.
create or replace function public.get_my_offer_matches(
  p_limit integer default 40,
  p_offset integer default 0,
  p_query text default '',
  p_cabin text default '',
  p_min_stars integer default 0,
  p_freshness text default 'fresh',
  p_sort text default 'newest'
)
returns table(
  match_id uuid,
  monitor_id uuid,
  offer_id uuid,
  stars integer,
  feedback text,
  notified_at timestamptz,
  updated_at timestamptz,
  fingerprint text,
  source text,
  route text,
  origin text,
  destination text,
  travel_date date,
  return_date date,
  trip_type text,
  cabin text,
  airline text,
  airline_name text,
  price_pln integer,
  duration_minutes integer,
  stops integer,
  aircraft text,
  link text,
  tags jsonb,
  raw jsonb,
  last_seen_at timestamptz,
  verification_status text,
  verification_note text
)
language sql
stable
security definer
set search_path = public
as $$
  select matches.id, matches.monitor_id, matches.offer_id, matches.stars,
         matches.feedback, matches.notified_at, matches.updated_at,
         offer.fingerprint, offer.source, offer.route, offer.origin,
         offer.destination, offer.travel_date, offer.return_date,
         offer.trip_type, offer.cabin, offer.airline, offer.airline_name,
         offer.price_pln, offer.duration_minutes, offer.stops, offer.aircraft,
         offer.link, offer.tags, offer.raw, offer.last_seen_at,
         offer.verification_status, offer.verification_note
  from public.user_matches matches
  join public.monitors monitor on monitor.id = matches.monitor_id
  join public.flight_offers offer on offer.id = matches.offer_id
  where matches.user_id = auth.uid()
    and public.is_active_user(matches.user_id)
    and monitor.status = 'active'
    and matches.visible
    and public.offer_matches_monitor_filters(offer.id, monitor.filters)
    and (
      nullif(trim(coalesce(p_query, '')), '') is null
      or lower(coalesce(offer.route, '')) like '%' || lower(trim(p_query)) || '%'
      or lower(coalesce(offer.airline_name, '')) like '%' || lower(trim(p_query)) || '%'
      or lower(coalesce(offer.source, '')) like '%' || lower(trim(p_query)) || '%'
    )
    and (nullif(trim(coalesce(p_cabin, '')), '') is null or upper(offer.cabin) = upper(trim(p_cabin)))
    and matches.stars >= greatest(coalesce(p_min_stars, 0), 0)
    and (
      coalesce(p_freshness, 'fresh') = 'all'
      or (p_freshness = 'fresh' and offer.verification_status <> 'stale')
      or (p_freshness = 'stale' and offer.verification_status = 'stale')
    )
  order by
    case when p_sort = 'price' then offer.price_pln end asc nulls last,
    case when p_sort = 'stars' then matches.stars end desc nulls last,
    case when p_sort = 'newest' then matches.updated_at end desc nulls last,
    matches.updated_at desc, matches.id desc
  limit least(greatest(coalesce(p_limit, 40), 1), 100)
  offset greatest(coalesce(p_offset, 0), 0);
$$;

revoke all on function public.reconcile_monitor_offers(uuid) from public, anon, authenticated;
grant execute on function public.reconcile_monitor_offers(uuid) to service_role;
revoke all on function public.get_my_offer_matches(integer, integer, text, text, integer, text, text) from public, anon;
grant execute on function public.get_my_offer_matches(integer, integer, text, text, integer, text, text) to authenticated;

