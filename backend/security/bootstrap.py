"""CLI helper for explicit initial OWNER enrollment (no hardcoded passwords)."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from backend.security.auth import AuthManager
from backend.security.config import load_security_settings
from backend.storage.duckdb_store import DuckDBStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enroll initial OWNER for The Certainty Stack")
    parser.add_argument("--db-path", default=os.environ.get("CERTAINTY_DUCKDB_PATH", "data/certainty_stack.duckdb"))
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", default=None)
    parser.add_argument("--password", default=None, help="Omit to prompt interactively (recommended)")
    parser.add_argument("--bootstrap-token", default=os.environ.get("CERTAINTY_BOOTSTRAP_TOKEN", ""))
    args = parser.parse_args(argv)

    password = args.password
    if not password:
        password = getpass.getpass("Initial OWNER password (>=12 chars, 3 char classes): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            return 1

    store = DuckDBStore(db_path=args.db_path)
    try:
        settings = load_security_settings()
        auth = AuthManager(store=store, settings=settings)
        owner = auth.bootstrap_owner(
            username=args.username,
            password=password,
            email=args.email,
            bootstrap_token=args.bootstrap_token,
            ip_address="cli-bootstrap",
        )
        print(f"Enrolled OWNER '{owner['username']}' ({owner['user_id']}) in {store.db_path}")
        return 0
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
