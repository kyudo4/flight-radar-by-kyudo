-- Telegram alerts always include every offer that passes the monitor filters.
-- The old min_stars field remains for compatibility, but is normalized to 3
-- and is no longer used as an alert gate.

create or replace function public.normalize_telegram_rules()
returns trigger language plpgsql set search_path = public as $$
begin
  new.telegram_rules := jsonb_set(
    coalesce(new.telegram_rules, '{}'::jsonb),
    '{min_stars}',
    '3'::jsonb,
    true
  );
  return new;
end;
$$;

revoke all on function public.normalize_telegram_rules() from public, anon, authenticated;

drop trigger if exists normalize_telegram_rules on public.monitors;
create trigger normalize_telegram_rules
  before insert or update of telegram_rules on public.monitors
  for each row execute procedure public.normalize_telegram_rules();

update public.monitors
set telegram_rules = jsonb_set(
  coalesce(telegram_rules, '{}'::jsonb),
  '{min_stars}',
  '3'::jsonb,
  true
);
