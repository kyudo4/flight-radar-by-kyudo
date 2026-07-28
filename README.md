# Flight Radar by Kyudo

Zamknięta wersja pustego monitora lotów dla maksymalnie 10 zaproszonych osób.

Projekt jest niezależny od prywatnej aplikacji w `Apki/flight-radar`. Nie kopiuje
jej stanu, tokenów Telegrama ani historii alertów.

## Architektura

- `site/` — statyczny panel GitHub Pages (HTML/CSS/JS);
- `supabase/schema.sql` — prywatna baza użytkowników, monitorów, ofert i alertów;
- `monitor_scan_items` — trwała kolejka każdej konkretnej trasy, daty i klasy;
- `scanner/` — skaner Python uruchamiany przez GitHub Actions;
- `.github/workflows/scan.yml` — wspólny skan co 3 godziny;
- Google Flights — wspólny kolektor z kontrolowanym limitem zapytań i retry dla przejściowych błędów;
- RSS źródeł promocyjnych — Secret Flying, Fly4Free, LoyaltyLobby, OMAAT, Travel Dealz, View From The Wing, FlyerTalk i Reddit;
- Telegram OIDC — jedyne logowanie i jednocześnie kanał alertów.

## Uruchomienie

1. Utwórz projekt Supabase i wykonaj `supabase/schema.sql`. Jeśli baza już działała
   na wcześniejszej wersji, wykonaj dodatkowo `supabase/migrations/20260728_hardening.sql`.
2. W Supabase Auth dodaj własnego providera OIDC `custom:telegram`:
   issuer `https://oauth.telegram.org`, zakresy `openid profile telegram:bot_access`,
   `email_optional = true`. Client ID i secret pobierz z BotFather.
   Client ID/secret muszą pochodzić z tego samego bota, którego token ustawisz
   później jako `TG_BOT_TOKEN`.
3. W BotFather dodaj jako Allowed URLs domenę panelu oraz callback URL pokazany
   przez Supabase dla providera `custom:telegram`.
4. Ustaw adres projektu i anon key w `site/config.js` na podstawie `site/config.example.js`.
5. Ustaw sekrety GitHub Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_ANON_KEY` i `TG_BOT_TOKEN`.
6. Zaloguj się pierwszy raz przez Telegram, odczytaj `telegram_user_id` z
   `public.profiles` i wykonaj `supabase/bootstrap-admin.sql` po jego wpisaniu.
7. Włącz GitHub Pages przez workflow `Publish Flight Radar by Kyudo dashboard`.
8. Włącz workflow `Flight Radar by Kyudo scan`.

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
- preferowane linie są ustawiane osobno przez użytkownika i wpływają na ocenę,
  a wykluczone linie są odrzucane przed zapisem;
- panel ma dodatkowe lokalne filtrowanie wyników po trasie/linii, klasie,
  minimalnej ocenie oraz sortowanie po cenie, ocenie i świeżości;
- zapisanie lub wznowienie zmienionego monitora ustawia jego kolejkę do sprawdzenia
  w najbliższym przebiegu, zamiast czekać na poprzedni termin;
- każda osoba ma własne monitory, dopasowania, feedback i połączenie Telegrama.
- nowa osoba zaczyna z pustą kartą; skaner nie wykonuje żadnych wyszukiwań, dopóki
  użytkownik nie zapisze własnych filtrów.

## Zasady bezpieczeństwa

- `SUPABASE_SERVICE_ROLE_KEY` i token Telegrama są wyłącznie sekretami workflow;
- `site/config.js` może zawierać tylko publiczny anon key Supabase;
- dane użytkowników chronią polityki RLS;
- nie commituj `site/config.js` ani żadnego pliku `.env`;
- po usunięciu użytkownika jego prywatne dane są kasowane, a miejsce wraca do puli.

## Limit skanera

Scheduler deduplikuje identyczne zapytania użytkowników. W jednym przebiegu
wybiera domyślnie maksymalnie 60 zapytań standardowych i 4 First. Pozycje kolejki
są wybierane rotacyjnie między monitorami, żeby jeden użytkownik nie zablokował
pozostałych. W puli standardowej Business, Economy i Premium Economy również
rotują między sobą; nie ma sztywnego limitu Economy, więc niewykorzystane miejsca
przechodzą do klas, które mają zadania. Identyczne zadania są wykonywane tylko raz.
Kolejka jest stronicowana,
więc duża liczba monitorów nie ucina jej po pierwszych 20 000 rekordów.

Limity można zmienić zmiennymi środowiskowymi `MAX_STANDARD_QUERIES`,
`MAX_FIRST_QUERIES` i `GOOGLE_REQUEST_DELAY_SECONDS`. Domyślne 64 zapytania na
przebieg co 3 godziny oznaczają około 2,5 dnia na pełny obieg monitora
10 × 9 × 14 bez First; przy wielu różnych monitorach czas rośnie proporcjonalnie.
Nie należy usuwać opóźnienia ani zwiększać limitów bez obserwacji blokad Google.

## Testy lokalne

Uruchom `python -m unittest discover -s tests -v`. Ten sam zestaw jest
wykonywany w GitHub Actions przed każdym skanem.
