-- Search saved offers by a city name or IATA code before pagination.
-- The frontend resolves a city query through the shared airport directory and
-- passes the matching codes here, so a search such as "Tokio" finds HND/NRT
-- just like a direct "NRT" search.

alter table public.flight_offers
  add column if not exists verification_status text not null default 'verified',
  add column if not exists verification_note text not null default '';

create or replace function public.get_my_offer_matches(
  p_limit integer default 40,
  p_offset integer default 0,
  p_query text default '',
  p_cabin text default '',
  p_min_stars integer default 0,
  p_freshness text default 'fresh',
  p_sort text default 'newest',
  p_airport_codes text[] default '{}'::text[]
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
      or upper(offer.origin) = any(coalesce(p_airport_codes, '{}'::text[]))
      or upper(offer.destination) = any(coalesce(p_airport_codes, '{}'::text[]))
    )
    and (nullif(trim(coalesce(p_cabin, '')), '') is null or upper(offer.cabin) = upper(trim(p_cabin)))
    and matches.stars >= greatest(coalesce(p_min_stars, 0), 0)
    and (
      coalesce(p_freshness, 'fresh') = 'all'
      or (p_freshness = 'fresh' and offer.verification_status = 'verified')
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

revoke all on function public.get_my_offer_matches(integer, integer, text, text, integer, text, text, text[]) from public, anon;
grant execute on function public.get_my_offer_matches(integer, integer, text, text, integer, text, text, text[]) to authenticated;
