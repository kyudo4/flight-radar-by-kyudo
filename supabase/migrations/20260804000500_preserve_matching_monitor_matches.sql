-- Keep matches that still satisfy the edited monitor instead of hiding the
-- entire previous result set when one airport/date/class is removed.

create or replace function public.offer_matches_monitor_filters(
  p_offer_id uuid,
  p_filters jsonb
)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.flight_offers offer
    where offer.id = p_offer_id
      and offer.origin in (
        select value from jsonb_array_elements_text(
          coalesce(p_filters -> 'origins', '[]'::jsonb)
        ) as origins(value)
      )
      and offer.destination in (
        select value from jsonb_array_elements_text(
          coalesce(p_filters -> 'destinations', '[]'::jsonb)
        ) as destinations(value)
      )
      and offer.travel_date >= nullif(p_filters ->> 'from', '')::date
      and offer.travel_date <= nullif(p_filters ->> 'to', '')::date
      and offer.trip_type = coalesce(nullif(p_filters ->> 'trip_type', ''), 'one_way')
      and (
        offer.trip_type = 'one_way'
        or (
          offer.return_date is not null
          and offer.return_date >= nullif(p_filters ->> 'return_from', '')::date
          and offer.return_date <= nullif(p_filters ->> 'return_to', '')::date
        )
      )
      and upper(offer.cabin) in (
        select upper(value)
        from jsonb_array_elements_text(
          case
            when jsonb_typeof(p_filters -> 'cabins') = 'array'
              then p_filters -> 'cabins'
            else jsonb_build_array(p_filters ->> 'cabin')
          end
        ) as cabins(value)
      )
      and offer.price_pln is not null
      and offer.price_pln <= coalesce((p_filters ->> 'budget_pln')::numeric, 0)
      and (
        nullif(trim(p_filters ->> 'max_duration_h'), '') is null
        or offer.duration_minutes is not null
           and offer.duration_minutes <= (
             nullif(trim(p_filters ->> 'max_duration_h'), '')::numeric * 60
           )
      )
      and offer.stops is not null
      and offer.stops <= coalesce((p_filters ->> 'max_stops')::integer, 2)
      and (
        coalesce((p_filters ->> 'direct_only')::boolean, false) is false
        or offer.stops = 0
      )
      and not exists (
        select 1
        from jsonb_array_elements_text(
          coalesce(p_filters -> 'excluded_airlines', '[]'::jsonb)
        ) as excluded(value)
        where nullif(trim(excluded.value), '') is not null
          and (
            lower(coalesce(offer.airline_name, '')) like '%' || lower(trim(excluded.value)) || '%'
            or lower(coalesce(offer.airline, '')) = lower(trim(excluded.value))
          )
      )
  );
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
    set visible = false,
        telegram_eligible = false,
        updated_at = now()
    where matches.monitor_id = new.id
      and matches.visible
      and not public.offer_matches_monitor_filters(matches.offer_id, new.filters);
  end if;
  return new;
end;
$$;

revoke execute on function public.offer_matches_monitor_filters(uuid, jsonb) from public, anon, authenticated;
