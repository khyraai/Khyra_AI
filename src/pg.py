"""
pg.py — Shared PostgreSQL connection pool.

Usage:
    from pg import get_conn

    with get_conn() as cur:
        cur.execute("SELECT 1")
        row = cur.fetchone()

The context manager yields a RealDictCursor and automatically
commits on success or rolls back on exception, then returns the
connection to the pool.
"""

import os
import contextlib
import threading

import psycopg2
import psycopg2.pool
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(override=True)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

_MIN_CONN = 2
_MAX_CONN = 25


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = os.getenv("DATABASE_URL", "")
                if not dsn:
                    raise RuntimeError(
                        "[pg] DATABASE_URL is not set. "
                        "Add it to your .env file."
                    )
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    _MIN_CONN, _MAX_CONN, dsn
                )
                print(f"[pg] Connection pool created (min={_MIN_CONN}, max={_MAX_CONN})")
    return _pool


@contextlib.contextmanager
def get_conn():
    """
    Context manager that yields a RealDictCursor.
    Commits on clean exit, rolls back on exception.
    Always returns the underlying connection to the pool.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def close_pool():
    """Call this on application shutdown to release all connections."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        print("[pg] Connection pool closed.")
