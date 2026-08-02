-- Durable preference learning independent from monitor and offer retention.

create table if not exists public.user_preference_signals (
  user_id uuid not null references public.profiles(id) on delete cascade,
  dimension text not null check (dimension in ('airline', 'route', 'destination', 'duration', 'price')),
  value text not null,
  cabin text not null default '*',
  score integer not null default 0 check (score between -20 and 20),
  positive_count integer not null default 0 check (positive_count >= 0),
  negative_count integer not null default 0 check (negative_count >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, dimension, value, cabin)
);

create index if not exists user_preference_signals_user_idx
  on public.user_preference_signals(user_id, updated_at desc);

create or replace function public.preference_verdict_delta(verdict_value text, dimension_value text)
returns integer language sql immutable parallel safe as $$
  select case
    when verdict_value = 'buy' then 1
    when verdict_value = 'badairline' and dimension_value = 'airline' then -3
    when verdict_value = 'toolong' and dimension_value = 'duration' then -3
    when verdict_value = 'expensive' and dimension_value = 'price' then -3
    when verdict_value = 'skip' and dimension_value in ('route', 'destination') then -2
    else 0
  end;
$$;

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

drop trigger if exists capture_feedback_preference on public.feedback;
create trigger capture_feedback_preference
  after insert or update of verdict on public.feedback
  for each row execute procedure public.capture_feedback_preference();

-- Existing reactions become part of the durable profile immediately.
with feedback_context as (
  select feedback.user_id,
         feedback.verdict,
         upper(replace(offer.cabin, '-', '_')) as cabin,
         case
           when nullif(trim(offer.airline), '') is not null then upper(trim(offer.airline))
           else lower(regexp_replace(trim(offer.airline_name), '\s+', ' ', 'g'))
         end as airline,
         offer.route,
         upper(offer.destination) as destination,
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
  from public.feedback feedback
  join public.user_matches match on match.id = feedback.match_id and match.user_id = feedback.user_id
  join public.flight_offers offer on offer.id = match.offer_id
  join public.monitors monitor on monitor.id = match.monitor_id
), events as (
  select user_id, verdict, cabin, dimension, value
  from feedback_context
  cross join lateral (values
    ('airline'::text, airline::text),
    ('route'::text, route::text),
    ('destination'::text, destination::text),
    ('duration'::text, duration_bucket::text),
    ('price'::text, price_bucket::text)
  ) as signals(dimension, value)
  where nullif(value, '') is not null
), aggregated as (
  select user_id, dimension, value, cabin,
         greatest(-20, least(20, sum(public.preference_verdict_delta(verdict, dimension))))::integer as score,
         count(*) filter (where public.preference_verdict_delta(verdict, dimension) > 0)::integer as positive_count,
         count(*) filter (where public.preference_verdict_delta(verdict, dimension) < 0)::integer as negative_count
  from events
  group by user_id, dimension, value, cabin
)
insert into public.user_preference_signals(
  user_id, dimension, value, cabin, score, positive_count, negative_count, updated_at
)
select user_id, dimension, value, cabin, score, positive_count, negative_count, now()
from aggregated
where score <> 0 or positive_count <> 0 or negative_count <> 0
on conflict (user_id, dimension, value, cabin) do nothing;

-- Detailed past results are disposable now that their learning signal is durable.
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

  delete from public.user_matches match
  using public.flight_offers offer
  where match.offer_id = offer.id
    and offer.travel_date < current_date - 7;
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

alter table public.user_preference_signals enable row level security;
drop policy if exists preference_signals_owner_read on public.user_preference_signals;
create policy preference_signals_owner_read
  on public.user_preference_signals for select to authenticated
  using (public.is_active_user(user_id) and user_id = auth.uid());

revoke all on function public.preference_verdict_delta(text, text) from public, anon, authenticated;
revoke all on function public.preference_signal_score(text, integer, integer) from public, anon, authenticated;
revoke all on function public.capture_feedback_preference() from public, anon, authenticated;
