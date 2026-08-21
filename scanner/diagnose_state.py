#!/usr/bin/env python3
"""Bounded administrator-only diagnostics for monitor coverage and matches."""
from datetime import datetime, timezone

import friends_scanner as scanner


def iso_due(value):
    if not value:
        return True
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def main():
    profiles = scanner.api("GET", "profiles", params={
        "status": "eq.active", "select": "id", "limit": "20",
    })
    user_ids = [row["id"] for row in profiles]
    monitors = scanner.fetch_all_rows("monitors", {
        "status": "eq.active",
        "user_id": "in.(%s)" % ",".join(user_ids) if user_ids else "in.(00000000-0000-0000-0000-000000000000)",
        "select": "id,name,user_id,filters,last_scanned_at,next_scan_at,filters_changed_at,queue_generation",
        "order": "created_at.asc,id.asc",
    })
    print("DIAGNOSTYKA aktywne_monitory=%d" % len(monitors))
    for monitor in monitors:
        monitor_id = monitor["id"]
        items = scanner.fetch_all_rows("monitor_scan_items", {
            "monitor_id": "eq." + monitor_id,
            "select": "last_scanned_at,next_scan_at,origin,destination,travel_date,return_date,cabin",
            "order": "travel_date.asc,id.asc",
        })
        matches = scanner.fetch_all_rows("user_matches", {
            "monitor_id": "eq." + monitor_id,
            "select": "id,visible,notified_at,flight_offers(route,origin,destination,travel_date,return_date,cabin,airline_name,price_pln,duration_minutes,stops,verification_status,last_seen_at)",
            "order": "updated_at.desc,id.desc",
        })
        filters = monitor.get("filters") or {}
        never = sum(1 for item in items if not item.get("last_scanned_at"))
        due = sum(1 for item in items if iso_due(item.get("next_scan_at")))
        visible = [row for row in matches if row.get("visible", True)]
        current = [row for row in visible if (row.get("flight_offers") or {}).get("verification_status") == "verified"]
        print(
            "MONITOR id=%s nazwa=%r trasa=%s->%s daty=%s..%s klasy=%s budzet=%s czas=%s przesiadki=%s kolejka=%d nigdy=%d zalegle=%d dopasowania=%d widoczne=%d aktualne=%d ostatni=%s nastepny=%s zmiana=%s generacja=%s"
            % (
                monitor_id[:8], monitor.get("name"),
                ",".join(filters.get("origins") or []), ",".join(filters.get("destinations") or []),
                filters.get("from"), filters.get("to"),
                ",".join(filters.get("cabins") or [filters.get("cabin") or ""]),
                filters.get("budget_pln"), filters.get("max_duration_h"), filters.get("max_stops"),
                len(items), never, due, len(matches), len(visible), len(current),
                monitor.get("last_scanned_at"), monitor.get("next_scan_at"),
                monitor.get("filters_changed_at"), monitor.get("queue_generation"),
            )
        )
        for row in current[:12]:
            offer = row.get("flight_offers") or {}
            print(
                "  OFERTA %s %s %s %s PLN %s min %s przesiadek status=%s powiadomiono=%s"
                % (
                    offer.get("route"), offer.get("travel_date"), offer.get("airline_name"),
                    offer.get("price_pln"), offer.get("duration_minutes"), offer.get("stops"),
                    offer.get("verification_status"), bool(row.get("notified_at")),
                )
            )


if __name__ == "__main__":
    main()
