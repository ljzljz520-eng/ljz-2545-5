"""行程生成与局部重算（持久化 plans/segments/connections/impact_edges/alternatives）。"""
from __future__ import annotations
import json
from datetime import datetime, timedelta

from ..db import tx
from .planner import (SLOT_TEMPLATE, DataView, evaluate_segment, evaluate_connections,
                      TIDE_MATCH_KM)
from .ingest import latest_version_ids

PLANNER_VERSION = "planner-1.0"

# 数据变更类型 -> 可能直接受影响的槽位（再向上/向下传播）
CHANGE_TO_SLOTS = {
    "ferry_cancel": {"morning_ferry", "return_ferry", "dock_to_lh", "lh_to_dock"},
    "ferry_time": {"morning_ferry", "return_ferry", "dock_to_lh", "lh_to_dock"},
    "ferry_added": {"morning_ferry", "return_ferry", "dock_to_lh", "lh_to_dock"},
    "closure_added": {"lighthouse_visit", "reef_window",
                      "morning_ferry", "return_ferry", "dock_to_lh", "lh_to_dock"},
    "place_coord": {"reef_window", "lighthouse_visit"},
    "tide_missing": {"reef_window"},
}
SLOT_INDEX = {t["slot_code"]: i for i, t in enumerate(SLOT_TEMPLATE)}


def _dt(v):
    if isinstance(v, datetime) or v is None:
        return v
    return datetime.fromisoformat(str(v))


