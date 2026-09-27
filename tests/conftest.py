import os
import re
import sqlite3

import pytest


class SQLiteTestConnection:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()

    def execute(self, sql, params=None):
        cleaned_sql = sql.replace("%s", "?")
        cleaned_sql = cleaned_sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        cleaned_sql = cleaned_sql.replace("BYTEA", "BLOB")
        cleaned_sql = cleaned_sql.replace("DOUBLE PRECISION", "REAL")
        cleaned_sql = re.sub(r"ALTER TABLE \w+ DROP COLUMN IF EXISTS \w+;?", "", cleaned_sql)
        cleaned_sql = re.sub(r"DROP INDEX IF EXISTS \w+;?", "", cleaned_sql)
        cleaned_sql = re.sub(r"ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS [^;]+;?", "", cleaned_sql)
        if not cleaned_sql.strip():
            class DummyCursor:
                rowcount = 0

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return DummyCursor()
        return self._conn.execute(cleaned_sql, params or ())

    def executemany(self, sql, seq):
        cleaned_sql = sql.replace("%s", "?")
        return self._conn.executemany(cleaned_sql, seq)

    def close(self):
        pass


if not os.getenv("DATABASE_URL", "").strip():
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY", "test-key-for-unit-tests")

import db

_shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
_shared_conn.execute("PRAGMA foreign_keys = ON")
db.connect = lambda: SQLiteTestConnection(_shared_conn)
