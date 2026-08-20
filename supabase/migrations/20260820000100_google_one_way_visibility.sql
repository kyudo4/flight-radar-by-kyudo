-- A one-way fare visible in Google Flights is already sufficiently verified
-- for this application's policy. The purchase-link check is only relevant to
-- round trips, where the return leg must also be confirmed.
--
-- The earlier purchase-verification migration moved historical one-way Google
-- fares to pending_verification because their raw payload did not contain a
-- rendered payment link. That made valid fares disappear from the default
-- "current prices" view even though they were present in Google Flights.

update public.flight_offers
set verification_status = 'verified',
    verification_note = '',
    tags = (
      select coalesce(jsonb_agg(tag), '[]'::jsonb)
      from jsonb_array_elements(coalesce(public.flight_offers.tags, '[]'::jsonb)) as tag
      where tag <> to_jsonb('Do potwierdzenia'::text)
    )
where trip_type = 'one_way'
  and lower(coalesce(source, '')) like '%google%'
  and verification_status = 'pending_verification';