def _persist_segment(cur, plan_id, pos, ev, locked=False):
    state = "locked" if locked and ev["state"] == "feasible" else ev["state"]
    cur.execute(
        """INSERT INTO segments
           (plan_id,position,slot_code,kind,title,from_place,to_place,
            starts_at,ends_at,state,infeasible_reason,reason_codes,record_ref,evidence,locked)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (plan_id, pos, ev["slot_code"], ev["kind"], ev["title"],
         ev.get("from_place"), ev.get("to_place"),
         ev["starts_at"], ev["ends_at"], state, ev.get("infeasible_reason"),
         ev.get("reason_codes", []),
         json.dumps(ev["record_ref"], ensure_ascii=False, default=str),
         json.dumps(ev["evidence"], ensure_ascii=False, default=str),
         locked and ev["state"] == "feasible"))
    seg_id = cur.fetchone()["id"]
    for alt in ev.get("alternatives", []):
        cur.execute(
            """INSERT INTO alternatives (segment_id,rank,kind,title,starts_at,ends_at,record_ref,evidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (seg_id, alt["rank"], alt["kind"], alt["title"], alt["starts_at"], alt["ends_at"],
             json.dumps(alt["record_ref"], ensure_ascii=False, default=str),
             json.dumps(alt["evidence"], ensure_ascii=False, default=str)))
    return seg_id


def _load_ev(cur, seg_row) -> dict:
    return {
        "id": seg_row["id"], "slot_code": seg_row["slot_code"], "kind": seg_row["kind"],
        "title": seg_row["title"], "from_place": seg_row["from_place"],
        "to_place": seg_row["to_place"],
        "starts_at": _dt(seg_row["starts_at"]), "ends_at": _dt(seg_row["ends_at"]),
        "state": "feasible" if seg_row["state"] == "locked" else seg_row["state"],
        "_locked_state": seg_row["state"],
        "infeasible_reason": seg_row["infeasible_reason"],
        "locked": seg_row["locked"], "evidence": seg_row["evidence"] or [],
        "record_ref": seg_row["record_ref"]}


def generate_static(cur, travel_date: str, title: str = "灯塔一日导览") -> int:
    """一次生成静态行程：全量评估所有段与接驳。"""
    view = DataView(cur, travel_date)
    baseline = latest_version_ids(cur)
    cur.execute(
        """INSERT INTO plans (travel_date,title,status,generated_mode,based_on_versions)
           VALUES (%s,%s,'static_generated','static',%s) RETURNING id""",
        (travel_date, title, json.dumps(baseline)))
    plan_id = cur.fetchone()["id"]

    evs = []
    anchor = None
    for pos, tmpl in enumerate(SLOT_TEMPLATE):
        ev = evaluate_segment(cur, view, tmpl, anchor_arrival=anchor)
        evs.append(ev)
        anchor = ev["ends_at"]
    seg_ids = [_persist_segment(cur, plan_id, i, ev) for i, ev in enumerate(evs)]
    for ev, sid in zip(evs, seg_ids):
        ev["id"] = sid
    _rebuild_connections(cur, plan_id, evs)
    return plan_id


def _rebuild_connections(cur, plan_id, evs, knock_set: set[int] | None = None,
                         upstream_detail: dict[int, str] | None = None):
    cur.execute("DELETE FROM connections WHERE plan_id=%s", (plan_id,))
    conns = evaluate_connections(evs)
    knock_set = knock_set or set()
    for conn, a, b in zip(conns, evs[:-1], evs[1:]):
        knock = b["id"] in knock_set or a["id"] in knock_set
        detail = conn["detail"]
        knock_detail = None
        if knock and not conn["feasible"]:
            knock_detail = (upstream_detail or {}).get(b["id"], "上游段时刻变化，本接驳连带受影响")
        elif knock:
            knock_detail = (upstream_detail or {}).get(b["id"], "上游段时刻变化，接驳时间已连带重算")
        cur.execute(
            """INSERT INTO connections
               (plan_id,from_segment_id,to_segment_id,feasible,gap_minutes,cross_day,
                detail,knock_on,knock_on_detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (plan_id, a["id"], b["id"], conn["feasible"], conn["gap_minutes"],
             conn["cross_day"], detail, knock, knock_detail))
        cid = cur.fetchone()["id"]
        if knock:
            cur.execute(
                """INSERT INTO impact_edges (source_version_id,change_type,target_segment_id,
                   target_connection_id,effect,detail)
                   SELECT id,'recompute',%s,%s,'knock_on',%s FROM source_versions
                   WHERE superseded_by IS NULL LIMIT 1""",
                (b["id"], cid, knock_detail))


def recompute_incremental(cur, plan_id: int, changes: list[dict],
                          trigger_version_id: int) -> dict:
    """局部重算。

    changes: ingest 返回的变更摘要。
    - 直接命中的段：用最新数据重新评估；
    - 之后的段：按新时刻连带重算（锚定传播）；
    - 用户锁定段：不移动/不改时，只追加说明；接驳连带影响仍要计算。
    """
    cur.execute("SELECT * FROM plans WHERE id=%s", (plan_id,))
    plan = cur.fetchone()
    travel_date = str(plan["travel_date"])
    view = DataView(cur, travel_date)

    cur.execute("SELECT * FROM segments WHERE plan_id=%s ORDER BY position", (plan_id,))
    rows = cur.fetchall()
    evs = [_load_ev(cur, r) for r in rows]

    # 1) 计算直接受影响槽位（按变更类型与实体精确命中）
    direct_positions: set[int] = set()
    pos_change: dict[int, str] = {}

    def mark(i, ctype):
        direct_positions.add(i)
        pos_change.setdefault(i, ctype)

    def _slots_for_route(route_code: str):
        for i, t in enumerate(SLOT_TEMPLATE):
            if t["kind"] in ("ferry", "shuttle") and t.get("route_hint") == route_code:
                yield i

    for ch in changes:
        ctype = ch["change_type"]
        if ctype == "closure_added":
            if ch.get("entity_type") == "place" or (not ch.get("entity_type") and ch.get("entity_key")):
                for i, t in enumerate(SLOT_TEMPLATE):
                    if t.get("place") == ch["entity_key"]:
                        mark(i, ctype)
            # 路线封闭：route code 暂以 entity_key 承载，交由数据行精确判定，
            # 保守命中同 OD 的交通槽位（重算时 evaluate 会读取真实封闭记录）
        elif ctype in ("ferry_cancel", "ferry_time", "ferry_added"):
            rc = ch.get("route_code")
            hit = False
            if rc:
                for i in _slots_for_route(rc):
                    mark(i, ctype); hit = True
            if not hit:
                # 无 route_hint 时按 OD 模式命中所有交通槽位（再经评估筛选）
                for i, t in enumerate(SLOT_TEMPLATE):
                    if t["kind"] in ("ferry", "shuttle"):
                        mark(i, ctype)
        elif ctype == "tide_missing":
            mark(SLOT_INDEX["reef_window"], ctype)
        elif ctype == "place_coord":
            for i, t in enumerate(SLOT_TEMPLATE):
                if t.get("place") == ch.get("entity_key"):
                    mark(i, ctype)

    if not direct_positions:
        return {"plan_id": plan_id, "recomputed": [], "locked_blocked": [], "changed": False}

    start = min(direct_positions)
    affected = set(range(start, len(SLOT_TEMPLATE)))  # 含下游连带
    upstream_detail: dict[int, str] = {}
    recomputed, locked_blocked = [], []

    # 2) 逐段重算
    anchor = evs[start - 1]["ends_at"] if start > 0 else None
    for pos in range(start, len(evs)):
        tmpl = SLOT_TEMPLATE[pos]
        ev_old = evs[pos]
        if ev_old["locked"]:
            # 锁定段不移动；但用新数据校验可行性，记录被锁定阻挡的原因
            check = evaluate_segment(cur, view, tmpl, anchor_arrival=anchor)
            note = "用户已锁定该段，时刻保持不变；"
            if check["state"] == "infeasible":
                note += f"新数据下该段已不可行（{check.get('infeasible_reason')}），需用户决定"
                locked_blocked.append({"slot_code": tmpl["slot_code"], "segment_id": ev_old["id"],
                                       "reason": check["infeasible_reason"]})
            elif check["starts_at"] and ev_old["starts_at"] and check["starts_at"] != ev_old["starts_at"]:
                note += "新数据建议时刻已变化但未采用"
                locked_blocked.append({"slot_code": tmpl["slot_code"], "segment_id": ev_old["id"],
                                       "reason": "锁定段与新数据时刻不一致"})
            cur.execute("UPDATE segments SET evidence = evidence || %s WHERE id=%s",
                        (json.dumps([{"type": "recompute_locked", "note": note,
                                      "trigger_version_id": trigger_version_id,
                                      "at": datetime.now().astimezone().isoformat()}],
                                    ensure_ascii=False), ev_old["id"]))
            upstream_detail[ev_old["id"]] = "下游受锁定段阻挡，时刻无法自动传播"
            # 锁定段后锚点仍用旧时刻
            anchor = ev_old["ends_at"]
            recomputed.append({"slot_code": tmpl["slot_code"], "segment_id": ev_old["id"],
                               "locked": True, "state": ev_old["state"]})
            continue

        ev_new = evaluate_segment(cur, view, tmpl, anchor_arrival=anchor)
        ev_new["id"] = ev_old["id"]
        # 更新段
        cur.execute(
            """UPDATE segments SET starts_at=%s,ends_at=%s,state=%s,infeasible_reason=%s,
               reason_codes=%s,record_ref=%s,evidence=%s WHERE id=%s""",
            (ev_new["starts_at"], ev_new["ends_at"], ev_new["state"],
             ev_new.get("infeasible_reason"), ev_new.get("reason_codes", []),
             json.dumps(ev_new["record_ref"], ensure_ascii=False, default=str),
             json.dumps(ev_new["evidence"], ensure_ascii=False, default=str),
             ev_old["id"]))
        # 刷新替代候选
        cur.execute("DELETE FROM alternatives WHERE segment_id=%s", (ev_old["id"],))
        for alt in ev_new.get("alternatives", []):
            cur.execute(
                """INSERT INTO alternatives (segment_id,rank,kind,title,starts_at,ends_at,record_ref,evidence)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ev_old["id"], alt["rank"], alt["kind"], alt["title"],
                 alt["starts_at"], alt["ends_at"],
                 json.dumps(alt["record_ref"], ensure_ascii=False, default=str),
                 json.dumps(alt["evidence"], ensure_ascii=False, default=str)))
        # 影响边
        effect = "direct" if pos in direct_positions else "knock_on"
        cur.execute(
            """INSERT INTO impact_edges
               (source_version_id,change_type,target_segment_id,effect,detail)
               VALUES (%s,%s,%s,%s,%s)""",
            (trigger_version_id,
             pos_change.get(pos, "upstream_shift"),
             ev_old["id"], effect,
             "直接受数据变更影响" if effect == "direct" else "上游时刻变化连带重算"))
        if effect == "knock_on":
            upstream_detail[ev_old["id"]] = "上游段时刻变化，本段连带调整"
        evs[pos] = ev_new
        anchor = ev_new["ends_at"]
        recomputed.append({"slot_code": tmpl["slot_code"], "segment_id": ev_old["id"],
                           "locked": False, "state": ev_new["state"]})

    knock_seg_ids = {evs[p]["id"] for p in affected}
    _rebuild_connections(cur, plan_id, evs, knock_set=knock_seg_ids,
                         upstream_detail=upstream_detail)
    cur.execute(
        "UPDATE plans SET status='active', generated_mode='incremental', based_on_versions=%s WHERE id=%s",
        (json.dumps(latest_version_ids(cur)), plan_id))
    return {"plan_id": plan_id, "recomputed": recomputed,
            "locked_blocked": locked_blocked, "changed": True,
            "direct_slots": [SLOT_TEMPLATE[p]["slot_code"] for p in sorted(direct_positions)]}


