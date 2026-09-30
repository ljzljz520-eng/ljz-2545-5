"""五类关键场景：船班取消、来源矛盾、地点改坐标、旧缓存回滚、跨日接驳，
另加潮汐缺测与临时封闭规则。"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_scenario, push, make_plan, get_plan, seg_by_slot, conn_between
from app.db import tx
from app.services import ingest as ingest_svc
from app.services import plans as plans_svc


def test_ferry_cancel_infeasible_and_alternative(fresh_db):
    load_scenario("ferry_cancel")
    pid = make_plan()
    plan = get_plan(pid)
    mf = seg_by_slot(plan, "morning_ferry")
    # F1 取消 -> 不可行（不是整条行程作废）
    assert mf["state"] == "infeasible"
    assert "ferry_cancelled" in mf["infeasible_reason"] or "取消" in mf["infeasible_reason"]
    # F1B 是同日备选
    with tx() as (conn, cur):
        cur.execute("""SELECT a.* FROM alternatives a JOIN segments s ON s.id=a.segment_id
                       WHERE s.plan_id=%s AND s.slot_code='morning_ferry'""", (pid,))
        alts = cur.fetchall()
    assert any(a["record_ref"]["route_code"] == "F1B" for a in alts)

    # 局部重算（模拟数据变更）：直接命中进岛段，下游段被连带标记
    with tx() as (conn, cur):
        res = plans_svc.recompute_incremental(
            cur, pid, [{"change_type": "ferry_cancel", "route_code": "F1"}], 1)
    assert any(r["slot_code"] == "morning_ferry" for r in res["recomputed"])
    plan2 = get_plan(pid)
    assert plan2["plan"]["generated_mode"] == "incremental"


def test_conflicting_sources_official_wins(fresh_db):
    load_scenario("conflict")
    pid = make_plan()
    plan = get_plan(pid)
    mf = seg_by_slot(plan, "morning_ferry")
    # 官方 08:00 胜出（社区 08:30 败）
    assert mf["starts_at"][11:16] == "08:00"
    # 证据链记录采用的来源版本
    vids = {e.get("source_version_id") for e in mf["evidence"]}
    with tx() as (conn, cur):
        cur.execute("SELECT id FROM source_versions WHERE source_key='ferry.official'")
        official = {r["id"] for r in cur.fetchall()}
    assert vids & official


def test_place_coord_change_blocks_tide(fresh_db):
    load_scenario("coord_change")
    pid = make_plan()
    plan = get_plan(pid)
    reef = seg_by_slot(plan, "reef_window")
    assert reef["state"] == "infeasible"
    assert "tide_coord_mismatch" in reef["reason_codes"]
    # 证据里必须能看到距离
    ev = reef["evidence"]
    tp_ev = next(e for e in ev if e["type"] == "tide_point")
    assert tp_ev["distance_km"] > 2.0


def test_rollback_restores_schedule(fresh_db):
    load_scenario("default")
    pid = make_plan()
    # 接入 r3 取消
    push({"source_key": "ferry.official", "revision": "2026-10-03-r3", "priority": 10,
          "payload": {"ferries": [
              {"mode": "ferry", "route_code": "F1", "date": "2026-10-03",
               "depart_place": "MAIN_DOCK", "arrive_place": "ISLAND_DOCK",
               "depart_at": "2026-10-03T08:00:00+08:00", "arrive_at": "2026-10-03T08:45:00+08:00",
               "status": "cancelled"}], "shuttles": []}})
    with tx() as (conn, cur):
        plans_svc.recompute_incremental(cur, pid, [{"change_type": "ferry_cancel"}], 1)
    assert seg_by_slot(get_plan(pid), "morning_ferry")["state"] == "infeasible"
    # 回滚到 r1，再增量重算 -> 恢复可行
    with tx() as (conn, cur):
        ingest_svc.rollback_source(cur, "ferry.official", "2026-10-03-r1")
        plans_svc.recompute_incremental(cur, pid, [{"change_type": "ferry_time"}], 1)
    restored = seg_by_slot(get_plan(pid), "morning_ferry")
    assert restored["state"] in ("feasible", "locked")
    assert restored["starts_at"][11:16] == "08:00"


def test_cross_day_connection(fresh_db):
    load_scenario("crossday")
    pid = make_plan()
    plan = get_plan(pid)
    conn = conn_between(plan, "morning_ferry", "dock_to_lh")
    assert conn["cross_day"] is True
    # 23:20 -> 次日 00:15 = 55 分钟缓冲
    assert conn["gap_minutes"] == 55
    assert conn["feasible"] is True
    mf = seg_by_slot(plan, "morning_ferry")
    assert str(mf["starts_at"])[:10] == "2026-10-03"
    dl = seg_by_slot(plan, "dock_to_lh")
    assert str(dl["starts_at"])[:10] == "2026-10-04"


def test_tide_missing_not_passable(fresh_db):
    load_scenario("default")
    # 把低潮改为缺测
    push({"source_key": "tide.marine_bureau", "revision": "2026-10-03-r2", "priority": 10,
          "payload": {"tides": [
              {"tide_point_code": "TP-REEF-01", "observed_at": "2026-10-03T06:10:00+08:00",
               "level_cm": 150, "kind": "high"},
              {"tide_point_code": "TP-REEF-01", "observed_at": "2026-10-03T12:20:00+08:00",
               "level_cm": None, "kind": "low"},
              {"tide_point_code": "TP-REEF-01", "observed_at": "2026-10-03T18:35:00+08:00",
               "level_cm": 165, "kind": "high"}]}})
    pid = make_plan()
    reef = seg_by_slot(get_plan(pid), "reef_window")
    assert reef["state"] == "infeasible"
    assert "tide_data_missing" in reef["reason_codes"]


def test_closure_only_makes_segment_pending(fresh_db):
    load_scenario("default")
    push({"source_key": "closure.island_office", "revision": "2026-10-01-r2", "priority": 15,
          "payload": {"closures": [
              {"closure_key": "CL-LH-01", "target_type": "place", "target_code": "LIGHTHOUSE",
               "reason": "灯室维护", "starts_at": "2026-10-03T09:00:00+08:00",
               "ends_at": "2026-10-03T12:00:00+08:00"}]}})
    pid = make_plan()
    plan = get_plan(pid)
    lh = seg_by_slot(plan, "lighthouse_visit")
    assert lh["state"] == "pending_adjustment"
    assert "封闭" in lh["infeasible_reason"]
    # 其它段不受「封闭」影响，仍可行
    assert seg_by_slot(plan, "morning_ferry")["state"] == "feasible"
    assert seg_by_slot(plan, "return_ferry")["state"] == "feasible"

    # 增量重算：灯塔段之后接驳出现 knock_on 连带说明
    with tx() as (conn, cur):
        r = plans_svc.recompute_incremental(
            cur, pid, [{"change_type": "closure_added", "entity_key": "LIGHTHOUSE",
                        "closure_key": "CL-LH-01"}], 1)
    plan2 = get_plan(pid)
    after = conn_between(plan2, "lighthouse_visit", "reef_window")
    assert after["knock_on"] is True
