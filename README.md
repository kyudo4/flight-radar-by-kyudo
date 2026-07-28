# Asia Flight Radar — Friends

Zamknięta wersja monitora lotów dla maksymalnie 10 zaproszonych osób.

Projekt jest niezależny od prywatnej aplikacji w `Apki/flight-radar`. Nie kopiuje
jej stanu, tokenów Telegrama ani historii alertów.

## Architektura

- `site/` — statyczny panel GitHub Pages (HTML/CSS/JS);
- `supabase/schema.sql` — prywatna baza użytkowników, monitorów, ofert i alertów;
- `scanner/` — skaner Python uruchamiany przez GitHub Actions;
- `.github/workflows/scan.yml` — wspólny skan co 3 godziny;
- Google Flights — wspólny kolektor, maksymalnie 30 zapytań standardowych + 2 First na przebieg;
- Telegram — indywidualne połączenie każdego użytkownika.

## Uruchomienie

1. Utwórz projekt Supabase i wykonaj `supabase/schema.sql`.
2. Ustaw adres projektu i anon key w `site/config.js` na podstawie `site/config.example.js`.
3. Ustaw sekrety GitHub Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `TG_BOT_TOKEN`, `TG_BOT_USERNAME`.
4. Utwórz pierwsze konto w Supabase Auth i nadaj mu rolę `admin` w tabeli
   `profiles`.
5. Opublikuj zawartość `site/` przez GitHub Pages.
6. Włącz workflow `Friends flight scan`.

Do działania skanera potrzebny jest Python 3.11 i zależność `fast-flights`.
Wyniki i ustawienia nie są zapisywane w repozytorium.

## Zasady bezpieczeństwa

- `SUPABASE_SERVICE_ROLE_KEY` i token Telegrama są wyłącznie sekretami workflow;
- `site/config.js` może zawierać tylko publiczny anon key Supabase;
- dane użytkowników chronią polityki RLS;
- nie commituj `site/config.js` ani żadnego pliku `.env`;
- po usunięciu użytkownika jego prywatne dane są kasowane, a miejsce wraca do puli.

## Limit skanera

Scheduler deduplikuje identyczne zapytania użytkowników. W jednym przebiegu
wybiera maksymalnie 30 zapytań standardowych i 2 First, zachowując kolejność
fair-use oraz priorytety dla konkretnych dat, świeżych spadków i ofert blisko
progu Telegrama.
