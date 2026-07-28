-- Flight Radar by Kyudo: hardening after the pre-login security audit.
-- Safe for the existing empty project and future installations.

alter function public.touch_updated_at() set search_path = public;

create or replace function public.validate_monitor_filters()
returns trigger language plpgsql set search_path = public as $$
declare
  from_date date;
  to_date date;
  origin_count integer;
  destination_count integer;
  budget numeric;
  duration numeric;
  stops integer;
  min_stars integer;
  drop_percent numeric;
begin
  if length(trim(coalesce(new.name, ''))) < 1 or length(new.name) > 120 then
    raise exception 'Nazwa monitora musi mieć od 1 do 120 znaków';
  end if;
  if jsonb_typeof(coalesce(new.filters -> 'origins', 'null'::jsonb)) <> 'array'
     or jsonb_typeof(coalesce(new.filters -> 'destinations', 'null'::jsonb)) <> 'array' then
    raise exception 'Lotniska muszą być zapisane jako listy';
  end if;
  from_date := (new.filters ->> 'from')::date;
  to_date := (new.filters ->> 'to')::date;
  origin_count := jsonb_array_length(new.filters -> 'origins');
  destination_count := jsonb_array_length(new.filters -> 'destinations');
  budget := coalesce((new.filters ->> 'budget_pln')::numeric, 0);
  duration := coalesce((new.filters ->> 'max_duration_h')::numeric, 24);
  stops := coalesce((new.filters ->> 'max_stops')::integer, 2);
  if origin_count < 1 or origin_count > 5 or destination_count < 1 or destination_count > 5 then
    raise exception 'Monitor może zawierać maksymalnie 5 lotnisk wylotu i 5 celów';
  end if;
  if exists (
    select 1 from jsonb_array_elements_text(new.filters -> 'origins') as x(value)
    where x.value !~ '^[A-Z]{3}$'
  ) or exists (
    select 1 from jsonb_array_elements_text(new.filters -> 'destinations') as x(value)
    where x.value !~ '^[A-Z]{3}$'
  ) then
    raise exception 'Kod lotniska musi mieć dokładnie trzy wielkie litery';
  end if;
  if from_date is null or to_date is null or to_date < from_date or to_date - from_date > 31 then
    raise exception 'Zakres dat monitora może mieć maksymalnie 32 dni';
  end if;
  if budget <= 0 or budget > 1000000 or duration <= 0 or duration > 24 or stops < 0 or stops > 9 then
    raise exception 'Nieprawidłowy limit czasu lub przesiadek';
  end if;
  if new.filters ? 'cabins' then
    if jsonb_typeof(new.filters -> 'cabins') <> 'array'
       or jsonb_array_length(new.filters -> 'cabins') < 1
       or jsonb_array_length(new.filters -> 'cabins') > 4
       or exists (
         select 1 from jsonb_array_elements_text(new.filters -> 'cabins') as x(value)
         where x.value not in ('BUSINESS', 'FIRST', 'PREMIUM_ECONOMY', 'ECONOMY')
       ) then
      raise exception 'Monitor musi mieć od 1 do 4 prawidłowych klas lotu';
    end if;
  elsif coalesce(new.filters ->> 'cabin', '') not in ('BUSINESS', 'FIRST', 'PREMIUM_ECONOMY', 'ECONOMY') then
    raise exception 'Nieprawidłowa klasa lotu';
  end if;
  min_stars := coalesce((new.telegram_rules ->> 'min_stars')::integer, 4);
  drop_percent := coalesce((new.telegram_rules ->> 'drop_percent')::numeric, 10);
  if min_stars < 3 or min_stars > 5 or drop_percent < 1 or drop_percent > 50 then
    raise exception 'Nieprawidłowe zasady alertów Telegram';
  end if;
  return new;
end;
$$;

drop trigger if exists validate_monitor_filters on public.monitors;
create trigger validate_monitor_filters
  before insert or update of name, filters, telegram_rules on public.monitors
  for each row execute procedure public.validate_monitor_filters();

revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.prevent_self_privilege_change() from public, anon, authenticated;
revoke execute on function public.enforce_monitor_limit() from public, anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;
revoke execute on function public.validate_monitor_filters() from public, anon, authenticated;

revoke execute on function public.is_admin() from public;
revoke execute on function public.is_active_user(uuid) from public;
revoke execute on function public.claim_invite(text) from public;
revoke execute on function public.create_invite(text, text) from public;
revoke execute on function public.set_profile_status(uuid, public.profile_status) from public;
revoke execute on function public.admin_delete_profile(uuid) from public;
revoke execute on function public.sync_telegram_connection(text, text) from public;
revoke execute on function public.is_admin() from anon;
revoke execute on function public.is_active_user(uuid) from anon;
revoke execute on function public.claim_invite(text) from anon;
revoke execute on function public.create_invite(text, text) from anon;
revoke execute on function public.set_profile_status(uuid, public.profile_status) from anon;
revoke execute on function public.admin_delete_profile(uuid) from anon;
revoke execute on function public.sync_telegram_connection(text, text) from anon;
grant execute on function public.is_admin() to authenticated;
grant execute on function public.is_active_user(uuid) to authenticated;
grant execute on function public.claim_invite(text) to authenticated;
grant execute on function public.create_invite(text, text) to authenticated;
grant execute on function public.set_profile_status(uuid, public.profile_status) to authenticated;
grant execute on function public.admin_delete_profile(uuid) to authenticated;
grant execute on function public.sync_telegram_connection(text, text) to authenticated;

alter policy profiles_self_or_admin on public.profiles to authenticated;
alter policy profiles_self_update on public.profiles to authenticated;
alter policy profiles_admin_delete on public.profiles to authenticated;
alter policy invites_admin_all on public.invites to authenticated;
alter policy monitors_owner_all on public.monitors to authenticated;
alter policy offers_matched_owner_read on public.flight_offers to authenticated;
alter policy matches_owner_read on public.user_matches to authenticated;
alter policy matches_owner_update on public.user_matches to authenticated;
alter policy connections_self_read on public.telegram_connections to authenticated;
alter policy connections_self_delete on public.telegram_connections to authenticated;
alter policy telegram_state_admin_read on public.telegram_state to authenticated;
alter policy scan_runs_admin_read on public.scan_runs to authenticated;
alter policy feedback_owner_all on public.feedback to authenticated;
