-- Quality controls, price history, mutes and scanner observability.
-- Additive migration for projects that already applied earlier migrations.

alter table public.flight_offers
  add column if not exists verification_status text not null default 'verified',
  add column if not exists verification_note text not null default '';

alter table public.flight_offers
  drop constraint if exists flight_offers_verification_status_check;
alter table public.flight_offers
  add constraint flight_offers_verification_status_check
  check (verification_status in ('verified', 'pending_return', 'pending_verification', 'stale'));

create table if not exists public.offer_price_history (
  id bigint generated always as identity primary key,
  offer_id uuid not null references public.flight_offers(id) on delete cascade,
  price_pln integer not null check (price_pln > 0),
  observed_at timestamptz not null default now()
);

create table if not exists public.offer_mutes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  kind text not null check (kind in ('offer', 'airline', 'route')),
  value text not null,
  label text not null default '',
  created_at timestamptz not null default now(),
  unique (user_id, kind, value)
);

alter table public.offer_mutes add column if not exists label text not null default '';

create index if not exists offer_price_history_offer_idx
  on public.offer_price_history(offer_id, observed_at desc);
create index if not exists offer_mutes_user_idx
  on public.offer_mutes(user_id, kind, value);

alter table public.offer_price_history enable row level security;
alter table public.offer_mutes enable row level security;

drop policy if exists offer_price_history_owner_read on public.offer_price_history;
create policy offer_price_history_owner_read on public.offer_price_history
for select to authenticated using (public.can_read_flight_offer(offer_id));

drop policy if exists offer_mutes_owner_all on public.offer_mutes;
create policy offer_mutes_owner_all on public.offer_mutes
for all to authenticated
using (public.is_active_user(user_id) and user_id = auth.uid())
with check (public.is_active_user(user_id) and user_id = auth.uid());
