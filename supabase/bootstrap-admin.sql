-- Uruchom po pierwszym logowaniu przez Telegram.
-- Wpisz swój identyfikator Telegrama z tabeli public.profiles.
update public.profiles
set role = 'admin', status = 'active', display_name = 'Administrator'
where telegram_user_id = 'TU_WPISZ_SWÓJ_TELEGRAM_ID';
