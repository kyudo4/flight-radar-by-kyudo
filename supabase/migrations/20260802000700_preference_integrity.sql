-- Keep learned scores mathematically stable after saturation and verdict edits.

create or replace function public.preference_signal_score(
  dimension_value text, positive_value integer, negative_value integer
)
returns integer language sql immutable parallel safe as $$
  select greatest(-20, least(20,
    greatest(0, coalesce(positive_value, 0)) -
    greatest(0, coalesce(negative_value, 0)) *
      case when dimension_value in ('airline', 'duration', 'price') then 3 else 2 end
  ))::integer;
$$;

create or replace function public.capture_feedback_preference()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  context_row record;
  signal_row record;
  old_delta integer;
  new_delta integer;
  score_delta integer;
  positive_delta integer;
  negative_delta integer;
begin
  select match.user_id,
         offer.route,
         upper(offer.destination) as destination,
         upper(replace(offer.cabin, '-', '_')) as cabin,
         case
           when nullif(trim(offer.airline), '') is not null then upper(trim(offer.airline))
           else lower(regexp_replace(trim(offer.airline_name), '\s+', ' ', 'g'))
         end as airline,
         case when greatest(
                    coalesce(offer.duration_minutes, 0),
                    coalesce((offer.raw ->> 'return_duration_h')::numeric * 60, 0)
                  ) > 0
              then (ceil(greatest(
                     coalesce(offer.duration_minutes, 0),
                     coalesce((offer.raw ->> 'return_duration_h')::numeric * 60, 0)
                   ) / 120) * 2)::integer end as duration_bucket,
         case when offer.price_pln > 0 and nullif((monitor.filters ->> 'budget_pln')::numeric, 0) is not null
              then greatest(10, least(200,
                   (ceil((offer.price_pln::numeric / (monitor.filters ->> 'budget_pln')::numeric) * 10) * 10)::integer))
              end as price_bucket
  into context_row
  from public.user_matches match
  join public.flight_offers offer on offer.id = match.offer_id
  join public.monitors monitor on monitor.id = match.monitor_id
  where match.id = new.match_id;

  if context_row.user_id is null or context_row.user_id <> new.user_id then
    raise exception 'Feedback does not belong to this user';
  end if;

  for signal_row in
    select dimension, value
    from (values
      ('airline'::text, context_row.airline::text),
      ('route'::text, context_row.route::text),
      ('destination'::text, context_row.destination::text),
      ('duration'::text, context_row.duration_bucket::text),
      ('price'::text, context_row.price_bucket::text)
    ) as signals(dimension, value)
    where nullif(value, '') is not null
  loop
    new_delta := public.preference_verdict_delta(new.verdict, signal_row.dimension);
    old_delta := case when tg_op = 'UPDATE'
                      then public.preference_verdict_delta(old.verdict, signal_row.dimension)
                      else 0 end;
    score_delta := new_delta - old_delta;
    positive_delta := (case when new_delta > 0 then 1 else 0 end)
                    - (case when old_delta > 0 then 1 else 0 end);
    negative_delta := (case when new_delta < 0 then 1 else 0 end)
                    - (case when old_delta < 0 then 1 else 0 end);

    if score_delta <> 0 or positive_delta <> 0 or negative_delta <> 0 then
      insert into public.user_preference_signals(
        user_id, dimension, value, cabin, score, positive_count, negative_count, updated_at
      ) values (
        new.user_id, signal_row.dimension, signal_row.value, context_row.cabin,
        public.preference_signal_score(
          signal_row.dimension, greatest(0, positive_delta), greatest(0, negative_delta)
        ),
        greatest(0, positive_delta), greatest(0, negative_delta), now()
      )
      on conflict (user_id, dimension, value, cabin) do update set
        score = public.preference_signal_score(
          signal_row.dimension,
          greatest(0, public.user_preference_signals.positive_count + positive_delta),
          greatest(0, public.user_preference_signals.negative_count + negative_delta)
        ),
        positive_count = greatest(0, public.user_preference_signals.positive_count + positive_delta),
        negative_count = greatest(0, public.user_preference_signals.negative_count + negative_delta),
        updated_at = now();
    end if;
  end loop;
  return new;
end;
$$;

-- Repair any score that reached a clamp before this migration.
update public.user_preference_signals
set score = public.preference_signal_score(dimension, positive_count, negative_count),
    updated_at = now()
where score is distinct from public.preference_signal_score(dimension, positive_count, negative_count);

revoke all on function public.preference_signal_score(text, integer, integer) from public, anon, authenticated;
revoke all on function public.capture_feedback_preference() from public, anon, authenticated;
