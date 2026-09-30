import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LIGHTHOUSE_DSN", "host=/tmp user=node dbname=lighthouse_test port=5432")

from fastapi.testclient import TestClient
from app.db import tx, get_conn
from app.main import app
from app.adapters import JsonFileAdapter, DictAdapter
from app.services import ingest as ingest_svc
from app.services import plans as plans_svc

SCHEMA = (ROOT / "app" / "schema.sql").read_text(encoding="utf-8")
SCEN = ROOT / "data" / "samples"


def _ensure_db():
    import psycopg2
    dsn_admin = "host=/tmp user=node dbname=postgres port=5432"
    conn = psycopg2.connect(dsn_admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname='lighthouse_test'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE lighthouse_test")
    conn.close()


def reset_db():
    with tx() as (conn, cur):
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(SCHEMA)


def load_scenario(name):
    """装载某场景全部文件，返回 [(file, source_key, version_id, changes)]。"""
    out = []
    with tx() as (conn, cur):
        for f in sorted((SCEN / name).glob("*.json")):
            for b in JsonFileAdapter(f).bundles():
                r = ingest_svc.ingest_bundle(cur, b)
                out.append((f.name, b.source_key, r["source_version_id"], r["changes"]))
    return out


def push(raw):
    with tx() as (conn, cur):
        results = []
        for b in DictAdapter(raw).bundles():
            results.append(ingest_svc.ingest_bundle(cur, b))
        return results


def make_plan(date="2026-10-03"):
    with tx() as (conn, cur):
        return plans_svc.generate_static(cur, date)


def get_plan(pid):
    from app.services.query import plan_payload
    with tx() as (conn, cur):
        return plan_payload(cur, pid)


@pytest.fixture(scope="session", autouse=True)
def _db_ready():
    _ensure_db()


@pytest.fixture
def client():
    reset_db()
    return TestClient(app)


@pytest.fixture
def fresh_db():
    reset_db()


def seg_by_slot(plan, slot):
    return next(s for s in plan["segments"] if s["slot_code"] == slot)


def conn_between(plan, slot_a, slot_b):
    return next(c for c in plan["connections"]
                if c["from_slot"] == slot_a and c["to_slot"] == slot_b)