def lock_segment(cur, segment_id: int, locked: bool) -> dict:
    cur.execute("SELECT * FROM segments WHERE id=%s", (segment_id,))
    seg = cur.fetchone()
    if not seg:
        raise ValueError("段不存在")
    new_state = "locked" if locked else ("feasible" if seg["starts_at"] else seg["state"])
    cur.execute("UPDATE segments SET locked=%s, state=%s WHERE id=%s",
                (locked, new_state, segment_id))
    cur.execute("UPDATE plans SET locked=TRUE WHERE id=%s", (seg["plan_id"],))
    return {"segment_id": segment_id, "locked": locked, "state": new_state}


def apply_alternative(cur, segment_id: int, alternative_id: int) -> dict:
    cur.execute("SELECT * FROM alternatives WHERE id=%s AND segment_id=%s",
                (alternative_id, segment_id))
    alt = cur.fetchone()
    if not alt:
        raise ValueError("替代候选不存在")
    cur.execute("UPDATE alternatives SET chosen=FALSE WHERE segment_id=%s", (segment_id,))
    cur.execute("UPDATE alternatives SET chosen=TRUE WHERE id=%s", (alternative_id,))
    cur.execute(
        """UPDATE segments SET starts_at=%s,ends_at=%s,state='feasible',
           infeasible_reason=NULL, reason_codes='{}', record_ref=%s,
           evidence=evidence||%s WHERE id=%s""",
        (alt["starts_at"], alt["ends_at"],
         json.dumps(alt["record_ref"], ensure_ascii=False),
         json.dumps([{"type": "alternative_chosen", "alternative_id": alternative_id,
                      "at": datetime.now().astimezone().isoformat(),
                      "record_ref": alt["record_ref"]}], ensure_ascii=False, default=str),
         segment_id))
    # 重算下游接驳与时刻
    cur.execute("SELECT * FROM segments WHERE id=%s", (segment_id,))
    seg = cur.fetchone()
    cur.execute("SELECT * FROM plans WHERE id=%s", (seg["plan_id"],))
    plan = cur.fetchone()
    view = DataView(cur, str(plan["travel_date"]))
    cur.execute("SELECT * FROM segments WHERE plan_id=%s ORDER BY position", (seg["plan_id"],))
    rows = cur.fetchall()
    evs = [_load_ev(cur, r) for r in rows]
    pos = seg["position"]
    anchor = _dt(alt["ends_at"])
    for p in range(pos + 1, len(evs)):
        if evs[p]["locked"]:
            anchor = evs[p]["ends_at"]
            continue
        # 用户采用替代后，下游交通段允许在原时段窗口内重新择班（不受锚点过早限制）
        ev_new = evaluate_segment(cur, view, SLOT_TEMPLATE[p],
                                  anchor_arrival=anchor, rewindow=True)
        ev_new["id"] = evs[p]["id"]
        cur.execute(
            """UPDATE segments SET starts_at=%s,ends_at=%s,state=%s,infeasible_reason=%s,
               reason_codes=%s,record_ref=%s,evidence=%s WHERE id=%s""",
            (ev_new["starts_at"], ev_new["ends_at"], ev_new["state"],
             ev_new.get("infeasible_reason"), ev_new.get("reason_codes", []),
             json.dumps(ev_new["record_ref"], ensure_ascii=False, default=str),
             json.dumps(ev_new["evidence"], ensure_ascii=False, default=str),
             evs[p]["id"]))
        evs[p] = ev_new
        anchor = ev_new["ends_at"]
    knock_ids = {evs[p]["id"] for p in range(pos, len(evs))}
    _rebuild_connections(cur, seg["plan_id"], evs, knock_set=knock_ids)
    return {"segment_id": segment_id, "alternative_id": alternative_id, "applied": True}
