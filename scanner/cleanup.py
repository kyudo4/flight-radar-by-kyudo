#!/usr/bin/env python3
"""Run bounded-retention cleanup in Supabase."""

from friends_scanner import api


if __name__ == "__main__":
    result = api("POST", "rpc/cleanup_retention", body={})
    print("Retention cleanup:", result)
