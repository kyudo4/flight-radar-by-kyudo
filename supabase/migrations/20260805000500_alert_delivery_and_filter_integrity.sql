-- Durable Telegram delivery, accurate round-trip revalidation and safe
-- compatibility with all previously stored offers.

create table if not exists public.telegram_outbox (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  monitor_id uuid not null references public.monitors(id) on delete cascade,
  match_id uuid not null references public.user_matches(id) on delete cascade,
  chat_id text not null,
  dedupe_key text not null unique,
  price_pln integer not null check (price_pln > 0),
  generation bigint not null default 0,
  message_text text not null,
  reply_markup jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending', 'sending', 'retry', 'sent', 'dead')),
  attempts integer not null default 0 check (attempts >= 0),
  available_at timestamptz not null default now(),
  sent_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists telegram_outbox_delivery_idx
  on public.telegram_outbox(status, available_at, created_at);
create index if not exists telegram_outbox_user_idx
  on public.telegram_outbox(user_id, created_at desc);

alter table public.telegram_outbox enable row level security;
drop policy if exists telegram_outbox_admin_read on public.telegram_outbox;
create policy telegram_outbox_admin_read on public.telegram_outbox
  for select to authenticated using (public.is_admin());

create or replace function public.claim_telegram_outbox(p_limit integer default 50)
returns setof public.telegram_outbox
language plpgsql security definer set search_path = public as $$
begin
  return query
  with candidates as (
    select id
    from public.telegram_outbox
    where (
        status in ('pending', 'retry') and available_at <= now()
      ) or (
        status = 'sending' and updated_at <= now() - interval '15 minutes'
      )
    order by available_at asc, created_at asc, id asc
    limit greatest(1, least(coalesce(p_limit, 50), 200))
    for update skip locked
  )
  update public.telegram_outbox outbox
  set status = 'sending',
      attempts = outbox.attempts + 1,
      updated_at = now()
  from candidates
  where outbox.id = candidates.id
  returning outbox.*;
end;
$$;

create or replace function public.admin_delivery_summary()
returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'pending', count(*) filter (where status in ('pending', 'retry', 'sending')),
    'sent_24h', count(*) filter (where status = 'sent' and sent_at >= now() - interval '24 hours'),
    'failed', count(*) filter (where status = 'dead'),
    'last_sent_at', max(sent_at)
  )
  from public.telegram_outbox
  where public.is_admin();
$$;

revoke all on function public.claim_telegram_outbox(integer) from public, anon, authenticated;
grant execute on function public.claim_telegram_outbox(integer) to service_role;
revoke all on function public.admin_delivery_summary() from public, anon;
grant execute on function public.admin_delivery_summary() to authenticated;

-- Complete the Telegram delivery and the match notification marker in one
-- database transaction. This prevents a sent message from remaining marked
-- as unnotified when the second update fails halfway through.
create or replace function public.complete_telegram_outbox(p_outbox_id uuid)
returns boolean
language plpgsql security definer set search_path = public as $$
declare
  completed boolean := false;
begin
  update public.user_matches matches
  set notified_at = now(),
      last_notified_price = outbox.price_pln,
      notified_generation = outbox.generation,
      updated_at = now()
  from public.telegram_outbox outbox
  where outbox.id = p_outbox_id
    and outbox.status = 'sending'
    and matches.id = outbox.match_id;

  if not found then
    return false;
  end if;

  update public.telegram_outbox
  set status = 'sent',
      sent_at = now(),
      last_error = null,
      updated_at = now()
  where id = p_outbox_id
    and status = 'sending';
  completed := found;
  return completed;
end;
$$;

revoke all on function public.complete_telegram_outbox(uuid) from public, anon, authenticated;
grant execute on function public.complete_telegram_outbox(uuid) to service_role;

-- Re-evaluate both legs after a monitor edit.  Legacy one-way rows continue
-- to use duration_minutes/stops; round-trip rows require the raw outbound and
-- inbound values when a corresponding filter is set.
create or replace function public.offer_matches_monitor_filters(
  p_offer_id uuid,
  p_filters jsonb
)
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1
    from public.flight_offers offer
    cross join lateral (select coalesce(offer.raw ->> 'outbound_duration_h',
                                         case when offer.duration_minutes is not null
                                              then (offer.duration_minutes::numeric / 60)::text end) as outbound_duration,
                               coalesce(offer.raw ->> 'return_duration_h', '') as return_duration,
                               coalesce(offer.raw ->> 'outbound_stops', offer.stops::text) as outbound_stops,
                               coalesce(offer.raw ->> 'return_stops', '') as return_stops) legs
    where offer.id = p_offer_id
      and offer.origin in (select value from jsonb_array_elements_text(coalesce(p_filters -> 'origins', '[]'::jsonb)) as origins(value))
      and offer.destination in (select value from jsonb_array_elements_text(coalesce(p_filters -> 'destinations', '[]'::jsonb)) as destinations(value))
      and offer.travel_date >= nullif(p_filters ->> 'from', '')::date
      and offer.travel_date <= nullif(p_filters ->> 'to', '')::date
      and offer.trip_type = coalesce(nullif(p_filters ->> 'trip_type', ''), 'one_way')
      and (offer.trip_type = 'one_way' or (offer.return_date is not null
        and offer.return_date >= nullif(p_filters ->> 'return_from', '')::date
        and offer.return_date <= nullif(p_filters ->> 'return_to', '')::date))
      and upper(offer.cabin) in (
        select upper(value) from jsonb_array_elements_text(
          case when jsonb_typeof(p_filters -> 'cabins') = 'array'
               then p_filters -> 'cabins' else jsonb_build_array(p_filters ->> 'cabin') end
        ) as cabins(value)
      )
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((p_filters ->> 'budget_pln')::numeric, 0)
      and (nullif(trim(p_filters ->> 'max_duration_h'), '') is null
        or (legs.outbound_duration <> '' and legs.outbound_duration::numeric <= nullif(trim(p_filters ->> 'max_duration_h'), '')::numeric)
        and (offer.trip_type = 'one_way' or (legs.return_duration <> '' and legs.return_duration::numeric <= nullif(trim(p_filters ->> 'max_duration_h'), '')::numeric)))
      and (nullif(trim(p_filters ->> 'max_stops'), '') is null
        or (legs.outbound_stops <> '' and legs.outbound_stops::integer <= coalesce((p_filters ->> 'max_stops')::integer, 2))
        and (offer.trip_type = 'one_way' or (legs.return_stops <> '' and legs.return_stops::integer <= coalesce((p_filters ->> 'max_stops')::integer, 2))))
      and (coalesce((p_filters ->> 'direct_only')::boolean, false) is false
        or (legs.outbound_stops = '0' and (offer.trip_type = 'one_way' or legs.return_stops = '0')))
      and not exists (
        select 1 from jsonb_array_elements_text(coalesce(p_filters -> 'excluded_airlines', '[]'::jsonb)) as excluded(value)
        where nullif(trim(excluded.value), '') is not null
          and (lower(coalesce(offer.airline_name, '')) like '%' || lower(trim(excluded.value)) || '%'
            or lower(coalesce(offer.airline, '')) = lower(trim(excluded.value)))
      )
  );
$$;

revoke execute on function public.offer_matches_monitor_filters(uuid, jsonb) from public, anon, authenticated;
