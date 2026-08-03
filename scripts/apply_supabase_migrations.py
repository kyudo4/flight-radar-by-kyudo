#!/usr/bin/env python3
"""Apply new production migrations without replaying the legacy baseline.

The first private deployment was bootstrapped in the Supabase SQL editor, so
the remote migration ledger is not guaranteed to contain every historical
file. New migrations are therefore applied through Supabase's Management API
and recorded in the same ledger after successful execution. Each migration is
idempotent and is applied in filename order.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_REF = os.environ["SUPABASE_PROJECT_REF"]
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
MIGRATION_DIR = Path(os.environ.get("SUPABASE_MIGRATION_DIR", "supabase/migrations"))
MIN_VERSION = os.environ.get("SUPABASE_MIGRATION_MIN_VERSION", "20260803")
API_URL = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
VERSION_RE = re.compile(r"^(\d{14})_(.+)\.sql$")
DIRECT_CONNECTION = None
POOLER_REGIONS = (
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "us-east-1",
    "us-west-1",
    "ca-central-1",
    "sa-east-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-south-1",
)


def _detail(body):
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return "request rejected"
    if isinstance(payload, dict):
        for key in ("message", "error", "error_description"):
            if payload.get(key):
                return str(payload[key])[:240]
    return "request rejected"


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
        raise RuntimeError(
            f"Supabase Management API HTTP {exc.code}: {_detail(exc.read())}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Supabase Management API connection failed: {exc}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(
            f"Supabase Management API rejected SQL: {str(payload['error'])[:240]}"
        )
    if isinstance(payload, dict):
        result = payload.get("result", [])
        if isinstance(result, dict):
            return result.get("rows") or result.get("data") or []
        return result if isinstance(result, list) else []
    return payload if isinstance(payload, list) else []


def run_direct_sql(query=None, path=None):
    """Run SQL through psql without ever putting the password in arguments."""
    global DIRECT_CONNECTION
    password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "SUPABASE_DB_PASSWORD is required for the direct database fallback"
        )
    if query is None and path is None:
        raise RuntimeError("direct SQL execution needs a query or file")

    explicit_host = os.environ.get("SUPABASE_DB_HOST")
    if DIRECT_CONNECTION is not None:
        candidates = [DIRECT_CONNECTION]
    elif explicit_host:
        candidates = [
            {
                "host": explicit_host,
                "port": os.environ.get("SUPABASE_DB_PORT", "5432"),
                "user": os.environ.get("SUPABASE_DB_USER", "postgres"),
            }
        ]
    else:
        candidates = [
            {
                "host": f"aws-0-{region}.pooler.supabase.com",
                "port": "6543",
                "user": f"postgres.{PROJECT_REF}",
            }
            for region in POOLER_REGIONS
        ]

    errors = []
    for connection in candidates:
        command = [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            connection["host"],
            "-p",
            connection["port"],
            "-U",
            connection["user"],
            "-d",
            os.environ.get("SUPABASE_DB_NAME", "postgres"),
        ]
        if query is not None:
            command.extend(["-c", query])
        else:
            command.extend(["-f", str(path)])
        environment = os.environ.copy()
        environment["PGPASSWORD"] = password
        environment["PGSSLMODE"] = os.environ.get("SUPABASE_DB_SSLMODE", "require")
        environment["PGCONNECT_TIMEOUT"] = os.environ.get("SUPABASE_DB_CONNECT_TIMEOUT", "5")
        completed = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            DIRECT_CONNECTION = connection
            rows = []
            for line in completed.stdout.splitlines():
                value = line.strip()
                if value:
                    rows.append({"version": value})
            return rows
        detail = (completed.stderr or completed.stdout or "request rejected").strip()
        errors.append(f"{connection['host']}: {detail[:160]}")
        if explicit_host:
            break
    raise RuntimeError(
        "direct PostgreSQL migration failed; tried " + "; ".join(errors)
    )


def migration_files():
    files = []
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if match and match.group(1) >= MIN_VERSION:
            files.append((match.group(1), match.group(2), path))
    return files


def main():
    files = migration_files()
    if not files:
        print("No pending production migrations in the configured range")
        return

    ledger_query = (
        "select version from supabase_migrations.schema_migrations "
        "where version >= '%s' order by version" % MIN_VERSION.replace("'", "''")
    )
    try:
        rows = run_sql(ledger_query)
        execute_sql = run_sql
        execute_file = lambda path: run_sql(path.read_text(encoding="utf-8"))
        print("Using Supabase Management API")
    except RuntimeError as api_error:
        if not os.environ.get("SUPABASE_DB_PASSWORD"):
            raise RuntimeError(
                f"Management API migration failed: {api_error}. "
                "Set SUPABASE_DB_PASSWORD for the direct database fallback."
            ) from api_error
        print(
            "Management API unavailable; using the direct database fallback "
            f"({api_error})"
        )
        rows = run_direct_sql(ledger_query)
        execute_sql = run_direct_sql
        execute_file = lambda path: run_direct_sql(path=path)
    applied = {
        str(row.get("version"))
        for row in rows
        if isinstance(row, dict) and row.get("version") is not None
    }
    for version, name, path in files:
        if version in applied:
            print(f"Already applied {version}")
            continue
        execute_file(path)
        safe_version = version.replace("'", "''")
        safe_name = name.replace("'", "''")
        execute_sql(
            "insert into supabase_migrations.schema_migrations(version, name, statements) "
            f"values ('{safe_version}', '{safe_name}', ARRAY[]::text[]) "
            "on conflict (version) do nothing"
        )
        print(f"Applied {version}")


if __name__ == "__main__":
    main()
