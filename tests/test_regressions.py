import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scanner"))

import friends_scanner as scanner
import gflights
import rss
import telegram_io


class FlightRadarRegressionTests(unittest.TestCase):
    def test_full_monitor_is_materialized_per_route_and_date(self):
        monitor = {
            "id": "monitor-1",
            "filters": {
                "origins": ["POZ", "WAW"],
                "destinations": ["BKK", "SIN"],
                "from": "2026-09-01",
                "to": "2026-09-14",
                "cabin": "BUSINESS",
            },
        }
        items = scanner.monitor_combinations(monitor)
        self.assertEqual(len(items), 56)
        self.assertEqual(len({(x["origin"], x["destination"], x["travel_date"]) for x in items}), 56)

    def test_monitor_materializes_each_selected_cabin(self):
        monitor = {
            "id": "monitor-multi-cabin",
            "filters": {
                "origins": ["POZ"], "destinations": ["BKK"],
                "from": "2026-09-01", "to": "2026-09-01",
                "cabins": ["BUSINESS", "FIRST"],
            },
        }
        items = scanner.monitor_combinations(monitor)
        self.assertEqual({item["cabin"] for item in items}, {"business", "first"})
        self.assertEqual(len(items), 2)

    def test_monitor_rejects_more_than_five_airports_per_side(self):
        monitor = {
            "id": "monitor-too-wide",
            "filters": {
                "origins": ["GDN", "WAW", "POZ", "VIE", "BUD", "MXP"],
                "destinations": ["BKK"],
                "from": "2026-09-01",
                "to": "2026-09-14",
                "cabin": "BUSINESS",
            },
        }
        self.assertEqual(scanner.monitor_combinations(monitor), [])

    def test_monitor_rejects_unknown_iata_code(self):
        monitor = {
            "id": "monitor-invalid-airport",
            "filters": {
                "origins": ["ZZZ"], "destinations": ["BKK"],
                "from": "2026-09-01", "to": "2026-09-01",
                "cabin": "BUSINESS",
            },
        }
        self.assertNotIn("ZZZ", scanner.VALID_AIRPORT_CODES)
        self.assertEqual(scanner.monitor_combinations(monitor), [])

    def test_paginated_history_reads_every_page(self):
        calls = []

        def fake_api(method, path, body=None, params=None):
            calls.append(params.copy())
            offset = int(params["offset"])
            return [{"id": offset}, {"id": offset + 1}] if offset == 0 else [{"id": offset}]

        with patch.object(scanner, "api", side_effect=fake_api):
            rows = scanner.fetch_all_rows("user_matches", {"select": "id"}, page_size=2)
        self.assertEqual([row["id"] for row in rows], [0, 1, 2])
        self.assertEqual([call["offset"] for call in calls], ["0", "2"])

    def test_manual_scan_forces_existing_monitor_queue_due(self):
        calls = []

        def fake_api(method, path, body=None, params=None):
            calls.append((method, path, body, params))
            return []

        with patch.object(scanner, "api", side_effect=fake_api):
            scanner.force_due_scan_items(
                [{"id": "monitor-a"}, {"id": "monitor-b"}],
                datetime(2026, 9, 1, 12, 0, 0),
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], "monitor_scan_items")
        self.assertEqual(calls[1][1], "monitors")
        self.assertEqual(calls[0][2]["next_scan_at"], "2026-09-01T12:00:00Z")

    def test_scan_selection_rotates_between_monitors(self):
        due = []
        for monitor_id in ("a", "b", "c"):
            for day in range(1, 5):
                due.append({"id": f"{monitor_id}-{day}", "monitor_id": monitor_id,
                            "origin": monitor_id.upper(), "destination": "BKK",
                            "travel_date": f"2026-09-{day:02d}", "cabin": "business"})
        selected = scanner.select_scan_items(due, max_standard=3, max_first=0)
        self.assertEqual({item["monitor_id"] for item in selected}, {"a", "b", "c"})
        self.assertEqual(len(selected), 3)

    def test_standard_cabins_are_not_starved_by_business(self):
        due = []
        for day in range(1, 8):
            due.append({"id": f"business-{day}", "monitor_id": "business-user",
                        "origin": "POZ", "destination": "BKK",
                        "travel_date": f"2026-09-{day:02d}", "cabin": "business"})
        due.append({"id": "economy-1", "monitor_id": "economy-user",
                    "origin": "WAW", "destination": "BKK",
                    "travel_date": "2026-09-01", "cabin": "economy"})
        selected = scanner.select_scan_items(due, max_standard=4, max_first=0)
        self.assertEqual(len(selected), 4)
        self.assertIn("economy-user", {item["monitor_id"] for item in selected})
        self.assertEqual(len(scanner.select_scan_items(due, max_standard=1, max_first=0)), 1)

    def test_historical_duplicate_uses_durable_low_not_current_offer(self):
        previous = [{
            "min_price_for_user": 5000,
            "flight_offers": {
                "route": "POZ → BKK", "travel_date": "2026-09-01",
                "airline": "QR", "price_pln": 5000,
            },
        }]
        previous[0]["flight_offers"]["cabin"] = "BUSINESS"
        self.assertTrue(scanner.historical_duplicate(previous, "POZ → BKK", "BUSINESS", "QR", "2026-09-02", 5500))
        self.assertFalse(scanner.historical_duplicate(previous, "POZ → BKK", "BUSINESS", "QR", "2026-09-02", 4900))
        self.assertFalse(scanner.historical_duplicate(previous, "POZ → BKK", "BUSINESS", "EY", "2026-09-02", 5500))
        self.assertFalse(scanner.historical_duplicate(previous, "POZ → BKK", "FIRST", "QR", "2026-09-02", 5500))

    def test_exact_ten_percent_drop_is_eligible(self):
        self.assertTrue(scanner.price_drop_eligible(5000, 4500, 10))
        self.assertFalse(scanner.price_drop_eligible(5000, 4501, 10))

    def test_user_preferred_airline_increases_offer_score(self):
        flight = {"airline": "SQ", "airline_name": "Singapore Airlines", "price_pln": 5000, "duration_h": 14, "stops": 1}
        base = scanner.score(flight, {"budget_pln": 6000})
        preferred = scanner.score(flight, {"budget_pln": 6000, "preferred_airlines": ["Singapore Airlines"]})
        self.assertEqual(preferred, min(5, base + 1))

    def test_preferred_airline_is_kept_even_when_not_top_three(self):
        flights = [
            {"airline": "CA", "airline_name": "Air China", "price_pln": 3000, "duration_h": 14, "stops": 1},
            {"airline": "MU", "airline_name": "China Eastern", "price_pln": 3100, "duration_h": 14, "stops": 1},
            {"airline": "CZ", "airline_name": "China Southern", "price_pln": 3200, "duration_h": 14, "stops": 1},
            {"airline": "SQ", "airline_name": "Singapore Airlines", "price_pln": 5200, "duration_h": 13, "stops": 1},
        ]
        picks = gflights.cheapest_picks(flights, {"SQ"}, max_options=3)
        self.assertIn("SQ", {flight["airline"] for flight in picks})

    def test_unknown_duration_or_stops_is_rejected(self):
        filters = {"max_duration_h": 24, "max_stops": 2}
        self.assertFalse(scanner.quality({"duration_h": None, "stops": 1}, filters))
        self.assertFalse(scanner.quality({"duration_h": 12, "stops": None}, filters))
        self.assertTrue(scanner.quality({"duration_h": 12, "stops": 1}, filters))
        self.assertFalse(scanner.quality({"duration_h": 24.1, "stops": 1}, filters))

    def test_budget_rejects_normal_offer_but_keeps_error_fare_exception(self):
        filters = {"budget_pln": 4500}
        self.assertTrue(scanner.budget_ok({"price_pln": 4500}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": 4501}, filters))
        self.assertTrue(scanner.budget_ok({"price_pln": 12000, "tags": ["Error Fare"]}, filters))
        self.assertTrue(scanner.budget_ok({"price_pln": 12000, "tags": ["Mistake Fare"]}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": None}, filters))

    def test_prices_accept_european_thousands_formats(self):
        self.assertEqual(gflights._parse_price_pln("5.173 PLN"), 5173)
        self.assertEqual(rss.price("1.000 EUR"), 4350)
        self.assertEqual(rss.price("1,299.00 USD"), 4936)
        self.assertEqual(rss.premium_price("Economy 500 EUR, Business class 1.500 EUR"), 6525)
        self.assertIsNone(rss.premium_price("Economy 500 EUR, hotel 300 EUR"))

    def test_rss_requires_a_valid_recent_publication_date(self):
        self.assertFalse(rss.fresh(None))
        self.assertFalse(rss.fresh("not a date"))
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S %z")
        old = (datetime.now(timezone.utc) - timedelta(days=22)).strftime("%a, %d %b %Y %H:%M:%S %z")
        self.assertTrue(rss.fresh(recent))
        self.assertFalse(rss.fresh(old))
        self.assertFalse(rss.fresh("Tue, 28 Jul 2030 10:00:00 +0000"))

    def test_rss_keeps_detected_first_class_for_multi_cabin_monitor(self):
        monitor = {
            "filters": {
                "origins": ["WAW"], "destinations": ["BKK"],
                "from": "2026-09-01", "to": "2026-09-01",
                "cabins": ["BUSINESS", "FIRST"], "cabin": "BUSINESS",
            }
        }
        item = {
            "title": "Warsaw to Bangkok First class 1.000 EUR",
            "description": "2026-09-01, 12 hours, 1 stop",
            "link": "https://example.com/deal", "source": "Test",
        }
        with patch.object(rss, "FEEDS", [{"name": "Test", "url": "https://example.com/rss"}]), \
             patch.object(rss, "items", return_value=[item]):
            candidates = rss.candidates([monitor])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][1]["cabin"], "FIRST")

    def test_links_are_restricted_to_http(self):
        self.assertEqual(rss.safe_link("https://example.com/deal"), "https://example.com/deal")
        self.assertEqual(rss.safe_link("javascript:alert(1)"), "")

    def test_new_airline_is_alert_eligible_even_when_not_cheapest(self):
        previous = [{
            "id": "old-match", "offer_id": "old-offer", "min_price_for_user": 4000,
            "flight_offers": {"route": "POZ → BKK", "travel_date": "2026-09-01",
                               "airline": "QR", "price_pln": 4000},
        }]
        monitor = {"id": "monitor-1", "user_id": "user-1", "filters": {"max_duration_h": 24, "max_stops": 2, "budget_pln": 6000},
                   "telegram_rules": {"min_stars": 4, "immediate_new_low": True}}
        flight = {"airline": "EY", "airline_name": "Etihad", "price_pln": 5000, "duration_h": 12, "stops": 1, "departure": "09:00"}
        saved_match = {}

        def fake_api(method, path, body=None, params=None):
            if method == "GET" and path == "user_matches":
                return previous
            if method == "POST" and path == "flight_offers":
                return [{"id": "new-offer", **body}]
            if method == "POST" and path == "user_matches":
                saved_match.update(body)
                return [{"id": "new-match", **body}]
            if method == "GET" and path == "telegram_connections":
                return [{"chat_id": "123"}]
            return []

        with patch.object(scanner, "api", side_effect=fake_api), patch.object(scanner, "telegram", return_value={"ok": True}):
            added, sent = scanner.process_candidate(monitor, {"origin": "POZ", "dest": "BKK", "date": "2026-09-02", "cabin": "business"}, flight)
        self.assertEqual((added, sent), (1, 1))
        self.assertTrue(saved_match["telegram_eligible"])
        self.assertTrue(saved_match["new_airline"])

    def test_unnotified_alert_eligibility_survives_missing_telegram_connection(self):
        monitor = {"id": "monitor-1", "user_id": "user-1", "filters": {"max_duration_h": 24, "max_stops": 2, "budget_pln": 6000},
                   "telegram_rules": {"min_stars": 4, "immediate_new_low": True}}
        task = {"origin": "POZ", "dest": "BKK", "date": "2026-09-02", "cabin": "business"}
        flight = {"airline": "QR", "airline_name": "Qatar Airways", "price_pln": 4000, "duration_h": 14, "stops": 1, "departure": "10:00"}
        state = {"offer": None, "match": None, "connected": False}

        def fake_api(method, path, body=None, params=None):
            if method == "GET" and path == "user_matches":
                return [{**state["match"], "flight_offers": state["offer"]}] if state["match"] else []
            if method == "POST" and path == "flight_offers":
                state["offer"] = {"id": "offer", **body}; return [state["offer"]]
            if method == "POST" and path == "user_matches":
                state["match"] = {**(state["match"] or {"id": "match", "notified_at": None, "last_notified_price": None}), **body}
                return [state["match"].copy()]
            if method == "GET" and path == "telegram_connections":
                return [{"chat_id": "1"}] if state["connected"] else []
            return []

        with patch.object(scanner, "api", side_effect=fake_api), patch.object(scanner, "telegram", return_value={"ok": True}):
            self.assertEqual(scanner.process_candidate(monitor, task, flight), (1, 0))
            self.assertTrue(state["match"]["telegram_eligible"])
            state["connected"] = True
            self.assertEqual(scanner.process_candidate(monitor, task, flight), (1, 1))

    def test_network_retry_does_not_retry_google_block(self):
        task = {"origin": "POZ", "dest": "BKK", "date": "2026-09-02", "cabin": "business"}
        with patch.object(scanner.gflights, "fetch_gf", side_effect=[urllib.error.URLError("temporary"), ("low", [])]) as fetch:
            self.assertEqual(scanner.fetch_task(task), ("low", []))
            self.assertEqual(fetch.call_count, 2)
        with patch.object(scanner.gflights, "fetch_gf", side_effect=scanner.gflights.BlockedError("blocked")) as fetch:
            with self.assertRaises(scanner.gflights.BlockedError): scanner.fetch_task(task)
            self.assertEqual(fetch.call_count, 1)

    def test_google_http_throttling_is_classified_as_block(self):
        error = urllib.error.HTTPError("https://google.test", 429, "too many requests", {}, None)
        with patch.object(gflights.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gflights.BlockedError):
                gflights.fetch_gf("POZ", "BKK", "2026-09-02")

    def test_invalid_telegram_feedback_is_rejected_before_database_write(self):
        api_calls = []

        def fake_api(method, path, body=None, params=None):
            api_calls.append((method, path, body, params))
            if method == "GET" and path == "telegram_state":
                return [{"update_offset": 0}]
            return []

        def fake_telegram(method, payload=None):
            if method == "getUpdates":
                return {"result": [{
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": "fb|match-1|invalid",
                        "message": {"chat": {"id": 123}},
                    },
                }]}
            return {"ok": True}

        with patch.object(scanner, "TG_TOKEN", "test-token"), \
             patch.object(scanner, "api", side_effect=fake_api), \
             patch.object(scanner, "telegram", side_effect=fake_telegram):
            scanner.process_link_updates()

        self.assertFalse(any(method == "POST" and path == "feedback" for method, path, _, _ in api_calls))

    def test_frontend_dependencies_and_browser_policy_are_pinned(self):
        html = (ROOT / "site" / "index.html").read_text()
        self.assertIn("@supabase/supabase-js@2.110.9", html)
        self.assertIn('integrity="sha384-', html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn('name="referrer" content="no-referrer"', html)
        self.assertIn('href="https://t.me/flight_radar_kyudo_bot"', html)

    def test_frontend_loads_matches_and_offers_separately(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn('select("id, monitor_id, offer_id, stars, feedback, notified_at, updated_at")', app)
        self.assertIn('from("flight_offers")', app)
        self.assertIn('.in("id", offerIds)', app)
        self.assertIn('.range(from, to)', app)
        self.assertIn('if (offersLoading) return;', app)
        self.assertIn('flight_offers: byId.get(match.offer_id) || null', app)
        self.assertIn('monitor?.filters?.budget_pln', app)
        self.assertIn('id="loadMoreOffersButton"', (ROOT / "site" / "index.html").read_text())

    def test_google_block_uses_circuit_breaker(self):
        source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("except gflights.BlockedError as exc:", source)
        self.assertIn("Przerwano Google po wykryciu blokady", source)
        self.assertIn("break\n            except Exception", source)

    def test_telegram_feedback_has_fast_separate_workflow(self):
        workflow = (ROOT / ".github" / "workflows" / "telegram-feedback.yml").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn('group: friends-backend', workflow)
        self.assertIn("python scanner/telegram_feedback.py", workflow)
        self.assertIn("telegram_io.process_link_updates(api, telegram, TG_TOKEN)", scanner_source)

    def test_telegram_auth_is_origin_limited_and_rate_limited(self):
        source = (ROOT / "supabase" / "functions" / "telegram-auth" / "index.ts").read_text()
        schema = (ROOT / "supabase" / "schema.sql").read_text()
        self.assertNotIn("'Access-Control-Allow-Origin': '*'", source)
        self.assertIn("APP_ORIGIN", source)
        self.assertIn("telegram_auth_attempts", source)
        self.assertIn("corsHeaders, 429", source)
        self.assertIn("create table public.telegram_auth_attempts", schema)
        self.assertIn("revoke all on table public.telegram_auth_attempts", schema)

    def test_global_airport_dataset_is_large_and_contains_common_codes(self):
        import json
        airports = json.loads((ROOT / "site" / "airports.json").read_text())
        self.assertGreater(len(airports), 5000)
        for code in ("POZ", "BKK", "JFK", "LGA", "SYD"):
            self.assertIn(code, airports)

    def test_telegram_smoke_workflow_uses_real_delivery_script(self):
        workflow = (ROOT / ".github" / "workflows" / "telegram-smoke.yml").read_text()
        script = (ROOT / "scanner" / "telegram_smoke.py").read_text()
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("python scanner/telegram_smoke.py", workflow)
        self.assertIn('telegram_io.telegram("sendMessage"', script)
        self.assertIn('response.get("ok")', script)

    def test_telegram_jobs_do_not_import_google_flights(self):
        feedback = (ROOT / "scanner" / "telegram_feedback.py").read_text()
        smoke = (ROOT / "scanner" / "telegram_smoke.py").read_text()
        helper = (ROOT / "scanner" / "telegram_io.py").read_text()
        self.assertNotIn("friends_scanner", feedback + smoke)
        self.assertNotIn("fast_flights", feedback + smoke + helper)

    def test_admin_is_a_separate_role_gated_app_tab(self):
        html = (ROOT / "site" / "index.html").read_text()
        app_js = (ROOT / "site" / "app.js").read_text()
        self.assertIn('id="radarTab"', html)
        self.assertIn('id="adminTab"', html)
        self.assertIn('id="radarView"', html)
        self.assertIn('id="adminPanel"', html)
        self.assertIn('id="appTabs"', html)
        self.assertEqual(html.count('id="radarTab"'), 1)
        self.assertLess(html.index('id="appTabs"'), html.index('id="appView"'))
        self.assertIn('class="radar-sections"', html)
        self.assertIn('id="alertsSection"', html)
        self.assertIn('id="monitorsSection"', html)
        self.assertLess(html.index('id="alertsSection"'), html.index('id="monitorsSection"'))
        self.assertNotIn('Oferty dopasowane do Twoich zasad.', html)
        self.assertIn('name="monitorCabin"', html)
        self.assertIn('wybierz jedną lub więcej', html)
        self.assertIn('Array.isArray(relation)', app_js)
        self.assertIn('offer.route || !offer.travel_date', app_js)
        self.assertIn('show("adminTab", profile.role === "admin")', app_js)
        self.assertIn('profile?.role === "admin"', app_js)
        self.assertIn('show("adminView", adminVisible)', app_js)

    def test_telegram_bootstrap_password_fits_bcrypt_limit(self):
        source = (ROOT / "supabase" / "functions" / "telegram-auth" / "index.ts").read_text()
        self.assertEqual(source.count("crypto.randomUUID().replaceAll('-', '')"), 2)
        password_bytes = 32 * 2 + len("A9!")
        self.assertEqual(password_bytes, 67)
        self.assertLessEqual(password_bytes, 72)

    def test_github_actions_are_pinned_to_full_commit_hashes(self):
        import re
        workflows = "\n".join(path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml"))
        action_refs = re.findall(r"uses:\s*[^\s@]+@([^\s#]+)", workflows)
        self.assertTrue(action_refs)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs))

    def test_database_policies_are_limited_to_authenticated_users(self):
        schema = (ROOT / "supabase" / "schema.sql").read_text()
        policies = [line for line in schema.splitlines() if line.startswith("create policy ")]
        self.assertTrue(policies)
        self.assertTrue(all(" to authenticated " in line for line in policies))
        self.assertIn("where x.value !~ '^[A-Z]{3}$'", schema)
        self.assertIn("new.filters ? 'cabins'", schema)
        self.assertIn("jsonb_array_length(new.filters -> 'cabins') > 4", schema)


if __name__ == "__main__":
    unittest.main()
