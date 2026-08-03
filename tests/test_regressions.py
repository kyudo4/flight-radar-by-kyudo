import json
import sys
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scanner"))

import friends_scanner as scanner
import gflights
import google_browser
import google_parser
import rss
import telegram_feedback
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

    def test_round_trip_monitor_materializes_only_returns_after_departure(self):
        monitor = {
            "id": "monitor-round-trip",
            "filters": {
                "origins": ["POZ"], "destinations": ["BKK"],
                "from": "2026-09-01", "to": "2026-09-02",
                "return_from": "2026-09-03", "return_to": "2026-09-04",
                "trip_type": "round_trip", "cabins": ["BUSINESS"],
            },
        }
        items = scanner.monitor_combinations(monitor)
        self.assertEqual(len(items), 4)
        self.assertTrue(all(item["trip_type"] == "round_trip" for item in items))
        self.assertEqual({item["return_date"] for item in items}, {"2026-09-03", "2026-09-04"})

    def test_round_trip_google_filter_has_two_legs(self):
        fake_filter = type("FakeFilter", (), {"as_b64": lambda self: b"round-trip-filter"})()
        with patch.object(gflights, "create_filter", return_value=fake_filter) as create:
            url = gflights.build_url("POZ", "BKK", "2026-09-01", return_date="2026-09-14")
        self.assertIn("round-trip-filter", url)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["trip"], "round-trip")
        self.assertEqual(len(kwargs["flight_data"]), 2)
        self.assertEqual(kwargs["flight_data"][1].date, "2026-09-14")

    def test_google_parser_reads_current_server_payload_instead_of_empty_html(self):
        segment = [None, None, "Qatar", "POZ", "Poznań Airport", "Bangkok", "BKK", None,
                   [6], None, [8, 30], 150, [], 1, "", [], 3, "A350", None, 0,
                   [2026, 9, 1], [2026, 9, 1]]
        payload = [None, None, None, [[[["business", ["Qatar Airways"], [segment]], [[0, 4500]]]]]]
        html = '<script class="ds:1">AF_initDataCallback({data:' + json.dumps(payload) + ',x:1})</script>'
        flights = google_parser.parse(html)
        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0]["price_pln"], 4500)
        self.assertEqual(flights[0]["stops"], 0)
        self.assertEqual(flights[0]["airline_name"], "Qatar Airways")

    def test_google_parser_finds_groups_after_payload_path_moves(self):
        segment = [None, None, "Qatar", "POZ", "Poznań Airport", "Bangkok", "BKK", None,
                   [6], None, [8, 30], 150, [], 1, "", [], 3, "A350", None, 0,
                   [2026, 9, 1], [2026, 9, 1]]
        groups = [[["business", ["Qatar Airways"], [segment]], [[0, 4500]]]]
        payload = [None, None, None, None, None, None, None, [groups]]
        html = '<script class="ds:1">AF_initDataCallback({data:' + json.dumps(payload) + ',x:1})</script>'
        flights = google_parser.parse(html)
        self.assertEqual(flights[0]["price_pln"], 4500)

    def test_rendered_google_card_parser_uses_accessible_flight_data(self):
        label = (
            "From 6,653 Polish zlotys round trip total. 2 stops flight with KLM and Air France. "
            "Operated by KLM Cityhopper. Leaves Gdansk Lech Walesa Airport at 6:05 AM on "
            "Wednesday, October 21 and arrives at Kansai International Airport at 9:40 AM on "
            "Thursday, October 22. Total duration 20 hr 35 min. Layover details. Select flight"
        )
        card = google_browser.parse_card_label(label)
        self.assertEqual(card["price_pln"], 6653)
        self.assertEqual(card["stops"], 2)
        self.assertAlmostEqual(card["duration_h"], 20 + 35 / 60, places=2)
        self.assertEqual(card["airline_name"], "KLM and Air France")
        self.assertIn("6:05 AM", card["departure"])

    def test_rendered_google_card_parser_accepts_changed_units_and_currency(self):
        label = (
            "From 4 950 PLN round trip total. 1 stop flight on Qatar Airways. "
            "Leaves Warsaw Airport at 10:00 AM and arrives at Bangkok Airport at "
            "8:00 AM. Total duration 14 hours 5 minutes."
        )
        card = google_browser.parse_card_label(label)
        self.assertEqual(card["price_pln"], 4950)
        self.assertEqual(card["stops"], 1)
        self.assertAlmostEqual(card["duration_h"], 14 + 5 / 60, places=2)

    def test_rendered_fallback_accepts_aria_label_cards_without_select_link(self):
        label = (
            "From 4,500 PLN. 1 stop flight with Qatar Airways. "
            "Leaves Warsaw Airport at 10:00 AM and arrives at Bangkok Airport at "
            "8:00 AM. Total duration 14 hours."
        )

        class EmptyLinks:
            def count(self): return 0

        class AccessibleCards:
            def count(self): return 1
            def nth(self, _): return self
            def get_attribute(self, _): return label

        class FakePage:
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return EmptyLinks()
            def locator(self, selector):
                return AccessibleCards() if "Total duration" in selector else EmptyLinks()
            def wait_for_timeout(self, _): pass

        cards = google_browser._load_cards(FakePage(), "https://google.test")
        self.assertEqual(cards[0]["price_pln"], 4500)
        self.assertEqual(cards[0]["airline_name"], "Qatar Airways")

    def test_rendered_fallback_retries_a_transient_empty_page(self):
        class FakeFirst:
            def __init__(self, parent): self.parent = parent
            def wait_for(self, timeout=None):
                self.parent.waits += 1
                if self.parent.waits == 1:
                    raise TimeoutError("temporary empty render")

        class FakeLinks:
            def __init__(self): self.waits = 0; self.first = FakeFirst(self)
            def count(self): return 1
            def nth(self, _): return self
            def get_attribute(self, _):
                return ("From 4,500 Polish zlotys. 1 stop flight with Qatar Airways. "
                        "Leaves Poznan Airport at 10:00 AM and arrives at Bangkok Airport at "
                        "8:00 AM. Total duration 14 hr. Select flight")

        class FakePage:
            def __init__(self): self.links = FakeLinks(); self.gotos = 0
            def goto(self, *args, **kwargs): self.gotos += 1
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return self.links
            def locator(self, _):
                return type("Body", (), {"inner_text": lambda self, timeout=None: "Loading results"})()
            def wait_for_timeout(self, _): pass

        page = FakePage()
        cards = google_browser._load_cards(page, "https://google.test")
        self.assertEqual(page.gotos, 2)
        self.assertEqual(cards[0]["price_pln"], 4500)

    def test_rendered_fallback_waits_for_cards_after_heading(self):
        label = (
            "From 2,894 Polish zlotys. 2 stops flight with Qatar Airways. "
            "Leaves Gdansk Airport at 10:00 AM and arrives at Osaka Airport at "
            "4:00 PM. Total duration 22 hr 10 min. Select flight"
        )

        class DelayedCards:
            def __init__(self): self.polls = 0; self.first = self
            def count(self): return 1 if self.polls >= 2 else 0
            def nth(self, _): return self
            def get_attribute(self, _): return label
            def wait_for(self, timeout=None): return None

        class FakePage:
            def __init__(self): self.cards = DelayedCards()
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return self.cards
            def locator(self, selector):
                if selector == "body":
                    return type("Body", (), {"inner_text": lambda self, timeout=None: "Search results"})()
                return self.cards
            def wait_for_timeout(self, _): self.cards.polls += 1

        cards = google_browser._load_cards(FakePage(), "https://google.test")
        self.assertEqual(cards[0]["price_pln"], 2894)

    def test_rendered_fallback_dismisses_google_consent_wall(self):
        label = (
            "From 3,026 Polish zlotys. 2 stops flight with Scandinavian Airlines and Qatar Airways. "
            "Leaves Gdansk Airport at 10:15 AM and arrives at Osaka Airport at 4:25 PM. "
            "Total duration 22 hr 10 min. Select flight"
        )

        class Locator:
            def __init__(self, page, kind): self.page = page; self.kind = kind; self.first = self
            def count(self):
                if self.kind == "reject": return 1 if self.page.consent else 0
                if self.kind == "cards": return 0 if self.page.consent else 1
                return 1
            def click(self, timeout=None): self.page.consent = False
            def wait_for(self, timeout=None): return None
            def get_attribute(self, _): return label if self.kind == "cards" else ""
            def nth(self, _): return self
            def inner_text(self, timeout=None):
                return "Before you continue to Google" if self.page.consent else "Search results"

        class ConsentPage:
            def __init__(self): self.consent = True
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "button" and kwargs.get("name") == "Reject all": return Locator(self, "reject")
                if role == "heading": return Locator(self, "heading")
                return Locator(self, "cards")
            def locator(self, selector):
                if selector == "body": return Locator(self, "body")
                return Locator(self, "cards")
            def wait_for_load_state(self, *args, **kwargs): pass
            def wait_for_timeout(self, _): pass

        page = ConsentPage()
        cards = google_browser._load_cards(page, "https://google.test")
        self.assertFalse(page.consent)
        self.assertEqual(cards[0]["price_pln"], 3026)

    def test_round_trip_picker_forces_click_on_accessible_card(self):
        class Candidate:
            def __init__(self): self.first = self; self.force = None
            def count(self): return 1
            def click(self, timeout=None, force=False): self.force = force

        class Page:
            def __init__(self): self.candidate = Candidate()
            def get_by_role(self, role, **kwargs): return self.candidate
            def locator(self, selector): return self.candidate

        page = Page()
        self.assertTrue(google_browser._click_card(page, "complete flight label"))
        self.assertTrue(page.candidate.force)

    def test_scan_fails_when_every_google_query_has_source_error(self):
        source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("successful_google_tasks = 0", source)
        self.assertIn("failed_google_tasks = 0", source)
        self.assertIn("successful_google_tasks == 0", source)
        self.assertIn("source_degraded or source_unavailable", source)

    def test_explicit_no_flights_is_not_a_source_failure(self):
        class NoFlightPage:
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return type("Empty", (), {"count": lambda self: 0})()
            def locator(self, selector):
                if selector == "body":
                    return type("Body", (), {"inner_text": lambda self, timeout=None: "No flights found"})()
                return type("Empty", (), {"count": lambda self: 0})()
            def wait_for_timeout(self, _): pass

        with patch.object(gflights.google_browser, "_page", return_value=NoFlightPage()):
            with self.assertRaises(google_browser.BrowserNoFlightsError):
                google_browser._load_cards(NoFlightPage(), "https://google.test")

    def test_google_clean_empty_state_is_not_a_source_failure(self):
        class EmptyResultPage:
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return type("Empty", (), {"count": lambda self: 0})()
            def locator(self, selector):
                if selector == "body":
                    return type("Body", (), {
                        "inner_text": lambda self, timeout=None:
                        "Search results No results returned"
                    })()
                return type("Empty", (), {"count": lambda self: 0})()
            def wait_for_timeout(self, _): pass

        with self.assertRaises(google_browser.BrowserNoFlightsError):
            google_browser._load_cards(EmptyResultPage(), "https://google.test")

    def test_google_error_shell_is_retried_instead_of_marked_as_no_flights(self):
        class ErrorShellPage:
            def goto(self, *args, **kwargs): pass
            def get_by_role(self, role, **kwargs):
                if role == "heading":
                    return type("Heading", (), {"wait_for": lambda self, timeout=None: None})()
                return type("Empty", (), {"count": lambda self: 0})()
            def locator(self, selector):
                if selector == "body":
                    return type("Body", (), {
                        "inner_text": lambda self, timeout=None:
                            "Search results No results returned. Oops, something went wrong. Reload"
                    })()
                return type("Empty", (), {"count": lambda self: 0})()
            def wait_for_timeout(self, _): pass

        with self.assertRaises(google_browser.BrowserParseError):
            google_browser._load_cards(ErrorShellPage(), "https://google.test")

    def test_no_flights_fallback_is_returned_as_an_empty_result(self):
        with patch.object(gflights, "_fetch_server", side_effect=gflights.SourceParseError("payload moved")), \
             patch.object(gflights.google_browser, "fetch_rendered", side_effect=google_browser.BrowserNoFlightsError("empty")):
            level, flights = gflights.fetch_gf("GDN", "KIX", "2026-10-29")
        self.assertIsNone(level)
        self.assertEqual(flights, [])

    def test_google_uses_rendered_fallback_when_server_payload_moves(self):
        rendered = [{
            "airline_name": "Qatar Airways", "price_pln": 4300,
            "duration_h": 14.5, "stops": 1, "departure": "10:00 → 08:30",
            "round_trip_verified": True, "outbound_duration_h": 14.5,
            "outbound_stops": 1, "return_duration_h": None,
            "return_stops": None, "return_departure": "", "link": "https://google.test",
        }]
        with patch.object(gflights, "_fetch_server", side_effect=gflights.SourceParseError("old payload")), \
             patch.object(gflights.google_browser, "fetch_rendered", return_value=rendered) as fallback:
            _, flights = gflights.fetch_gf("POZ", "BKK", "2026-09-02")
        self.assertEqual(flights[0]["airline"], "QR")
        self.assertEqual(flights[0]["price_pln"], 4300)
        fallback.assert_called_once()

    def test_google_parser_only_verifies_round_trip_when_return_leg_is_present(self):
        outbound = [None, None, "Qatar", "POZ", "Poznań Airport", "Bangkok", "BKK", None,
                    [6], None, [8, 30], 150, [], 1, "", [], 3, "A350", None, 0,
                    [2026, 9, 1], [2026, 9, 1]]
        inbound = list(outbound)
        inbound[3], inbound[6] = "BKK", "POZ"
        inbound[8], inbound[10] = [10], [18, 30]
        inbound[20], inbound[21] = [2026, 9, 14], [2026, 9, 14]
        payload = [None, None, None, [[[["business", ["Qatar Airways"], [outbound, inbound]], [[0, 4500]]]]]]
        html = '<script class="ds:1">AF_initDataCallback({data:' + json.dumps(payload) + ',x:1})</script>'
        flights = google_parser.parse(html, origin="POZ", destination="BKK", return_date="2026-09-14")
        self.assertTrue(flights[0]["round_trip_verified"])

    def test_round_trip_parser_keeps_each_leg_duration_and_stops_separately(self):
        outbound = [None, None, "Qatar", "WAW", "Warsaw", "NRT", "NRT", None,
                    [6], None, [8, 30], 150, [], 1, "", [], 3, "A350", None, 0,
                    [2026, 9, 1], [2026, 9, 1]]
        inbound = list(outbound)
        inbound[3], inbound[6] = "NRT", "WAW"
        inbound[8], inbound[10] = [10], [18, 30]
        inbound[20], inbound[21] = [2026, 9, 14], [2026, 9, 14]
        payload = [None, None, None, [[[['business', ['Qatar Airways'], [outbound, inbound]], [[0, 4500]]]]]]
        html = '<script class="ds:1">AF_initDataCallback({data:' + json.dumps(payload) + ',x:1})</script>'
        flight = google_parser.parse(html, origin="WAW", destination="NRT", return_date="2026-09-14")[0]
        self.assertTrue(flight["round_trip_verified"])
        self.assertEqual(flight["outbound_duration_h"], 2.5)
        self.assertEqual(flight["return_duration_h"], 8.5)
        self.assertEqual(flight["outbound_stops"], 0)
        self.assertEqual(flight["return_stops"], 0)
        self.assertEqual(flight["duration_h"], 8.5)

    def test_round_trip_parser_splits_connecting_flights_at_return_origin(self):
        def segment(origin, destination, travel_day, departure_hour, arrival_hour):
            value = [None] * 22
            value[2], value[3], value[6] = "Qatar", origin, destination
            value[8], value[10] = [departure_hour], [arrival_hour]
            value[17], value[20], value[21] = "A350", travel_day, travel_day
            return value

        segments = [
            segment("WAW", "DOH", [2026, 9, 1], 8, 14),
            segment("DOH", "NRT", [2026, 9, 1], 16, 23),
            segment("NRT", "DOH", [2026, 9, 14], 10, 16),
            segment("DOH", "WAW", [2026, 9, 14], 18, 23),
        ]
        payload = [None, None, None, [[[['business', ['Qatar Airways'], segments], [[0, 4500]]]]]]
        html = '<script class="ds:1">AF_initDataCallback({data:' + json.dumps(payload) + ',x:1})</script>'
        flight = google_parser.parse(html, origin="WAW", destination="NRT", return_date="2026-09-14")[0]
        self.assertTrue(flight["round_trip_verified"])
        self.assertEqual(flight["outbound_duration_h"], 15)
        self.assertEqual(flight["return_duration_h"], 13)
        self.assertEqual(flight["outbound_stops"], 1)
        self.assertEqual(flight["return_stops"], 1)
        self.assertTrue(scanner.quality(flight, {
            "trip_type": "round_trip", "max_duration_h": 22, "max_stops": 1,
        }))

    def test_round_trip_quality_applies_time_and_stops_to_both_legs(self):
        flight = {
            "round_trip_verified": True,
            "outbound_duration_h": 21.5, "return_duration_h": 22,
            "outbound_stops": 1, "return_stops": 1,
        }
        filters = {"trip_type": "round_trip", "max_duration_h": 22, "max_stops": 1}
        self.assertTrue(scanner.quality(flight, filters))
        flight["return_duration_h"] = 22.1
        self.assertFalse(scanner.quality(flight, filters))
        flight["return_duration_h"] = 22
        flight["return_stops"] = 2
        self.assertFalse(scanner.quality(flight, filters))

    def test_round_trip_queue_has_a_safe_combination_cap(self):
        monitor = {"id": "large-round-trip", "filters": {
            "origins": ["GDN", "WAW", "POZ", "VIE", "MXP"],
            "destinations": ["BKK", "SIN", "KUL", "HKG", "NRT"],
            "from": "2026-09-01", "to": "2026-10-02",
            "return_from": "2026-10-03", "return_to": "2026-11-03",
            "trip_type": "round_trip", "cabins": ["BUSINESS", "FIRST", "ECONOMY", "PREMIUM_ECONOMY"],
        }}
        self.assertEqual(scanner.monitor_combination_count(monitor["filters"]), 102400)
        self.assertEqual(scanner.monitor_combinations(monitor), [])

    def test_past_departures_are_not_materialized(self):
        past = (date.today() - timedelta(days=2)).isoformat()
        past_end = (date.today() - timedelta(days=1)).isoformat()
        monitor = {"id": "past", "filters": {"origins": ["POZ"], "destinations": ["BKK"],
                                                   "from": past, "to": past_end, "cabin": "BUSINESS"}}
        self.assertEqual(scanner.monitor_combinations(monitor), [])

    def test_round_trip_requires_at_least_one_valid_departure_return_pair(self):
        start = (date.today() + timedelta(days=10)).isoformat()
        end = (date.today() + timedelta(days=11)).isoformat()
        monitor = {"id": "invalid-pair", "filters": {"origins": ["POZ"], "destinations": ["BKK"],
            "from": start, "to": end, "return_from": (date.today() + timedelta(days=1)).isoformat(),
            "return_to": (date.today() + timedelta(days=2)).isoformat(), "trip_type": "round_trip", "cabin": "BUSINESS"}}
        self.assertEqual(scanner.monitor_combinations(monitor), [])

    def test_query_ramp_drops_after_google_block_and_grows_after_healthy_runs(self):
        with patch.object(scanner, "api", return_value=[{"status": "blocked", "blocked": True, "standard_limit": 320, "first_limit": 18}]):
            reduced = scanner.adaptive_query_limits()
        self.assertEqual(reduced, {"standard": 160, "first": 9})
        with patch.object(scanner, "api", return_value=[
            {"status": "ok", "blocked": False, "standard_limit": 240, "first_limit": 12},
            {"status": "ok", "blocked": False, "standard_limit": 200, "first_limit": 10},
            {"status": "partial", "blocked": False, "standard_limit": 160, "first_limit": 8},
        ]):
            increased = scanner.adaptive_query_limits()
            self.assertEqual(increased, {"standard": 240, "first": 12})

        with patch.object(scanner, "api", side_effect=RuntimeError("history unavailable")):
            self.assertEqual(scanner.adaptive_query_limits(), {"standard": 60, "first": 4})

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

    def test_round_trip_duplicate_ignores_return_date_when_comparing_new_days(self):
        previous = [{"min_price_for_user": 5000, "flight_offers": {
            "route": "POZ → BKK", "travel_date": "2026-09-01", "return_date": "2026-09-10",
            "trip_type": "round_trip", "cabin": "BUSINESS", "airline": "QR", "price_pln": 5000,
        }}]
        self.assertTrue(scanner.historical_duplicate(previous, "POZ → BKK", "BUSINESS", "QR", "2026-09-02", 5300, "2026-09-11", "round_trip"))
        self.assertFalse(scanner.historical_duplicate(previous, "POZ → BKK", "BUSINESS", "QR", "2026-09-02", 4900, "2026-09-11", "round_trip"))

    def test_exact_ten_percent_drop_is_eligible(self):
        self.assertTrue(scanner.price_drop_eligible(5000, 4500, 10))
        self.assertFalse(scanner.price_drop_eligible(5000, 4501, 10))

    def test_immediate_new_low_does_not_bypass_repeat_drop_threshold(self):
        match = {"id": "match-1", "stars": 5, "last_notified_price": 5000,
                 "telegram_eligible": True, "new_airline": False, "_same_offer": True}
        offer = {"price_pln": 4950, "tags": []}
        monitor = {"filters": {"budget_pln": 6000},
                   "telegram_rules": {"min_stars": 3, "drop_percent": 10, "immediate_new_low": True}}
        with patch.object(scanner, "telegram") as telegram:
            self.assertFalse(scanner.send_due_alert(match, offer, monitor, {"chat_id": "123"}))
        telegram.assert_not_called()

    def test_user_preferred_airline_increases_offer_score(self):
        flight = {"airline": "SQ", "airline_name": "Singapore Airlines", "price_pln": 5000, "duration_h": 14, "stops": 1}
        base = scanner.score(flight, {"budget_pln": 6000})
        preferred = scanner.score(flight, {"budget_pln": 6000, "preferred_airlines": ["Singapore Airlines"]})
        self.assertEqual(preferred, min(5, base + 1))

    def test_market_price_can_upgrade_non_priority_airline(self):
        flight = {"airline": "AY", "airline_name": "Finnair", "price_pln": 4500, "duration_h": 17.7, "stops": 1}
        self.assertEqual(scanner.score(flight, {"budget_pln": 5000}), 3)
        self.assertEqual(scanner.score(
            flight, {"budget_pln": 5000}, market_prices=[4500, 6500, 7000, 8200]
        ), 4)

    def test_priority_airline_is_not_required_for_high_market_rating(self):
        flight = {"airline": "AY", "airline_name": "Finnair", "price_pln": 3500, "duration_h": 14, "stops": 1}
        self.assertEqual(scanner.score(
            flight, {"budget_pln": 6000}, market_prices=[3500, 7000, 7600]
        ), 5)

    def test_market_rating_can_downgrade_a_fare_that_is_bad_for_the_route(self):
        flight = {"airline": "AY", "airline_name": "Finnair", "price_pln": 5000, "duration_h": 14, "stops": 1}
        self.assertEqual(scanner.score(
            flight, {"budget_pln": 5000}, market_prices=[3500, 4000, 5000]
        ), 1)

    def test_priority_bonus_does_not_rescue_a_bad_market_fare(self):
        flight = {"airline": "EY", "airline_name": "Etihad", "price_pln": 5000, "duration_h": 14, "stops": 1}
        self.assertEqual(scanner.score(
            flight, {"budget_pln": 6000}, market_prices=[4000, 4500, 5000]
        ), 2)

    def test_sparse_market_data_does_not_overrate_one_observation(self):
        self.assertIsNone(scanner.market_price_reference([4500, 7000]))

    def test_market_reference_counts_distinct_offers_with_the_same_price(self):
        offers = [
            {"airline": "AY", "price_pln": 4500, "departure": "08:00", "duration_h": 18, "stops": 1},
            {"airline": "LH", "price_pln": 7000, "departure": "09:00", "duration_h": 17, "stops": 1},
            {"airline": "KL", "price_pln": 7000, "departure": "10:00", "duration_h": 19, "stops": 1},
            {"airline": "AF", "price_pln": 7000, "departure": "11:00", "duration_h": 20, "stops": 1},
        ]
        self.assertEqual(scanner.market_price_reference(offers), 7000)

        duplicated_card = [offers[0], offers[1], dict(offers[1])]
        self.assertIsNone(scanner.market_price_reference(duplicated_card))

    def test_live_offer_and_its_stored_history_are_one_market_observation(self):
        task = {"origin": "GDN", "dest": "KIX", "date": "2026-10-25", "cabin": "economy", "trip_type": "one_way"}
        current = {"airline": "AY", "price_pln": 4500, "departure": "09:40", "duration_h": 19.17, "stops": 1}
        other = {"airline": "LH", "price_pln": 7000, "departure": "11:00", "duration_h": 18, "stops": 1}
        live = scanner.market_observations(task, [current, other], {})
        history = {"price_pln": 4500, "_market_key": scanner.offer_fingerprint(task, current)}
        self.assertIsNone(scanner.market_price_reference(live + [history]))
        self.assertEqual(scanner.score(current, {"budget_pln": 6000}, market_prices=live + [history]), 4)

    def test_round_trip_rating_penalizes_a_long_return_leg(self):
        flight = {
            "airline": "AY", "price_pln": 4500, "duration_h": 15, "outbound_duration_h": 15,
            "return_duration_h": 25, "stops": 1, "outbound_stops": 1, "return_stops": 1,
        }
        self.assertEqual(scanner.score(flight, {"budget_pln": 6000}), 3)

    def test_durable_bad_airline_signal_applies_across_monitors_and_dates(self):
        flight = {"airline": "CA", "airline_name": "Air China", "price_pln": 5000, "duration_h": 14, "stops": 1}
        filters = {"budget_pln": 6000}
        preferences = [{"dimension": "airline", "value": "CA", "cabin": "BUSINESS", "score": -3}]
        base = scanner.score(flight, filters)
        learned = scanner.score(
            flight, filters, preferences=preferences,
            route="WAW → NRT", destination="NRT", cabin="BUSINESS",
        )
        self.assertEqual(learned, max(1, base - 1))

    def test_durable_duration_and_price_signals_generalize_safely(self):
        preferences = [
            {"dimension": "duration", "value": "18", "cabin": "ECONOMY", "score": -3},
            {"dimension": "price", "value": "90", "cabin": "ECONOMY", "score": -3},
        ]
        slow_expensive = {"airline": "LO", "price_pln": 4500, "duration_h": 19, "stops": 1}
        fast_cheaper = {"airline": "LO", "price_pln": 3500, "duration_h": 14, "stops": 1}
        filters = {"budget_pln": 5000}
        self.assertEqual(scanner.preference_adjustment(
            slow_expensive, filters, preferences, cabin="ECONOMY"
        ), -2)
        self.assertEqual(scanner.preference_adjustment(
            fast_cheaper, filters, preferences, cabin="ECONOMY"
        ), 0)

    def test_durable_preferences_do_not_leak_between_cabins(self):
        flight = {"airline": "CA", "price_pln": 4000, "duration_h": 14, "stops": 1}
        preferences = [{"dimension": "airline", "value": "CA", "cabin": "BUSINESS", "score": -9}]
        self.assertEqual(scanner.preference_adjustment(
            flight, {"budget_pln": 5000}, preferences, cabin="ECONOMY"
        ), 0)

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
        self.assertTrue(scanner.quality({"duration_h": 42, "stops": 1}, {"max_stops": 2}))
        self.assertTrue(scanner.quality({"duration_h": None, "stops": 1}, {"max_stops": 2}))

    def test_budget_is_a_hard_limit_even_for_error_fares(self):
        filters = {"budget_pln": 4500}
        self.assertTrue(scanner.budget_ok({"price_pln": 4500}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": 4501}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": 12000, "tags": ["Error Fare"]}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": 12000, "tags": ["Mistake Fare"]}, filters))
        self.assertFalse(scanner.budget_ok({"price_pln": None}, filters))

    def test_telegram_never_sends_an_offer_above_budget(self):
        match = {"id": "match-1", "stars": 5, "last_notified_price": None,
                 "telegram_eligible": True, "new_airline": True}
        offer = {"price_pln": 12000, "tags": ["Error Fare"]}
        monitor = {"filters": {"budget_pln": 4500},
                   "telegram_rules": {"min_stars": 3, "immediate_new_low": True}}
        with patch.object(scanner, "telegram") as telegram:
            self.assertFalse(scanner.send_due_alert(match, offer, monitor, {"chat_id": "123"}))
        telegram.assert_not_called()

    def test_unverified_round_trip_never_sends_telegram_alert(self):
        match = {"id": "match-round-trip", "stars": 5, "last_notified_price": None,
                 "telegram_eligible": True, "new_airline": True}
        offer = {"price_pln": 3500, "tags": ["Powrót do potwierdzenia"]}
        monitor = {"filters": {"budget_pln": 5000},
                   "telegram_rules": {"min_stars": 3, "immediate_new_low": True}}
        with patch.object(scanner, "telegram") as telegram:
            self.assertFalse(scanner.send_due_alert(match, offer, monitor, {"chat_id": "123"}))
        telegram.assert_not_called()

    def test_round_trip_telegram_alert_places_each_leg_on_its_own_line(self):
        offer = {
            "route": "GDN → KIX", "cabin": "ECONOMY", "airline_name": "Finnair",
            "price_pln": 4500, "travel_date": "2026-10-22", "return_date": "2026-11-06",
            "link": "https://www.google.com/travel/flights", "raw": {
                "round_trip_verified": True, "outbound_duration_h": 17.666,
                "outbound_stops": 1, "return_duration_h": 17.25, "return_stops": 1,
            },
        }
        message = scanner.alert_text(offer, 3, "match-1")
        self.assertIn("Gdańsk (GDN) → Osaka (KIX)", message)
        self.assertIn("🗓 2026-10-22 → 2026-11-06\n", message)
        self.assertIn("🛫 Tam: 17h 40m · 1 przesiadka\n↩️ Powrót: 17h 15m · 1 przesiadka", message)
        self.assertNotIn("17h 40m, 1 przes. · powrót", message)

    def test_telegram_duration_rounding_carries_into_the_next_hour(self):
        offer = {
            "route": "GDN → KIX", "cabin": "ECONOMY", "airline_name": "Finnair",
            "price_pln": 4500, "travel_date": "2026-10-22", "return_date": "2026-11-06",
            "link": "https://www.google.com/travel/flights", "raw": {
                "round_trip_verified": True, "outbound_duration_h": 17.999,
                "outbound_stops": 1, "return_duration_h": 17.25, "return_stops": 1,
            },
        }
        message = scanner.alert_text(offer, 3, "match-1")
        self.assertIn("🛫 Tam: 18h 00m", message)
        self.assertNotIn("17h 60m", message)

    def test_database_budget_guard_is_part_of_read_policy(self):
        migration = (ROOT / "supabase" / "migrations" / "20260728230000_personal_data_isolation.sql").read_text()
        self.assertIn("match_within_monitor_budget", migration)
        self.assertIn("offer.price_pln <= coalesce((monitor.filters ->> 'budget_pln')::numeric, 0)", migration)
        self.assertNotIn("Error Fare", migration)
        self.assertNotIn("Mistake Fare", migration)
        self.assertIn("matches_owner_read", migration)
        self.assertIn("public.match_within_monitor_budget(id)", migration)
        self.assertIn("public.match_within_monitor_budget(m.id)", migration)
        self.assertIn("create or replace function public.can_read_flight_offer", migration)
        self.assertIn("using (public.can_read_flight_offer(id));", migration)

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

    def test_same_airline_from_prior_month_is_not_marked_as_new(self):
        previous = [{
            "id": "old-match", "offer_id": "old-offer", "min_price_for_user": 5000,
            "notified_at": "2026-08-01T00:00:00Z", "last_notified_price": 5000,
            "telegram_eligible": False, "new_airline": False, "feedback": None,
            "flight_offers": {
                "route": "POZ → BKK", "travel_date": "2026-09-05", "return_date": None,
                "trip_type": "one_way", "cabin": "BUSINESS", "airline": "QR",
                "airline_name": "Qatar Airways", "price_pln": 5000,
            },
        }]
        monitor = {
            "id": "monitor-1", "user_id": "user-1",
            "filters": {"max_stops": 2, "budget_pln": 6000},
            "telegram_rules": {"min_stars": 4, "drop_percent": 10, "immediate_new_low": False},
        }
        task = {"origin": "POZ", "dest": "BKK", "date": "2026-10-05", "cabin": "business", "trip_type": "one_way"}
        flight = {"airline": "QR", "airline_name": "Qatar Airways", "price_pln": 4800,
                  "duration_h": 14, "stops": 1, "departure": "10:00"}
        market = [
            flight,
            {"airline": "EY", "price_pln": 7000, "duration_h": 14, "stops": 1, "departure": "11:00"},
            {"airline": "EK", "price_pln": 7200, "duration_h": 15, "stops": 1, "departure": "12:00"},
        ]
        saved_match = {}

        def fake_api(method, path, body=None, params=None):
            if method == "POST" and path == "flight_offers":
                return [{"id": "new-offer", **body}]
            if method == "POST" and path == "user_matches":
                saved_match.update(body)
                return [{"id": "new-match", **body}]
            if method == "GET" and path == "telegram_connections":
                return [{"chat_id": "123"}]
            return []

        with patch.object(scanner, "api", side_effect=fake_api), patch.object(scanner, "telegram") as telegram:
            added, sent = scanner.process_candidate(
                monitor, task, flight, previous=previous, preferences=[], market_prices=market
            )
        self.assertEqual((added, sent), (1, 0))
        self.assertFalse(saved_match["new_airline"])
        telegram.assert_not_called()

    def test_source_error_routes_are_limited_to_the_current_streak(self):
        count, routes = scanner.reset_source_error_streak()
        count, routes = scanner.record_source_error(count, routes, ("WAW", "BKK"))
        count, routes = scanner.reset_source_error_streak()
        count, routes = scanner.record_source_error(count, routes, ("POZ", "NRT"))
        count, routes = scanner.reset_source_error_streak()
        for _ in range(3):
            count, routes = scanner.record_source_error(count, routes, ("GDN", "KIX"))
        self.assertEqual((count, len(routes)), (3, 1))
        self.assertFalse(scanner.source_circuit_open(count, routes))

        count, routes = scanner.reset_source_error_streak()
        for route in (("WAW", "BKK"), ("POZ", "NRT"), ("GDN", "KIX")):
            count, routes = scanner.record_source_error(count, routes, route)
        self.assertTrue(scanner.source_circuit_open(count, routes))

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

    def test_preference_read_retries_and_fails_closed(self):
        with patch.object(scanner, "fetch_all_rows", side_effect=[OSError("temporary"), OSError("temporary"), [{"score": 1}]]) as fetch, \
             patch.object(scanner.time, "sleep") as sleep:
            self.assertEqual(scanner.fetch_preferences("user-1"), [{"score": 1}])
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
        with patch.object(scanner, "fetch_all_rows", side_effect=OSError("database unavailable")) as fetch, \
             patch.object(scanner.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "bezpiecznie odczytać preferencji"):
                scanner.fetch_preferences("user-2")
            self.assertEqual(fetch.call_count, scanner.PREFERENCE_FETCH_RETRIES)

    def test_google_http_throttling_is_classified_as_block(self):
        error = urllib.error.HTTPError("https://google.test", 429, "too many requests", {}, None)
        with patch.dict(gflights.os.environ, {"GOOGLE_BROWSER_FALLBACK": "false"}), \
             patch.object(gflights.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gflights.BlockedError):
                gflights.fetch_gf("POZ", "BKK", "2026-09-02")

    def test_google_http_conflict_is_classified_as_block(self):
        error = urllib.error.HTTPError("https://google.test", 409, "conflict", {}, None)
        with patch.dict(gflights.os.environ, {"GOOGLE_BROWSER_FALLBACK": "false"}), \
             patch.object(gflights.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gflights.BlockedError):
                gflights.fetch_gf("GDN", "KIX", "2026-10-21")

    def test_google_captcha_body_is_classified_as_block(self):
        response = type("CaptchaResponse", (), {"status": 200, "read": lambda self: b"<html>unusual traffic - captcha</html>"})()
        with patch.dict(gflights.os.environ, {"GOOGLE_BROWSER_FALLBACK": "false"}), \
             patch.object(gflights.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            with self.assertRaises(gflights.BlockedError):
                gflights.fetch_gf("POZ", "BKK", "2026-09-01")

    def test_scan_queue_prefers_atomic_database_reconciliation(self):
        monitor = {
            "id": "monitor-atomic",
            "filters": {
                "origins": ["POZ"], "destinations": ["BKK"],
                "from": "2026-09-01", "to": "2026-09-01", "cabin": "BUSINESS",
            },
        }
        with patch.object(scanner, "api", return_value={"desired_count": 1, "queue_count": 1}) as api:
            scanner.sync_monitor_scan_items(monitor)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[:2], ("POST", "rpc/sync_monitor_scan_items"))
        self.assertEqual(len(api.call_args.kwargs["body"]["p_items"]), 1)

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

    def test_telegram_notification_levels_have_plain_language_labels(self):
        html = (ROOT / "site" / "index.html").read_text()
        app = (ROOT / "site" / "app.js").read_text()
        for label in ("Wszystkie powiadomienia", "Interesujące", "Najlepsze okazje"):
            self.assertIn(label, html)
            self.assertIn(label, app)

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

    def test_offer_and_monitor_cards_use_stacked_labeled_details(self):
        app = (ROOT / "site" / "app.js").read_text()
        styles = (ROOT / "site" / "styles.css").read_text()
        self.assertIn('class="offer-info"', app)
        self.assertIn('class="offer-info-row"', app)
        for label in ("Linia", "Klasa", "Termin", "Wylot", "Powrót", "Samolot", "Źródło"):
            self.assertIn(f'<span>{label}</span>', app)
        self.assertIn('class="monitor-info"', app)
        self.assertIn('.offer-info{display:grid', styles)
        self.assertIn('@media(max-width:760px){.offer-card-head', styles)
        self.assertIn('.offer-info,.monitor-info{grid-template-columns:1fr;gap:10px}', styles)

    def test_monitor_dialog_is_scrollable_and_explains_repeat_alert_threshold(self):
        html = (ROOT / "site" / "index.html").read_text()
        styles = (ROOT / "site" / "styles.css").read_text()
        self.assertIn("Ponowne powiadomienie po spadku ceny o (%)", html)
        self.assertIn("cena musi spaść o co najmniej 10%", html)
        self.assertIn('aria-describedby="telegramDropHint"', html)
        self.assertIn("dialog form{display:grid;gap:14px;max-height:calc(100dvh - 24px);overflow-y:auto}", styles)
        self.assertIn("dialog{width:calc(100% - 20px);max-height:calc(100dvh - 16px)", styles)
        self.assertIn("overflow-x:hidden", styles)
        self.assertIn("#monitorQueryEstimate:empty{display:none}", styles)
        self.assertIn(".cabin-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", styles)

    def test_mobile_header_wraps_controls_without_horizontal_overflow(self):
        styles = (ROOT / "site" / "styles.css").read_text()
        self.assertIn("html,body{max-width:100%;overflow-x:hidden}", styles)
        self.assertIn(".topbar{height:auto;min-height:64px;flex-wrap:wrap", styles)
        self.assertIn(".top-nav{order:3;flex:0 0 100%;width:100%", styles)
        self.assertIn(".top-actions .secondary{padding:8px 10px;font-size:12px", styles)

    def test_invite_link_has_real_copy_control_and_fallback(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn('data-copy-invite', app)
        self.assertIn('navigator.clipboard.writeText(link)', app)
        self.assertIn('document.execCommand("copy")', app)
        self.assertIn('aria-label="Link zaproszenia"', app)

    def test_dashboard_does_not_show_redundant_private_results_badge(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertNotIn("Wyniki tylko dla Ciebie", app)

    def test_offer_search_includes_city_names(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn('${offer.route || ""} ${routeName(offer.route)}', app)
        self.assertIn("const airportLabel =", app)
        self.assertIn("map(airportLabel)", app)

    def test_monitor_airports_use_city_search_and_store_iata_selections(self):
        app = (ROOT / "site" / "app.js").read_text()
        html = (ROOT / "site" / "index.html").read_text()
        styles = (ROOT / "site" / "styles.css").read_text()
        self.assertIn("function airportSearchResults(query, selected)", app)
        self.assertIn("function selectAirport(kind, code)", app)
        self.assertIn("airportSelections.origins", app)
        self.assertIn('data-airport-code=', app)
        self.assertIn('aria-autocomplete="list"', html)
        self.assertIn('id="monitorOriginsSuggestions"', html)
        self.assertIn('id="monitorDestinationsSuggestions"', html)
        self.assertIn(".airport-suggestions{position:absolute", styles)

    def test_personal_radar_queries_are_explicitly_scoped_to_current_user(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn('from("monitors").select("*").eq("user_id", user.id)', app)
        match_query = app[app.index('from("user_matches")'):app.index('if (matchError)')]
        self.assertIn('.eq("user_id", user.id)', match_query)

    def test_initial_auth_state_is_loaded_only_once(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn('if (event === "INITIAL_SESSION") return;', app)
        self.assertLess(app.index("await client.auth.getSession()"), app.index("client.auth.onAuthStateChange"))

    def test_final_database_policy_is_private_and_budget_guarded(self):
        schema = (ROOT / "supabase" / "schema.sql").read_text()
        migration = (ROOT / "supabase" / "migrations" / "20260728230000_personal_data_isolation.sql").read_text()
        for source in (schema, migration):
            can_read = source[source.index("create or replace function public.can_read_flight_offer"):]
            can_read = can_read[:can_read.index("$$;", can_read.index("as $$"))]
            self.assertIn("m.user_id = auth.uid()", can_read)
            self.assertIn("public.match_within_monitor_budget(m.id)", can_read)
            self.assertNotIn("public.is_admin()", can_read)
            self.assertIn("user_id = auth.uid()", source)
        self.assertIn("match_within_monitor_budget", schema)
        self.assertIn("drop policy if exists monitors_owner_all", migration)
        self.assertIn("drop policy if exists profiles_self_update", migration)
        self.assertIn("using (id = auth.uid())", migration)

    def test_site_and_database_changes_are_tested_before_deploy(self):
        checks = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
        pages = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
        for path in ('- "site/**"', '- "supabase/**"', '- "scripts/**"'):
            self.assertIn(path, checks)
        self.assertIn("Run pre-deploy verification", pages)
        self.assertIn("python -m unittest discover -s tests -v", pages)

    def test_duration_limit_is_optional_and_unlimited_by_default(self):
        app = (ROOT / "site" / "app.js").read_text()
        html = (ROOT / "site" / "index.html").read_text()
        migration = (ROOT / "supabase" / "migrations" / "20260802000100_unlimited_duration.sql").read_text()
        self.assertIn('placeholder="Bez limitu"', html)
        self.assertIn('durationRaw ? Number(durationRaw) : null', app)
        self.assertIn('durationLabel = f.max_duration_h ? `maks.', app)
        self.assertNotIn('maxDuration > 24', app)
        self.assertIn('duration is not null and duration <= 0', migration)
        self.assertNotIn('duration > 24', migration)
        self.assertIn("where filters ? 'max_duration_h'", migration)

    def test_google_block_uses_circuit_breaker(self):
        source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("except gflights.BlockedError as exc:", source)
        self.assertIn("Przerwano Google po wykryciu blokady", source)
        self.assertIn("break\n            except Exception", source)

    def test_scan_has_progress_and_runtime_guards(self):
        source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        workflow = (ROOT / ".github" / "workflows" / "scan.yml").read_text()
        self.assertIn("MAX_SCAN_RUNTIME_SECONDS", source)
        self.assertIn("update_scan_progress", source)
        self.assertIn("runtime_limit_reached", source)
        self.assertIn("timeout-minutes: 60", workflow)
        self.assertIn('MAX_SCAN_RUNTIME_SECONDS: "3000"', workflow)

    def test_telegram_feedback_has_fast_separate_workflow(self):
        workflow = (ROOT / ".github" / "workflows" / "telegram-feedback.yml").read_text()
        deployment = (ROOT / ".github" / "workflows" / "supabase-functions.yml").read_text()
        webhook = (ROOT / "supabase" / "functions" / "telegram-feedback-webhook" / "index.ts").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn('cron: "23 * * * *"', workflow)
        self.assertIn('group: friends-telegram-feedback', workflow)
        self.assertNotIn('group: friends-backend', workflow)
        self.assertIn("python scanner/telegram_feedback.py", workflow)
        self.assertIn("telegram-feedback-webhook", deployment)
        self.assertIn("setWebhook", deployment)
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", deployment)
        self.assertEqual(deployment.count("for attempt in 1 2 3"), 3)
        self.assertIn("x-telegram-bot-api-secret-token", webhook)
        self.assertIn(".eq('id', matchId).eq('user_id', connection.data.user_id)", webhook)
        self.assertNotIn("Access-Control-Allow-Origin", webhook)
        self.assertIn("telegram_io.process_link_updates(api, telegram, TG_TOKEN)", scanner_source)
        main_source = scanner_source.split("def main():", 1)[1]
        self.assertNotIn("process_link_updates()", main_source)

    def test_telegram_feedback_polling_is_only_a_webhook_fallback(self):
        with patch.object(telegram_feedback.telegram_io, "telegram", return_value={
            "ok": True, "result": {
                "url": "https://example.supabase.co/functions/v1/telegram-feedback-webhook",
                "last_error_date": 1,
                "last_error_message": "stale error",
                "pending_update_count": 0,
            }
        }), patch.object(telegram_feedback.telegram_io, "process_link_updates") as polling:
            telegram_feedback.main()
        polling.assert_not_called()

        with patch.object(telegram_feedback.telegram_io, "telegram", return_value={
            "ok": True, "result": {"url": ""}
        }), patch.object(telegram_feedback.telegram_io, "process_link_updates") as polling:
            telegram_feedback.main()
        polling.assert_called_once_with()

    def test_telegram_feedback_reports_an_active_delivery_backlog(self):
        with patch.object(telegram_feedback.telegram_io, "telegram", return_value={
            "ok": True, "result": {
                "url": "https://example.supabase.co/functions/v1/telegram-feedback-webhook",
                "last_error_date": 1,
                "last_error_message": "delivery failed",
                "pending_update_count": 2,
            }
        }), patch.object(telegram_feedback.telegram_io, "process_link_updates") as polling:
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                telegram_feedback.main()
        polling.assert_not_called()

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

    def test_admin_can_trigger_a_protected_manual_scan(self):
        html = (ROOT / "site" / "index.html").read_text()
        app_js = (ROOT / "site" / "app.js").read_text()
        function = (ROOT / "supabase" / "functions" / "admin-scan" / "index.ts").read_text()
        workflow = (ROOT / ".github" / "workflows" / "scan.yml").read_text()
        self.assertIn('id="scanNowButton"', html)
        self.assertIn('id="scanNowMessage"', html)
        self.assertIn('requestImmediateScan', app_js)
        self.assertIn("functions/v1/admin-scan", app_js)
        self.assertIn("profile?.role !== 'admin'", function)
        self.assertIn("profile?.status !== 'active'", function)
        self.assertIn("GITHUB_ACTIONS_TOKEN", function)
        self.assertIn("/actions/workflows/${workflow}/dispatches", function)
        self.assertIn("reserve_scan_slot", function)
        self.assertIn("reserved_run_id", function)
        self.assertIn("run_id: reservedRun", function)
        self.assertNotIn("'Access-Control-Allow-Origin': '*'", function)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("reserved_run_id:", workflow)
        self.assertIn("FORCE_SCAN", workflow)

    def test_supabase_functions_have_a_pinned_deployment_workflow(self):
        workflow = (ROOT / ".github" / "workflows" / "supabase-functions.yml").read_text()
        readme = (ROOT / "README.md").read_text()
        self.assertIn('"supabase/functions/**"', workflow)
        self.assertIn("SUPABASE_ACCESS_TOKEN", workflow)
        self.assertIn("SUPABASE_PROJECT_REF", workflow)
        self.assertIn("functions deploy telegram-auth", workflow)
        self.assertIn("functions deploy admin-scan", workflow)
        self.assertIn("SUPABASE_ACCESS_TOKEN", readme)

    def test_round_trip_and_scan_limits_are_consistent_across_layers(self):
        app = (ROOT / "site" / "app.js").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        schema = (ROOT / "supabase" / "schema.sql").read_text()
        migration = (ROOT / "supabase" / "migrations" / "20260802000200_round_trip_and_adaptive_scan.sql").read_text()
        self.assertIn("MAX_MONITOR_COMBINATIONS = 5000", scanner_source)
        self.assertIn("MAX_MONITOR_COMBINATIONS = 5000", app)
        self.assertIn("maksymalnie 5000", schema)
        self.assertIn("maksymalnie 5000", migration)
        self.assertIn("return_to_date <= from_date", schema)
        self.assertIn("return_to_date <= from_date", migration)

    def test_frontend_bumps_script_cache_after_markup_change(self):
        html = (ROOT / "site" / "index.html").read_text()
        self.assertIn('app.js?v=20260803-16', html)
        self.assertIn('styles.css?v=20260803-10', html)

    def test_frontend_date_picker_has_forward_only_constraints(self):
        app = (ROOT / "site" / "app.js").read_text()
        self.assertIn("from.min = today", app)
        self.assertIn("to.min = from.value", app)
        self.assertIn("returnFrom.min = minimumReturn", app)
        self.assertIn("returnTo.min = returnFrom.value || minimumReturn", app)
        self.assertNotIn("returnFrom > to", app)

    def test_admin_user_actions_are_compact(self):
        styles = (ROOT / "site" / "styles.css").read_text()
        self.assertIn(".user-row .secondary,.user-row .danger", styles)
        self.assertIn("font-size:12px", styles)

    def test_quality_features_are_wired_across_scanner_database_and_panel(self):
        app = (ROOT / "site" / "app.js").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        migration = (ROOT / "supabase" / "migrations" / "20260802000300_quality_history_mutes.sql").read_text()
        self.assertIn("offer_price_history", app)
        self.assertIn("offer_price_history", scanner_source)
        self.assertIn("mark_stale_offers", scanner_source)
        self.assertIn("offer_price_history", migration)
        self.assertIn("scanHistory", (ROOT / "site" / "index.html").read_text())

    def test_bounded_retention_cleanup_is_wired(self):
        migration = (ROOT / "supabase" / "migrations" / "20260802000400_reliability_retention.sql").read_text()
        workflow = (ROOT / ".github" / "workflows" / "cleanup.yml").read_text()
        cleanup = (ROOT / "scanner" / "cleanup.py").read_text()
        self.assertIn("row_number() over (partition by offer_id", migration)
        self.assertIn("interval '30 days'", migration)
        self.assertIn("cleanup_retention", migration)
        self.assertIn("python scanner/cleanup.py", workflow)
        self.assertIn("rpc/cleanup_retention", cleanup)

    def test_stale_offer_cleanup_skips_legacy_schema_without_repeating_http_400(self):
        previous = scanner.STALE_SCHEMA_SUPPORTED
        scanner.STALE_SCHEMA_SUPPORTED = None
        error = urllib.error.HTTPError("https://supabase.test", 400, "missing column", {}, None)
        try:
            with patch.object(scanner, "api", side_effect=error) as api:
                scanner.mark_stale_offers()
                scanner.mark_stale_offers()
            self.assertEqual(api.call_count, 1)
            self.assertFalse(scanner.STALE_SCHEMA_SUPPORTED)
        finally:
            scanner.STALE_SCHEMA_SUPPORTED = previous

    def test_stale_marking_requires_a_complete_healthy_google_scan(self):
        healthy = dict(
            selected_tasks=3, total_tasks=3, executed_tasks=3,
            successful_google_tasks=3, failed_google_tasks=0,
            blocked=False, source_degraded=False,
            source_capacity_reached=False, runtime_limit_reached=False,
            sync_errors=[], task_errors=[],
        )
        self.assertTrue(scanner.can_mark_stale_after_scan(**healthy))
        for field, value in {
            "selected_tasks": 2,
            "executed_tasks": 2,
            "successful_google_tasks": 2,
            "failed_google_tasks": 1,
            "blocked": True,
            "source_degraded": True,
            "source_capacity_reached": True,
            "runtime_limit_reached": True,
            "sync_errors": ["queue error"],
            "task_errors": ["write error"],
        }.items():
            broken = dict(healthy)
            broken[field] = value
            self.assertFalse(scanner.can_mark_stale_after_scan(**broken), field)

    def test_stale_marking_rejects_a_partial_global_queue(self):
        self.assertFalse(scanner.can_mark_stale_after_scan(
            selected_tasks=3, total_tasks=8, executed_tasks=3,
            successful_google_tasks=3, failed_google_tasks=0,
            blocked=False, source_degraded=False,
            source_capacity_reached=False, runtime_limit_reached=False,
            sync_errors=[], task_errors=[]))

    def test_latest_retention_removes_old_details_after_durable_aggregation(self):
        migration = (ROOT / "supabase" / "migrations" / "20260802000600_durable_preferences.sql").read_text()
        cleanup = migration.split("-- Detailed past results are disposable", 1)[1]
        self.assertIn("delete from public.user_matches", cleanup)
        self.assertIn("offer.travel_date < current_date - 7", cleanup)
        self.assertNotIn("match.feedback is null", cleanup)
        self.assertNotIn("saved_feedback.match_id = match.id", cleanup)
        self.assertIn("'matches_deleted', matches_deleted", cleanup)

    def test_feedback_is_aggregated_into_a_durable_cross_monitor_profile(self):
        migration = (ROOT / "supabase" / "migrations" / "20260802000600_durable_preferences.sql").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("create table if not exists public.user_preference_signals", migration)
        self.assertIn("capture_feedback_preference", migration)
        self.assertIn("Existing reactions become part of the durable profile", migration)
        self.assertIn("preference_signals_owner_read", migration)
        self.assertIn("Detailed past results are disposable", migration)
        self.assertIn("offer.travel_date < current_date - 7", migration)
        self.assertIn("on conflict (user_id, dimension, value, cabin) do nothing", migration)
        self.assertIn('fetch_all_rows("user_preference_signals"', scanner_source)

    def test_preference_integrity_migration_repairs_saturated_scores(self):
        migration = (ROOT / "supabase" / "migrations" / "20260802000700_preference_integrity.sql").read_text()
        self.assertIn("preference_signal_score", migration)
        self.assertIn("positive_count + positive_delta", migration)
        self.assertIn("negative_count + negative_delta", migration)
        self.assertIn("where score is distinct from public.preference_signal_score", migration)

    def test_atomic_queue_migration_is_idempotent_and_private(self):
        migration = (ROOT / "supabase" / "migrations" / "20260802000800_atomic_scan_queue.sql").read_text()
        self.assertIn("pg_advisory_xact_lock", migration)
        self.assertIn("on conflict do nothing", migration)
        self.assertIn("return_date is not distinct from", migration)
        self.assertIn("revoke all on function public.sync_monitor_scan_items", migration)
        self.assertIn("grant execute on function public.sync_monitor_scan_items(uuid, jsonb) to service_role", migration)

    def test_source_structure_errors_are_not_reported_as_google_blocks(self):
        source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("source_degraded = True", source)
        self.assertIn('"error" if source_degraded', source)
        self.assertIn("raise ScanSourceRun", source)

    def test_frontend_and_scanner_have_legacy_database_fallbacks(self):
        app = (ROOT / "site" / "app.js").read_text()
        scanner_source = (ROOT / "scanner" / "friends_scanner.py").read_text()
        self.assertIn("Compatibility with databases", app)
        self.assertIn("legacy_payload.pop(\"verification_status\"", scanner_source)

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
