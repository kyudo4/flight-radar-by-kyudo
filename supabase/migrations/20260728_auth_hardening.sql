-- Ogranicza nadużycia endpointu Telegram Login. Tabela jest dostępna wyłącznie
-- dla service role używanej przez Edge Function; użytkownicy nie mają polityk.
create table if not exists public.telegram_auth_attempts (
  id bigint generated always as identity primary key,
  telegram_user_id text not null,
  attempted_at timestamptz not null default now()
);

create index if not exists telegram_auth_attempts_lookup_idx
  on public.telegram_auth_attempts(telegram_user_id, attempted_at desc);

alter table public.telegram_auth_attempts enable row level security;
revoke all on table public.telegram_auth_attempts from public, anon, authenticated;
