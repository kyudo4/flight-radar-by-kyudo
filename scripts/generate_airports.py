#!/usr/bin/env python3
"""Generate the compact airport lookup used by both the panel and scanner."""

import csv
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: generate_airports.py INPUT.csv OUTPUT.json")
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    airports = {}
    with source.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("iata_code") or "").strip().upper()
            if len(code) != 3 or not code.isalpha():
                continue
            city = (row.get("municipality") or row.get("name") or code).strip()
            airports.setdefault(code, city)
    target.write_text(
        json.dumps(dict(sorted(airports.items())), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(airports)} airport codes")


if __name__ == "__main__":
    main()
