-- Do not present search results or promotion articles as confirmed purchase links.
-- Only the rendered itinerary picker may write purchase_link_verified=true in raw.

alter table public.flight_offers
  drop constraint if exists flight_offers_verification_status_check;
alter table public.flight_offers
  add constraint flight_offers_verification_status_check
  check (verification_status in ('verified', 'pending_return', 'pending_verification', 'stale'));

update public.flight_offers
set verification_status = 'pending_verification',
    verification_note = 'Nie potwierdzono jeszcze przejścia do dokładnej strony rezerwacji'
where verification_status = 'verified'
  and coalesce(raw ->> 'purchase_link_verified', '') <> 'true';

create index if not exists flight_offers_verification_status_idx
  on public.flight_offers(verification_status, last_seen_at desc);
