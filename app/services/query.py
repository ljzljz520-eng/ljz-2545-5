"""页面查询服务。"""
from __future__ import annotations


def _place_name(cur, code):
    cur.execute("SELECT name FROM places WHERE code=%s", (code,))
    r = cur.fetchone()
    return r["name"] if r else code


def story_payload(cur):
    cur.execute("SELECT * FROM places ORDER BY code")
    places = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM tide_points")
    tps = {r["code"]: dict(r) for r in cur.fetchall()}
    cur.execute("SELECT * FROM source_versions WHERE superseded_by IS NULL ORDER BY source_key")
    sources = [{"source_key": r["source_key"], "revision": r["revision"],
                "received_at": r["received_at"].isoformat()} for r in cur.fetchall()]
    # 开放资料条目（机器可读 JSON / CSV 下载在路由层生成）
    open_datasets = [
        {"id": "places", "title": "地点与观景点名录", "format": "JSON / CSV",
         "license": "CC BY 4.0", "rows": len(places)},
        {"id": "ferries", "title": "船班与接驳班次", "format": "JSON",
         "license": "CC BY 4.0", "rows": _count(cur, "ferry_records")},
        {"id": "tides", "title": "潮汐观测与预报", "format": "JSON",
         "license": "CC BY 4.0", "rows": _count(cur, "tide_records")},
    ]
    cur.execute("""SELECT fr.* FROM ferry_records fr
                   JOIN source_versions sv ON sv.id=fr.source_version_id
                   WHERE fr.active=TRUE AND sv.superseded_by IS NULL
                   ORDER BY fr.depart_at""")
    transports = []
    for r in cur.fetchall():
        transports.append({"mode": r["mode"], "route_code": r["route_code"],
                           "from_name": _place_name(cur, r["depart_place"]),
                           "to_name": _place_name(cur, r["arrive_place"]),
                           "depart_at": r["depart_at"].strftime("%H:%M"),
                           "arrive_at": r["arrive_at"].strftime("%H:%M"),
                           "status": r["status"],
                           "source_version_id": r["source_version_id"]})
    return {"places": places, "tide_points": list(tps.values()),
            "transports": transports,
            "sources": sources, "open_datasets": open_datasets}


def _count(cur, table):
    cur.execute(f"SELECT count(*) AS c FROM {table}")
    return cur.fetchone()["c"]


def plan_payload(cur, plan_id: int):
    cur.execute("SELECT * FROM plans WHERE id=%s", (plan_id,))
    plan = cur.fetchone()
    if not plan:
        return None
    cur.execute("SELECT * FROM segments WHERE plan_id=%s ORDER BY position", (plan_id,))
    segs = []
    for r in cur.fetchall():
        d = dict(r)
        d["starts_at"] = r["starts_at"].isoformat() if r["starts_at"] else None
        d["ends_at"] = r["ends_at"].isoformat() if r["ends_at"] else None
        cur.execute("SELECT * FROM alternatives WHERE segment_id=%s ORDER BY rank", (r["id"],))
        d["alternatives"] = [dict(a) for a in cur.fetchall()]
        segs.append(d)
    cur.execute("""SELECT c.*, s1.slot_code AS from_slot, s2.slot_code AS to_slot
                   FROM connections c
                   JOIN segments s1 ON s1.id=c.from_segment_id
                   JOIN segments s2 ON s2.id=c.to_segment_id
                   WHERE c.plan_id=%s ORDER BY c.id""", (plan_id,))
    conns = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT ie.*, sv.source_key, sv.revision FROM impact_edges ie
                   JOIN source_versions sv ON sv.id=ie.source_version_id
                   WHERE ie.target_segment_id IN (SELECT id FROM segments WHERE plan_id=%s)
                      OR ie.target_connection_id IN (SELECT id FROM connections WHERE plan_id=%s)
                   ORDER BY ie.id""", (plan_id, plan_id))
    impacts = [dict(r) for r in cur.fetchall()]
    return {"plan": dict(plan), "segments": segs, "connections": conns, "impacts": impacts}


def admin_payload(cur):
    cur.execute("SELECT * FROM source_versions ORDER BY id DESC")
    versions = []
    for r in cur.fetchall():
        versions.append({"id": r["id"], "source_key": r["source_key"], "revision": r["revision"],
                         "priority": r["priority"], "received_at": r["received_at"].isoformat(),
                         "superseded_by": r["superseded_by"]})
    cur.execute("SELECT * FROM data_conflicts ORDER BY id DESC")
    conflicts = [{"id": r["id"], "entity_type": r["entity_type"], "entity_key": r["entity_key"],
                  "reason": r["reason"], "winner_version_id": r["winner_version_id"],
                  "loser_version_id": r["loser_version_id"],
                  "detected_at": r["detected_at"].isoformat()} for r in cur.fetchall()]
    cur.execute("SELECT * FROM offline_bundles ORDER BY id DESC")
    bundles = [{"id": r["id"], "frozen_at": r["frozen_at"].isoformat(),
                "covers_date": str(r["covers_date"]), "gap_sources": r["gap_sources"],
                "note": r["note"], "bundle_path": r["bundle_path"]} for r in cur.fetchall()]
    return {"versions": versions, "conflicts": conflicts, "bundles": bundles}
