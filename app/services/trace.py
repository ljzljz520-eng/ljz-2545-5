"""可追溯：每个建议（段）采用了哪些条件、来源版本、冲突裁决与影响链。"""
from __future__ import annotations
import json


def trace_segment(cur, segment_id: int) -> dict:
    cur.execute("SELECT * FROM segments WHERE id=%s", (segment_id,))
    seg = cur.fetchone()
    if not seg:
        raise ValueError("段不存在")
    cur.execute("SELECT * FROM plans WHERE id=%s", (seg["plan_id"],))
    plan = cur.fetchone()

    version_ids = set()
    for ev in seg["evidence"] or []:
        vid = ev.get("source_version_id")
        if vid:
            version_ids.add(vid)
    if seg["record_ref"] and isinstance(seg["record_ref"], dict):
        vid = seg["record_ref"].get("source_version_id")
        if vid:
            version_ids.add(vid)

    # 来源版本详情
    versions = []
    for vid in sorted(version_ids):
        cur.execute("SELECT id,source_key,revision,priority,received_at FROM source_versions WHERE id=%s",
                    (vid,))
        v = cur.fetchone()
        if v:
            versions.append(dict(v))

    # 影响边（数据变更 -> 本段）
    cur.execute("""SELECT ie.*, sv.source_key, sv.revision FROM impact_edges ie
                   JOIN source_versions sv ON sv.id=ie.source_version_id
                   WHERE ie.target_segment_id=%s ORDER BY ie.id""", (segment_id,))
    impacts = [dict(r) for r in cur.fetchall()]

    # 按段涉及实体检索冲突裁决
    related_keys = set()
    if seg["record_ref"]:
        related_keys.add(f"{seg['record_ref'].get('mode')}|{seg['record_ref'].get('route_code')}|{plan['travel_date']}")
    related_keys.add(seg["to_place"] or "")
    related_keys.add(seg["from_place"] or "")
    cur.execute("SELECT * FROM data_conflicts WHERE entity_key = ANY(%s) ORDER BY detected_at DESC",
                (list(related_keys),))
    conflicts = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM alternatives WHERE segment_id=%s ORDER BY rank", (segment_id,))
    alts = [dict(r) for r in cur.fetchall()]

    # 相邻接驳
    cur.execute("""SELECT c.* FROM connections c
                   WHERE c.from_segment_id=%s OR c.to_segment_id=%s ORDER BY c.id""",
                (segment_id, segment_id))
    conns = []
    for r in cur.fetchall():
        d = dict(r)
        conns.append(d)

    return {
        "segment": {**dict(seg),
                    "starts_at": seg["starts_at"].isoformat() if seg["starts_at"] else None,
                    "ends_at": seg["ends_at"].isoformat() if seg["ends_at"] else None,
                    "evidence": seg["evidence"], "record_ref": seg["record_ref"]},
        "plan": {"id": plan["id"], "travel_date": str(plan["travel_date"]),
                 "generated_mode": plan["generated_mode"],
                 "based_on_versions": plan["based_on_versions"]},
        "source_versions": versions,
        "impacts": [{"change_type": i["change_type"], "effect": i["effect"],
                     "detail": i["detail"], "source_key": i["source_key"],
                     "revision": i["revision"]} for i in impacts],
        "conflicts": [{"entity_type": c["entity_type"], "entity_key": c["entity_key"],
                       "reason": c["reason"], "detected_at": c["detected_at"].isoformat(),
                       "winner_version_id": c["winner_version_id"],
                       "loser_version_id": c["loser_version_id"]} for c in conflicts],
        "alternatives": [{"id": a["id"], "rank": a["rank"], "title": a["title"],
                          "chosen": a["chosen"],
                          "starts_at": a["starts_at"].isoformat() if a["starts_at"] else None}
                         for a in alts],
        "connections": [{"feasible": c["feasible"], "gap_minutes": c["gap_minutes"],
                         "cross_day": c["cross_day"], "detail": c["detail"],
                         "knock_on": c["knock_on"], "knock_on_detail": c["knock_on_detail"]}
                        for c in conns],
    }
