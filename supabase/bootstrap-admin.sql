-- Uruchom po utworzeniu pierwszego konta w Supabase Auth.
-- Zmień adres na swój e-mail.
update public.profiles
set role = 'admin', status = 'active', display_name = 'Administrator'
where lower(email) = lower('TU_WPISZ_SWOJ_EMAIL');
