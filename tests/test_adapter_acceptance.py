"""数据适配验收 AC-1..AC-9（对应 docs/design.md §4.1）。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_db, load_scenario, push, SCEN
from app.db import tx
from app.adapters import JsonFileAdapter, DictAdapter, AdapterError
from app.services import ingest as ingest_svc


def test_ac1_missing_fields_rejected(client):
    r = client.post("/api/admin/ingest-push", json={"revision": "x"})
    assert r.status_code == 422


def test_ac2_idempotent(fresh_db):
    r1 = load_scenario("default")
    v_first = [x[2] for x in r1]
    r2 = load_scenario("default")
    v_second = [x[2] for x in r2]
    assert v_first == v_second  # 同内容重复接收不新增版本
    with tx() as (conn, cur):
        cur.execute("SELECT count(*) c FROM source_versions")
        assert cur.fetchone()["c"] == 4


def test_ac3_version_chain(fresh_db):
    load_scenario("default")
    push({"source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
          "payload": {"ferries": [], "shuttles": []}})
    with tx() as (conn, cur):
        cur.execute("""SELECT v.revision, v.superseded_by, n.revision AS newer
                       FROM source_versions v LEFT JOIN source_versions n ON n.id=v.superseded_by
                       WHERE v.source_key='ferry.official' ORDER BY v.id""")
        rows = cur.fetchall()
    assert rows[0]["newer"] is not None
    assert rows[-1]["superseded_by"] is None


def test_ac4_conflict_adjudication(fresh_db):
    load_scenario("conflict")  # 官方 10 vs 社区 5，F1 时刻矛盾
    with tx() as (conn, cur):
        cur.execute("SELECT count(*) c FROM data_conflicts WHERE entity_key LIKE 'ferry|F1|%'")
        assert cur.fetchone()["c"] >= 1
        cur.execute("""SELECT dc.winner_version_id, sv.priority FROM data_conflicts dc
                       JOIN source_versions sv ON sv.id=dc.winner_version_id""")
        win = cur.fetchone()
        assert win["priority"] == 10  # 官方胜


def test_ac5_cancel_change(fresh_db):
    load_scenario("default")
    results = push({"source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
                    "payload": {"ferries": [
                        {"mode": "ferry", "route_code": "F1", "date": "2026-10-03",
                         "depart_place": "MAIN_DOCK", "arrive_place": "ISLAND_DOCK",
                         "depart_at": "2026-10-03T08:00:00+08:00",
                         "arrive_at": "2026-10-03T08:45:00+08:00", "status": "cancelled"}]}})
    types = {c["change_type"] for c in results[0]["changes"]}
    assert "ferry_cancel" in types


def test_ac6_closure_change(fresh_db):
    load_scenario("default")
    results = push({"source_key": "closure.island_office", "revision": "2026-10-01-r2",
                    "priority": 15, "payload": {"closures": [
                        {"closure_key": "CL-LH-01", "target_type": "place",
                         "target_code": "LIGHTHOUSE", "reason": "灯室维护",
                         "starts_at": "2026-10-03T09:00:00+08:00",
                         "ends_at": "2026-10-03T12:00:00+08:00"}]}})
    assert any(c["change_type"] == "closure_added" for c in results[0]["changes"])


def test_ac7_tide_missing_change(fresh_db):
    raw = {"source_key": "tide.marine_bureau", "revision": "2026-10-03-r2", "priority": 10,
           "payload": {"tides": [
               {"tide_point_code": "TP-REEF-01", "observed_at": "2026-10-03T12:20:00+08:00",
                "level_cm": None, "kind": "low"}]}}
    results = push(raw)
    assert any(c["change_type"] == "tide_missing" for c in results[0]["changes"])


def test_ac8_rollback(fresh_db):
    load_scenario("default")
    push({"source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
          "payload": {"ferries": [
              {"mode": "ferry", "route_code": "F1", "date": "2026-10-03",
               "depart_place": "MAIN_DOCK", "arrive_place": "ISLAND_DOCK",
               "depart_at": "2026-10-03T08:00:00+08:00", "arrive_at": "2026-10-03T08:45:00+08:00",
               "status": "cancelled"}], "shuttles": []}})
    with tx() as (conn, cur):
        tid = ingest_svc.rollback_source(cur, "ferry.official", "2026-10-03-r1")
        cur.execute("SELECT status FROM ferry_records WHERE route_code='F1' ORDER BY source_version_id")
        # 权威展示：裁决视图取最高优先级来源中该版本；回滚后该行仍是 r1 scheduled
        cur.execute("""SELECT status FROM ferry_records fr
                       JOIN source_versions sv ON sv.id=fr.source_version_id
                       WHERE fr.route_code='F1' AND sv.superseded_by IS NULL""")
        row = cur.fetchone()
        assert row["status"] == "scheduled"
        assert tid


def test_ac9_pluggable_adapters():
    from app.adapters import BaseSourceAdapter
    raw = {"source_key": "x", "revision": "1", "payload": {}}
    assert list(DictAdapter(raw).bundles())[0].source_key == "x"
    assert list(JsonFileAdapter(SCEN / "default" / "source_tides.json").bundles())[0].source_key
    with pytest.raises(AdapterError):
        list(DictAdapter({"payload": {}}).bundles())
