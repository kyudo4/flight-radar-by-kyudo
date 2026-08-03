#!/usr/bin/env python3
"""Apply the idempotent audit migrations through Supabase Management API.

The GitHub Actions token is kept in the runner environment. No database
password or token is written to logs. Only the 20260803 audit migrations are
handled here; older migrations are managed by the existing project history.
"""
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_REF = os.environ["SUPABASE_PROJECT_REF"]
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
MIGRATION_DIR = Path(os.environ.get("SUPABASE_MIGRATION_DIR", "supabase/migrations"))
API_URL = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
VERSION_RE = re.compile(r"^(20260803\d{6})_(.+)\.sql$")


def run_sql(query):
    request = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not print the response body: Supabase may echo SQL fragments.
        raise RuntimeError(f"Supabase Management API returned HTTP {exc.code}") from exc
    if payload.get("error"):
        raise RuntimeError("Supabase Management API rejected the SQL query")
    return payload.get("result") or []


def main():
    migration_files = []
    for path in sorted(MIGRATION_DIR.glob("20260803*.sql")):
        match = VERSION_RE.match(path.name)
        if match:
            migration_files.append((match.group(1), match.group(2), path))
    if not migration_files:
        print("No audit migrations found")
        return

    rows = run_sql(
        "select version from supabase_migrations.schema_migrations "
        "where version like '20260803%' order by version"
    )
    applied = {str(row.get("version")) for row in rows if isinstance(row, dict)}
    for version, name, path in migration_files:
        if version in applied:
            print(f"Already applied {version}")
            continue
        run_sql(path.read_text(encoding="utf-8"))
        # Keep the CLI migration ledger consistent. The audit migrations are
        # idempotent, so a retry after a connection drop is safe as well.
        safe_version = version.replace("'", "''")
        safe_name = name.replace("'", "''")
        run_sql(
            "insert into supabase_migrations.schema_migrations(version, name, statements) "
            f"values ('{safe_version}', '{safe_name}', ARRAY[]::text[]) "
            "on conflict (version) do nothing"
        )
        print(f"Applied {version}")


if __name__ == "__main__":
    main()
