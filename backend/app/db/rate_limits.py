"""Bounded, persistent request counters shared by local app processes."""

import hashlib
import ipaddress
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from backend.app.db.config import Database, PostgresDatabase


MAX_COUNTERS = 4096


@dataclass(frozen=True)
class Budget:
    name: str
    limit: int
    seconds: int


# Per-network and whole-service limits both apply. Fixed windows start on first use.
GENERAL = (Budget("network", 120, 60), Budget("global", 600, 60))
POLICIES = {
    "create": (Budget("network-create", 5, 600), Budget("global-create", 30, 3600)),
    "evidence": (Budget("network-evidence", 30, 60), Budget("global-evidence", 180, 60)),
    "explanation": (Budget("network-explanation", 6, 60), Budget("global-explanation", 20, 60)),
}


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, *, capacity: bool = False):
        self.retry_after = max(1, retry_after)
        super().__init__(
            ("Request limiting is temporarily at capacity. " if capacity else "Too many requests. ")
            + f"Try again in {self.retry_after} seconds."
        )
        self.status_code = 503 if capacity else 429


def rate_database(database: Database) -> Database:
    if isinstance(database, PostgresDatabase):
        return database
    return database.with_name(database.stem + ".rates.sqlite3")


def initialise_rate_limits(database: Database) -> None:
    if isinstance(database, PostgresDatabase):
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=2)) as connection:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS request_counters ("
                "bucket TEXT PRIMARY KEY NOT NULL, hits INTEGER NOT NULL, "
                "expires_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS request_counter_expiry ON request_counters(expires_at)"
            )


def network_key(address: str | None) -> str:
    try:
        ip = ipaddress.ip_address(address or "")
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        value = (
            str(ipaddress.ip_network((ip, 64), strict=False))
            if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        )
    except ValueError:
        value = "unknown-peer"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def consume_request(
    database: Database,
    network: str,
    category: str | None,
    *,
    now: float | None = None,
) -> None:
    if isinstance(database, PostgresDatabase):
        from backend.app.db import postgres
        return postgres.consume_request(database, network, category, now=now)
    timestamp = time.time() if now is None else now
    budgets = (*GENERAL, *POLICIES.get(category, ()))
    with closing(sqlite3.connect(database, timeout=2)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM request_counters WHERE expires_at <= ?", (timestamp,))
            pending = []
            retry = 0
            missing = 0
            for budget in budgets:
                key = budget.name + ":" + (network if budget.name.startswith("network") else "all")
                row = connection.execute(
                    "SELECT hits, expires_at FROM request_counters WHERE bucket = ?", (key,),
                ).fetchone()
                if row is None:
                    missing += 1
                    pending.append((key, 1, timestamp + budget.seconds))
                else:
                    if row[0] >= budget.limit:
                        retry = max(retry, math.ceil(row[1] - timestamp))
                    pending.append((key, row[0] + 1, row[1]))
            if retry:
                raise RateLimitExceeded(retry)
            count = connection.execute("SELECT COUNT(*) FROM request_counters").fetchone()[0]
            if count + missing > MAX_COUNTERS:
                raise RateLimitExceeded(60, capacity=True)
            connection.executemany(
                "INSERT INTO request_counters (bucket, hits, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(bucket) DO UPDATE SET hits = excluded.hits, expires_at = excluded.expires_at",
                pending,
            )
