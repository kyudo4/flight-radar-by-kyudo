# Flight Radar by Kyudo

Zamknięta wersja pustego monitora lotów dla maksymalnie 10 zaproszonych osób.

Projekt jest niezależny od prywatnej aplikacji w `Apki/flight-radar`. Nie kopiuje
jej stanu, tokenów Telegrama ani historii alertów.

## Architektura

- `site/` — statyczny panel GitHub Pages (HTML/CSS/JS);
- `supabase/schema.sql` — prywatna baza użytkowników, monitorów, ofert i alertów;
- `monitor_scan_items` — trwała kolejka każdej konkretnej trasy, daty, klasy i —
  dla podróży tam i z powrotem — pary wylot/powrót;
- `scanner/` — skaner Python uruchamiany przez GitHub Actions;
- `.github/workflows/scan.yml` — wspólny skan 4 razy na dobę (co 6 godzin);
- `.github/workflows/telegram-feedback.yml` — odbiór reakcji z Telegrama co 5 minut;
- Google Flights — wspólny kolektor z kontrolowanym limitem zapytań i retry dla przejściowych błędów;
- RSS źródeł promocyjnych — Secret Flying, Fly4Free, LoyaltyLobby, OMAAT, Travel Dealz, View From The Wing, FlyerTalk i Reddit;
- Telegram Login — jedyne logowanie i jednocześnie kanał alertów. Panel korzysta
  z podpisanego `id_token`, a funkcja `supabase/functions/telegram-auth` weryfikuje
  go po stronie serwera i dopiero wtedy tworzy sesję Supabase.

## Uruchomienie

1. Utwórz projekt Supabase i wykonaj `supabase/schema.sql`. Jeśli baza już działała
   na wcześniejszej wersji, wykonaj dodatkowo migracje z katalogu `supabase/migrations/`
   w kolejności dat, w tym `20260728_offer_read_policy.sql`, `20260728_auth_hardening.sql`
   oraz najnowsze migracje `20260802000300_quality_history_mutes.sql` i
   `20260802000400_reliability_retention.sql` i
   `20260802000500_round_trip_retention_telegram.sql` oraz
   `20260802000600_durable_preferences.sql` i
   `20260802000700_preference_integrity.sql`.
2. W BotFather > Bot Settings > Web Login dodaj domenę panelu jako Allowed Origin
   i pozostaw Client ID/Secret dla tego samego bota.
3. W Supabase Edge Functions utwórz funkcję `telegram-auth` z pliku
   `supabase/functions/telegram-auth/index.ts`, wyłącz jej legacy JWT verification
   (funkcja sama sprawdza podpisany token Telegrama), a następnie dodaj sekrety
   `TELEGRAM_CLIENT_ID` i `TELEGRAM_CLIENT_SECRET`. Sekret klienta nie trafia do strony.
   Funkcja ogranicza żądania do `https://kyudo4.github.io`; przy zmianie domeny ustaw
   sekret `APP_ORIGIN` na nowy origin bez ścieżki.
   Utwórz również funkcję `admin-scan` z pliku `supabase/functions/admin-scan/index.ts`.
   Ustaw w niej sekrety `GITHUB_ACTIONS_TOKEN` (fine-grained token GitHub tylko dla tego
   repozytorium, uprawnienie Actions: Read and write), `GITHUB_REPOSITORY` oraz
   `GITHUB_WORKFLOW_ID=scan.yml`. Token GitHub zostaje wyłącznie w Supabase i nie trafia
   do strony. Przycisk ręcznego skanu jest dostępny tylko aktywnemu administratorowi.
   Po wdrożeniu nowych plików ponownie opublikuj funkcję `admin-scan`; przed zastosowaniem
   najnowszej migracji funkcja ma kompatybilny tryb przejściowy.
