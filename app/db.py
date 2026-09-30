"""PostgreSQL 连接管理（psycopg2, RealDictCursor）。"""
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from .config import DATABASE_URL


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    # 全岛统一展示时区；timestamp with time zone 按此时区读出
    with conn.cursor() as c:
        c.execute("SET TIME ZONE 'Asia/Shanghai'")
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetchall(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def fetchone(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()
