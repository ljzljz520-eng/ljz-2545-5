"""用户锁定段、替代候选采用、追溯 API、离线包缺口。"""
from fastapi.testclient import TestClient

from conftest import load_scenario, push, make_plan, get_plan, seg_by_slot
from app.db import tx
from app.services import plans as plans_svc


def test_locked_segment_blocks_shift(fresh_db):
    load_scenario("default")
    pid = make_plan()
    plan = get_plan(pid)
    mf = seg_by_slot(plan, "morning_ferry")
    seg_id = mf["id"]
    # 锁定进岛段
    with tx() as (conn, cur):
        plans_svc.lock_segment(cur, seg_id, True)
    # 推送新时刻（08:00 -> 07:30），增量重算
    push({"source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
          "payload": {"ferries": [
              {"mode": "ferry", "route_code": "F1", "date": "2026-10-03",
               "depart_place": "MAIN_DOCK", "arrive_place": "ISLAND_DOCK",
               "depart_at": "2026-10-03T07:30:00+08:00", "arrive_at": "2026-10-03T08:15:00+08:00",
               "status": "scheduled"}], "shuttles": []}})
    with tx() as (conn, cur):
        res = plans_svc.recompute_incremental(
            cur, pid, [{"change_type": "ferry_time"}], 1)
    assert any(r.get("locked") and r["slot_code"] == "morning_ferry" for r in res["recomputed"])
    # 时刻保持旧值
    assert seg_by_slot(get_plan(pid), "morning_ferry")["starts_at"][11:16] == "08:00"


def test_apply_alternative_cascades(fresh_db):
    load_scenario("ferry_cancel")
    pid = make_plan()
    with tx() as (conn, cur):
        cur.execute("SELECT id FROM segments WHERE plan_id=%s AND slot_code='morning_ferry'",
                    (pid,))
        seg_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM alternatives WHERE segment_id=%s AND record_ref->>'route_code'='F1B'",
                    (seg_id,))
        alt_id = cur.fetchone()["id"]
        plans_svc.apply_alternative(cur, seg_id, alt_id)
    plan = get_plan(pid)
    mf = seg_by_slot(plan, "morning_ferry")
    assert mf["state"] in ("feasible", "locked")
    assert mf["starts_at"][11:16] == "09:30"


def test_trace_api(client):
    load = client.post("/api/admin/ingest-file", json={"path": "data/samples/default"})
    assert load.status_code == 200
    gen = client.post("/api/plans/generate", json={"date": "2026-10-03"}).json()
    plan = client.get(f"/plan?plan_id={gen['plan_id']}")
    assert plan.status_code == 200
    # 取潮汐段 id
    from app.services.query import plan_payload
    from app.db import tx as _tx
    with _tx() as (conn, cur):
        p = plan_payload(cur, gen["plan_id"])
    reef_id = next(s["id"] for s in p["segments"] if s["slot_code"] == "reef_window")
    tr = client.get(f"/api/trace/{reef_id}").json()
    types = {e["type"] for e in tr["segment"]["evidence"]}
    assert {"place", "tide_point", "tide_obs"} <= types
    assert tr["source_versions"]  # 至少一个来源版本可追溯


def test_offline_bundle_gap(client):
    client.post("/api/admin/ingest-file", json={"path": "data/samples/default"})
    r = client.post("/api/admin/offline-bundle", json={"date": "2026-10-03"})
    assert r.status_code == 200
    assert r.json()["gap_sources"] == []
    # 冻结后接入新版本 -> 再生成时出现更新缺口
    client.post("/api/admin/ingest-push", json={
        "source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
        "payload": {"ferries": [], "shuttles": []}})
    r2 = client.post("/api/admin/offline-bundle", json={"date": "2026-10-03"}).json()
    assert "ferry.official" in r2["gap_sources"]
    page = client.get("/offline?date=2026-10-03")
    assert "不保证现实交通可用" in page.text
    assert "更新缺口" in page.text


def test_pages_render(client):
    client.post("/api/admin/ingest-file", json={"path": "data/samples/default"})
    for url in ("/", "/plan", "/admin", "/docs-site", "/open-data/places.csv",
                "/open-data/ferries.json", "/health"):
        r = client.get(url)
        assert r.status_code == 200, url