4. Ustaw adres projektu i anon key w `site/config.js` na podstawie `site/config.example.js`.
5. Ustaw sekrety GitHub Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_ANON_KEY` i `TG_BOT_TOKEN`.
6. Zaloguj się pierwszy raz przez Telegram, odczytaj `telegram_user_id` z
   `public.profiles` i wykonaj `supabase/bootstrap-admin.sql` po jego wpisaniu.
7. Włącz GitHub Pages przez workflow `Publish Flight Radar by Kyudo dashboard`.
8. Włącz workflow `Flight Radar by Kyudo scan`.
9. Workflow `Flight Radar Telegram feedback` odbiera reakcje z przycisków maksymalnie
   po około 5 minutach. `Flight Radar Telegram smoke test` służy wyłącznie do ręcznego
   sprawdzenia dostarczenia wiadomości administratorowi.
10. Workflow `Flight Radar retention cleanup` uruchamia cotygodniowe sprzątanie historii
    cen, wygasłych monitorów, starych skanów i zaproszeń.

Do działania skanera potrzebny jest Python 3.11 i zależność `fast-flights`.
Wyniki i ustawienia nie są zapisywane w repozytorium.

## Zachowanie ofert

- panel pokazuje tylko dokładne daty z monitoringu;
- monitor może działać w trybie „w jedną stronę” albo „tam i z powrotem”. W drugim
  trybie można podać osobny zakres dat powrotu; Google dostaje oba odcinki w jednym
  zapytaniu, a system odrzuca powrót wcześniejszy lub tego samego dnia. Czas i liczbę
  przesiadek sprawdza osobno dla wylotu oraz powrotu. Wynik round-trip bez
  potwierdzonych szczegółów powrotu nie przechodzi filtra jakości i nie wywołuje alertu;
- maksymalny czas i liczba przesiadek są sprawdzane przed zapisem dopasowania;
- drugi termin tej samej trasy i linii nie trafia do panelu ani Telegrama, jeśli
  nie jest tańszy od wcześniejszej ceny; nowa linia jest traktowana jako nowe
  dopasowanie;
- ponowny alert tej samej oferty wymaga spadku ceny o próg zapisany przez
  użytkownika (domyślnie 10%);
- cena jest zapisywana w historii, a oferta bez potwierdzenia przez 24 godziny
  dostaje status „Cena niepotwierdzona”; panel domyślnie pokazuje tylko ceny aktualne;
- historia ceny zapisuje tylko zmiany, panel pobiera ograniczoną liczbę ostatnich punktów,
  a cotygodniowy workflow usuwa obserwacje starsze niż 30 dni i zostawia stan minimum
  ceny oraz ostatniego alertu potrzebny do blokowania duplikatów;
- reakcje z panelu i Telegrama są agregowane w trwały profil preferencji użytkownika:
  osobno dla linii, trasy, celu, klasy, czasu podróży i relacji ceny do budżetu;
  profil działa również w nowych monitorach i po zmianie dat;
- po upływie tygodnia od daty wylotu sprzątane są szczegóły starych dopasowań,
  ofert i źródłowych reakcji, ale ich zagregowany sygnał pozostaje w profilu;
  usunięcie monitora również nie kasuje wyuczonych preferencji;
- preferowane linie są ustawiane osobno przez użytkownika i wpływają na ocenę,
  a wykluczone linie są odrzucane przed zapisem;
- panel ma dodatkowe lokalne filtrowanie wyników po trasie/linii, klasie,
  minimalnej ocenie oraz sortowanie po cenie, ocenie i świeżości;
- panel pobiera oferty stronami po 40 i pozwala wczytać całą historię;
- zapisanie lub wznowienie zmienionego monitora ustawia jego kolejkę do sprawdzenia
  w najbliższym przebiegu, zamiast czekać na poprzedni termin;
- każda osoba ma własne monitory, dopasowania, feedback i połączenie Telegrama.
- nowa osoba zaczyna z pustą kartą; skaner nie wykonuje żadnych wyszukiwań, dopóki
  użytkownik nie zapisze własnych filtrów.
- pojedynczy monitoring przyjmuje maksymalnie 5 lotnisk wylotu i 5 celów; limit jest
  sprawdzany w panelu, bazie oraz skanerze.
- panel i skaner sprawdzają kody względem globalnej listy IATA wygenerowanej z
  publicznego zbioru OurAirports (`site/airports.json`). Aktualizacja listy:
  `python scripts/generate_airports.py airports.csv site/airports.json`.
- jeden monitoring może obejmować kilka klas jednocześnie, np. Business i First;
  każda klasa dostaje osobne zadania skanera.
- jeden monitoring może wygenerować maksymalnie 5000 kombinacji. Panel pokazuje
  wyliczenie przed zapisem, a baza i skaner stosują ten sam limit.

## Zasady bezpieczeństwa

- `SUPABASE_SERVICE_ROLE_KEY` i token Telegrama są wyłącznie sekretami workflow;
- `site/config.js` może zawierać tylko publiczny anon key Supabase;
- dane użytkowników chronią polityki RLS;
- nie commituj `site/config.js` ani żadnego pliku `.env`;
- po usunięciu użytkownika jego prywatne dane są kasowane, a miejsce wraca do puli.

## Limit skanera

Scheduler deduplikuje identyczne zapytania użytkowników. Cztery przebiegi na dobę
startują od maksymalnie 240 zapytań standardowych i 12 First na przebieg. Pozycje kolejki
są wybierane rotacyjnie między monitorami, żeby jeden użytkownik nie zablokował
pozostałych. W puli standardowej Business, Economy i Premium Economy również
rotują między sobą; nie ma sztywnego limitu Economy, więc niewykorzystane miejsca
przechodzą do klas, które mają zadania. Identyczne zadania są wykonywane tylko raz.
Kolejka jest stronicowana,
więc duża liczba monitorów nie ucina jej po pierwszych 20 000 rekordów.

Po trzech zdrowych przebiegach limit automatycznie rośnie o 40 standardowych i 2 First,
aż do 400 + 24. Po pierwszym 403/429/503, CAPTCHA albo consent wall bieżący przebieg
kończy się natychmiast, a kolejny schodzi co najmniej o połowę (nie ma bezmyślnego
ponawiania zablokowanego żądania). Historia limitów i blokad jest zapisana w `scan_runs`.
Opóźnienie między zapytaniami pozostaje włączone. Wartości można zmienić przez
`MAX_STANDARD_QUERIES`, `MAX_FIRST_QUERIES`, `MAX_STANDARD_CEILING`,
`MAX_FIRST_CEILING`, `QUERY_RAMP_STANDARD_STEP`, `QUERY_RAMP_FIRST_STEP` oraz
`GOOGLE_REQUEST_DELAY_SECONDS`.

Odpowiedź Google `409` jest traktowana jak blokada już przy pierwszym żądaniu.
Trzy kolejne błędy struktury danych zatrzymują kolektor z osobnym statusem `error`,
żeby nie mylić zmiany formatu źródła z CAPTCHA lub ograniczeniem ruchu. Skaner nie
ocenia ani nie wysyła ofert, jeżeli nie może odczytać profilu preferencji użytkownika.

## Testy lokalne

Uruchom `python -m unittest discover -s tests -v`. GitHub Actions wykonuje ten sam
zestaw oraz osobny kontrakt na prawdziwym PostgreSQL, obejmujący reakcje, zmianę
werdyktu, retencję i zachowanie uczenia po usunięciu monitora.
