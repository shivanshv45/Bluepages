"""The database connection: SQLite locally, Postgres on Supabase.

One schema, two backends. SQLite is not a lesser mode for testing: it is what
runs during development, so the persistence path is exercised on every test run
rather than only when a cloud database happens to be reachable. Postgres is the
deployment target because AgentCore Runtime has no durable local disk.

The differences between the two are small and confined here:

- parameter style, `?` against `%s`
- upsert syntax, both support `ON CONFLICT` but Postgres needs the column list
- booleans, SQLite has none and stores 0/1

Everything above this module writes backend-neutral SQL and lets `Database`
translate. That is why the schema is one file rather than two that drift.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def new_id() -> str:
    """A row id. UUIDs rather than sequences so a row can be created offline.

    Layer 6 runs the pipeline in Lambda, where two invocations can be building
    rows at the same moment against different connections.
    """
    return uuid.uuid4().hex


def now() -> str:
    """UTC, ISO 8601. Stored as text so both backends order it the same way."""
    return datetime.now(UTC).isoformat()


class Cursor(Protocol):
    """The subset of DB-API both backends provide."""

    def execute(self, sql: str, params: Any = ...) -> Any: ...
    def fetchall(self) -> list[Any]: ...
    def fetchone(self) -> Any: ...


class Database:
    """A connection to the element database.

    Use it as a context manager. The transaction commits on a clean exit and
    rolls back on an exception, so a half-written run never persists: a diff
    that failed at scene 80 must not leave 79 scenes looking like a complete
    record.
    """

    def __init__(self, dsn: str | None = None, path: str | Path | None = None) -> None:
        """Open SQLite when given a path, Postgres when given a DSN."""
        if dsn and path:
            raise ValueError("pass either a Postgres dsn or a SQLite path, not both")

        self.dsn = dsn
        self.path = Path(path) if path else None
        self.is_postgres = dsn is not None
        self._conn: Any = None

    # --- connection --------------------------------------------------------

    def connect(self) -> Database:
        if self._conn is not None:
            return self

        if self.dsn is not None:
            import psycopg

            # prepare_threshold=None disables server-side prepared statements.
            # A transaction-mode pooler (Supabase's, on :6543) hands the next
            # query to a different backend session, so a statement prepared on
            # one connection is either missing or name-collides on the next:
            # "prepared statement _pg3_0 already exists". Preparing is a
            # throughput optimisation and these are one-shot reads, so there is
            # nothing to lose by turning it off.
            self._conn = psycopg.connect(self.dsn, prepare_threshold=None)
        else:
            target = str(self.path) if self.path else ":memory:"
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(target)
            self._conn.row_factory = sqlite3.Row
            # Off by default in SQLite, and without it ON DELETE CASCADE is
            # silently ignored, leaving orphan scenes behind a deleted draft.
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Database:
        return self.connect()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._conn is not None:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            self.close()

    # --- statements --------------------------------------------------------

    def _translate(self, sql: str) -> str:
        """Rewrite `?` placeholders for Postgres.

        Naive on purpose: the SQL in this package is written by us and contains
        no literal question marks. It is not a general-purpose translator and
        should not be used on arbitrary input.
        """
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = self._require().cursor()
        cursor.execute(self._translate(sql), params)
        return cursor

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        """Batch insert. One statement for 120 scenes rather than 120."""
        if not rows:
            return
        cursor = self._require().cursor()
        cursor.executemany(self._translate(sql), rows)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a select and return plain dicts, identically on both backends."""
        cursor = self.execute(sql, params)
        rows = cursor.fetchall()
        if not rows:
            return []
        if self.is_postgres:
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in rows]
        return [dict(row) for row in rows]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        self._require().commit()

    def _require(self) -> Any:
        if self._conn is None:
            raise RuntimeError("database is not connected; use `with Database(...) as db`")
        return self._conn

    # --- schema ------------------------------------------------------------

    def create_schema(self) -> None:
        """Create every table. Idempotent, so it is safe on an existing database."""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        if self.is_postgres:
            self._require().cursor().execute(sql)
        else:
            self._require().executescript(sql)
        self.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Column additions to a table that already existed before them.

        `CREATE TABLE IF NOT EXISTS` does nothing for a database that already
        has the table under an older shape, so a new column needs its own
        guarded ALTER TABLE here.
        """
        self._add_column_if_missing("budget", "auto_approve", "REAL NOT NULL DEFAULT 0")

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        if self.is_postgres:
            existing = {
                row["column_name"]
                for row in self.query(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s",
                    (table,),
                )
            }
        else:
            existing = {row["name"] for row in self.query(f"PRAGMA table_info({table})")}
        if column in existing:
            return
        self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.commit()

    def tables(self) -> set[str]:
        """Table names, for checking the schema applied."""
        if self.is_postgres:
            rows = self.query(
                "SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public'"
            )
        else:
            rows = self.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {r["name"] for r in rows}


def open_database(settings: Any = None, path: str | Path | None = None) -> Database:
    """Open the configured database.

    Prefers an explicit path, then Supabase when configured, then a local file
    under the project root. The local default matters: persistence should work
    out of the box during development rather than being gated on cloud setup.
    """
    if path is not None:
        return Database(path=path)

    if settings is None:
        from bluepages.config import get_settings

        settings = get_settings()

    dsn = getattr(settings, "supabase_db_url", None)
    if dsn:
        return Database(dsn=dsn)

    return Database(path=settings.project_root / ".bluepages" / "elements.db")


__all__ = ["Database", "new_id", "now", "open_database"]
