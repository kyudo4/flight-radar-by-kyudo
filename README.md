# Flight Radar by Kyudo

Zamknięta wersja monitora lotów dla maksymalnie 10 zaproszonych osób.

Projekt jest niezależny od prywatnej aplikacji w `Apki/flight-radar`. Nie kopiuje
jej stanu, tokenów Telegrama ani historii alertów.

## Architektura

- `site/` — statyczny panel GitHub Pages (HTML/CSS/JS);
- `supabase/schema.sql` — prywatna baza użytkowników, monitorów, ofert i alertów;
- `scanner/` — skaner Python uruchamiany przez GitHub Actions;
- `.github/workflows/scan.yml` — wspólny skan co 3 godziny;
- Google Flights — wspólny kolektor, maksymalnie 30 zapytań standardowych + 2 First na przebieg;
- RSS źródeł promocyjnych — Secret Flying, Fly4Free, LoyaltyLobby, OMAAT, Travel Dealz, View From The Wing, FlyerTalk i Reddit;
- Telegram — indywidualne połączenie każdego użytkownika.

## Uruchomienie

1. Utwórz projekt Supabase i wykonaj `supabase/schema.sql`.
2. Ustaw adres projektu i anon key w `site/config.js` na podstawie `site/config.example.js`.
3. Ustaw sekrety GitHub Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_ANON_KEY`, `TG_BOT_TOKEN`, `TG_BOT_USERNAME`.
4. Utwórz pierwsze konto w Supabase Auth i wykonaj `supabase/bootstrap-admin.sql`
   po wpisaniu swojego e-maila.
5. Włącz GitHub Pages przez workflow `Publish Flight Radar by Kyudo dashboard`.
6. Włącz workflow `Flight Radar by Kyudo scan`.

Do działania skanera potrzebny jest Python 3.11 i zależność `fast-flights`.
Wyniki i ustawienia nie są zapisywane w repozytorium.

## Zachowanie ofert

- panel pokazuje tylko dokładne daty z monitoringu;
- maksymalny czas i liczba przesiadek są sprawdzane przed zapisem dopasowania;
- drugi termin tej samej trasy i linii nie trafia do panelu ani Telegrama, jeśli
  nie jest tańszy od wcześniejszej ceny; nowa linia jest traktowana jako nowe
  dopasowanie;
- ponowny alert tej samej oferty wymaga spadku ceny o próg zapisany przez
  użytkownika (domyślnie 10%);
- każda osoba ma własne monitory, dopasowania, feedback i połączenie Telegrama.

## Zasady bezpieczeństwa

- `SUPABASE_SERVICE_ROLE_KEY` i token Telegrama są wyłącznie sekretami workflow;
- `site/config.js` może zawierać tylko publiczny anon key Supabase;
- dane użytkowników chronią polityki RLS;
- nie commituj `site/config.js` ani żadnego pliku `.env`;
- po usunięciu użytkownika jego prywatne dane są kasowane, a miejsce wraca do puli.

## Limit skanera

Scheduler deduplikuje identyczne zapytania użytkowników. W jednym przebiegu
wybiera maksymalnie 30 zapytań standardowych i 2 First. Kolejka zaczyna od
najdawniej sprawdzanych monitorów, żeby limit był dzielony fair-use.
