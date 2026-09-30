"""从可替换示例数据源接收数据：版本化、矛盾裁决、影响记录。

- 同一 source_key+revision 内容不变 -> 幂等，不重复入库
- 每个实体以「业务唯一键」归并；不同来源矛盾时按 priority 裁决（高者胜），
  平局时官方/较高 source_key 字典序兜底，裁决写入 data_conflicts
- 返回 changes：与此前生效状态对比的差异，供规划器做局部重算
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone

from ..db import tx
from ..adapters.base import SourceBundle

ENTITY_TYPES = ("places", "tide_points", "ferries", "shuttles", "closures", "tides")


def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _entity_key(etype: str, e: dict) -> str:
    if etype in ("places",):
        return e["code"]
    if etype == "tide_points":
        return e["code"]
    if etype in ("ferries", "shuttles"):
        return f"{e['mode']}|{e['route_code']}|{e['date']}"
    if etype == "closures":
        return e["closure_key"]
    if etype == "tides":
        return f"{e['tide_point_code']}|{e['observed_at']}"
    raise ValueError(etype)


# 实体对比时忽略的元数据（来源差异不应算作业务矛盾）
def _canonical(etype: str, e: dict) -> dict:
    drop = {"source_key", "priority"}
    return {k: v for k, v in e.items() if k not in drop}


def _current_version_id(cur, source_key: str):
    cur.execute(
        "SELECT id FROM source_versions WHERE source_key=%s AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
        (source_key,))
    row = cur.fetchone()
    return row["id"] if row else None


def ingest_bundle(cur, bundle: SourceBundle) -> dict:
    """写入一个来源包，返回变更摘要。"""
    digest = _sha(bundle.payload)
    cur.execute("SELECT id FROM source_versions WHERE source_key=%s AND revision=%s AND content_sha256=%s",
                (bundle.source_key, bundle.revision, digest))
    existing = cur.fetchone()
    if existing:
        return {"source_version_id": existing["id"], "noop": True, "changes": []}

    prev_id = _current_version_id(cur, bundle.source_key)
    cur.execute(
        """INSERT INTO source_versions (source_key, revision, content_sha256, payload, priority)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (bundle.source_key, bundle.revision, digest, json.dumps(bundle.payload), bundle.priority))
    version_id = cur.fetchone()["id"]
    if prev_id:
        cur.execute("UPDATE source_versions SET superseded_by=%s WHERE id=%s", (version_id, prev_id))

    changes: list[dict] = []
    p = bundle.payload
    # places / tide_points
    for e in p.get("places", []):
        _upsert_place(cur, e, version_id, bundle, changes)
    for e in p.get("tide_points", []):
        _upsert_tide_point(cur, e, version_id, bundle, changes)
    # ferries & shuttles
    for etype in ("ferries", "shuttles"):
        for e in p.get(etype, []):
            _upsert_ferry(cur, e, version_id, bundle, changes)
    for e in p.get("closures", []):
        _upsert_closure(cur, e, version_id, bundle, changes)
    for e in p.get("tides", []):
        _upsert_tide(cur, e, version_id, bundle, changes)

    return {"source_version_id": version_id, "noop": False, "changes": changes}


def _record_conflict(cur, etype, key, winner, winner_vid, loser, loser_vid, bundle):
    cur.execute(
        """INSERT INTO data_conflicts
           (entity_type, entity_key, winner_version_id, loser_version_id,
            winner_snapshot, loser_snapshot, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (etype, key, winner_vid, loser_vid, json.dumps(winner), json.dumps(loser),
         f"priority={bundle.priority} 来源生效；矛盾已留痕，可在后台追溯"))



def _deactivate_other_rows(cur, table, id_col, vid, where_sql, params):
    """新版本生效：把同业务实体、其它来源版本的活跃行置为 inactive。"""
    cur.execute(
        f"UPDATE {table} SET active=FALSE WHERE {where_sql} AND source_version_id <> %s AND active=TRUE",
        params + [vid])


def _reactivate_rows(cur, table, vid, where_sql, params):
    """回滚：激活目标版本对应行，停用其它。"""
    cur.execute(
        f"UPDATE {table} SET active=TRUE WHERE {where_sql} AND source_version_id=%s",
        params + [vid])
    cur.execute(
        f"UPDATE {table} SET active=FALSE WHERE {where_sql} AND source_version_id <> %s AND active=TRUE",
        params + [vid])


def _upsert_place(cur, e, vid, bundle, changes):
    key = e["code"]
    cur.execute("SELECT * FROM places WHERE code=%s", (key,))
    old = cur.fetchone()
    if old and old["source_version_id"] != vid:
        old_priority = _source_priority(cur, old["source_version_id"])
        new_vals = (float(e["lat"]), float(e["lon"]), e.get("timezone"))
        old_vals = (float(old["lat"]), float(old["lon"]), old["timezone"])
        if new_vals != old_vals:
            if bundle.priority > old_priority:
                cur.execute(
                    """UPDATE places SET name=%s,kind=%s,lat=%s,lon=%s,timezone=%s,
                       tide_point_code=%s,source_version_id=%s,updated_at=now() WHERE code=%s""",
                    (e["name"], e["kind"], e["lat"], e["lon"], e["timezone"],
                     e.get("tide_point_code"), vid, key))
                _record_conflict(cur, "place", key, e, vid,
                                 {"lat": old_vals[0], "lon": old_vals[1], "timezone": old_vals[2]},
                                 old["source_version_id"], bundle)
                changes.append({"change_type": "place_coord", "entity_type": "place",
                                "entity_key": key, "detail": "地点坐标/时区被高优先级来源修订"})
            else:
                _record_conflict(cur, "place", key,
                                 {"lat": old_vals[0], "lon": old_vals[1]}, old["source_version_id"],
                                 e, vid, bundle)
        return
    cur.execute(
        """INSERT INTO places (code,name,kind,lat,lon,timezone,tide_point_code,source_version_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, kind=EXCLUDED.kind,
             lat=EXCLUDED.lat, lon=EXCLUDED.lon, timezone=EXCLUDED.timezone,
             tide_point_code=EXCLUDED.tide_point_code, source_version_id=EXCLUDED.source_version_id,
             updated_at=now()""",
        (key, e["name"], e["kind"], e["lat"], e["lon"], e["timezone"],
         e.get("tide_point_code"), vid))
    if not old:
        changes.append({"change_type": "place_added", "entity_type": "place", "entity_key": key})


def _upsert_tide_point(cur, e, vid, bundle, changes):
    cur.execute(
        """INSERT INTO tide_points (code,name,lat,lon,timezone,applies_to,max_safe_cm,source_version_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name,lat=EXCLUDED.lat,lon=EXCLUDED.lon,
             timezone=EXCLUDED.timezone,applies_to=EXCLUDED.applies_to,
             max_safe_cm=EXCLUDED.max_safe_cm,source_version_id=EXCLUDED.source_version_id""",
        (e["code"], e["name"], e["lat"], e["lon"], e["timezone"], e["applies_to"],
         e["max_safe_cm"], vid))


def _source_priority(cur, vid) -> int:
    cur.execute("SELECT priority FROM source_versions WHERE id=%s", (vid,))
    row = cur.fetchone()
    return row["priority"] if row else 0


def _source_key(cur, vid) -> str | None:
    cur.execute("SELECT source_key FROM source_versions WHERE id=%s", (vid,))
    row = cur.fetchone()
    return row["source_key"] if row else None


def _upsert_ferry(cur, e, vid, bundle, changes):
    key = _entity_key("ferries", e)
    cur.execute(
        """SELECT fr.* FROM ferry_records fr
           JOIN source_versions sv ON sv.id = fr.source_version_id
           WHERE fr.mode=%s AND fr.route_code=%s AND fr.date=%s
             AND (fr.active=TRUE OR fr.source_version_id=%s
                  OR sv.source_key = (SELECT source_key FROM source_versions WHERE id=%s))
           ORDER BY fr.source_version_id = %s DESC""",
        (e["mode"], e["route_code"], e["date"], vid, vid, vid))
    rows = cur.fetchall()
    cur.execute(
        """INSERT INTO ferry_records
           (mode,route_code,date,depart_place,arrive_place,depart_at,arrive_at,status,source_version_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (mode,route_code,date,source_version_id) DO UPDATE SET
             depart_place=EXCLUDED.depart_place, arrive_place=EXCLUDED.arrive_place,
             depart_at=EXCLUDED.depart_at, arrive_at=EXCLUDED.arrive_at, status=EXCLUDED.status,
             updated_at=now()""",
        (e["mode"], e["route_code"], e["date"], e["depart_place"], e["arrive_place"],
         e["depart_at"], e["arrive_at"], e["status"], vid))
    # 该版本为当前来源最新 -> 先激活其行；再按来源优先级裁决：低来源不得覆盖高来源
    cur.execute("UPDATE ferry_records SET active=TRUE WHERE mode=%s AND route_code=%s AND date=%s AND source_version_id=%s",
                (e["mode"], e["route_code"], e["date"], vid))
    # 同一来源的旧版本行一律停用（superseded 版本不再是权威数据）
    cur.execute("""UPDATE ferry_records fr SET active=FALSE
                   FROM source_versions sv
                   WHERE fr.source_version_id=sv.id AND sv.superseded_by IS NOT NULL
                     AND fr.mode=%s AND fr.route_code=%s AND fr.date=%s""",
                (e["mode"], e["route_code"], e["date"]))
    # 不同来源的当前版本：仅最高优先级者 active
    cur.execute("""UPDATE ferry_records fr SET active = sub.winner
                   FROM (
                     SELECT x.id, (x.src_pri = max(x.src_pri) OVER ()) AS winner
                     FROM (
                       SELECT fr2.id, sv.priority AS src_pri
                       FROM ferry_records fr2
                       JOIN source_versions sv ON sv.id = fr2.source_version_id
                       WHERE fr2.mode=%s AND fr2.route_code=%s AND fr2.date=%s
                         AND sv.superseded_by IS NULL
                     ) x
                   ) sub
                   WHERE fr.id = sub.id""",
                (e["mode"], e["route_code"], e["date"]))
    # 与其它来源同实体记录比较
    for r in rows:
        if r["source_version_id"] == vid:
            continue
        same = (r["depart_at"].isoformat() == datetime.fromisoformat(e["depart_at"]).isoformat()
                and r["arrive_at"].isoformat() == datetime.fromisoformat(e["arrive_at"]).isoformat()
                and r["status"] == e["status"])
        if not same:
            other_pri = _source_priority(cur, r["source_version_id"])
            # 仅与「当前生效(active)」行比较，避免与同一来源历史版本反复记冲突
            if not r.get("active", False):
                continue
            # 已记录过同一对版本的冲突则跳过
            cur.execute("""SELECT 1 FROM data_conflicts
                           WHERE entity_type='ferry' AND entity_key=%s
                             AND ((winner_version_id=%s AND loser_version_id=%s)
                               OR (winner_version_id=%s AND loser_version_id=%s)) LIMIT 1""",
                        (key, vid, r["source_version_id"], r["source_version_id"], vid))
            if cur.fetchone():
                continue
            if bundle.priority > other_pri:
                _record_conflict(cur, "ferry", key, e, vid,
                                 {"depart_at": r["depart_at"].isoformat(),
                                  "arrive_at": r["arrive_at"].isoformat(), "status": r["status"]},
                                 r["source_version_id"], bundle)
            else:
                _record_conflict(cur, "ferry", key,
                                 {"depart_at": r["depart_at"].isoformat(),
                                  "arrive_at": r["arrive_at"].isoformat(), "status": r["status"]},
                                 r["source_version_id"], e, vid, bundle)
    same_source_prev = [r for r in rows if r["source_version_id"] != vid]
    other_source = [r for r in rows if r["source_version_id"] != vid]
    if not rows:
        changes.append({"change_type": "ferry_added", "entity_type": "ferry",
                        "entity_key": key, "route_code": e["route_code"]})
    elif e["status"] == "cancelled":
        # 裁决后该取消版本确实生效，且该路线此前不是取消状态 -> 发取消变更
        cur.execute("SELECT active FROM ferry_records WHERE mode=%s AND route_code=%s AND date=%s AND source_version_id=%s",
                    (e["mode"], e["route_code"], e["date"], vid))
        arow = cur.fetchone()
        was_cancelled = any(r["status"] == "cancelled" for r in same_source_prev)
        if arow and arow["active"] and not was_cancelled:
            changes.append({"change_type": "ferry_cancel", "entity_type": "ferry",
                            "entity_key": key, "route_code": e["route_code"]})
    else:
        # 时刻变更：以「同一来源」上一版本为准，避免把跨来源矛盾误报为时刻变更
        cur.execute("""SELECT source_key FROM source_versions WHERE id=%s""", (vid,))
        src_key = cur.fetchone()["source_key"]
        prev = next((r for r in rows
                     if r["source_version_id"] != vid and _source_key(cur, r["source_version_id"]) == src_key),
                    None)
        if prev and (prev["depart_at"] != datetime.fromisoformat(e["depart_at"])
                     or prev["arrive_at"] != datetime.fromisoformat(e["arrive_at"])):
            changes.append({"change_type": "ferry_time", "entity_type": "ferry",
                            "entity_key": key, "route_code": e["route_code"]})


def _upsert_closure(cur, e, vid, bundle, changes):
    cur.execute("SELECT id FROM closure_records WHERE closure_key=%s", (e["closure_key"],))
    existed = cur.fetchone()
    cur.execute(
        """INSERT INTO closure_records
           (closure_key,target_type,target_code,reason,starts_at,ends_at,source_version_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (closure_key,source_version_id) DO UPDATE SET
             target_type=EXCLUDED.target_type,target_code=EXCLUDED.target_code,
             reason=EXCLUDED.reason,starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at""",
        (e["closure_key"], e["target_type"], e["target_code"], e.get("reason"),
         e["starts_at"], e["ends_at"], vid))
    cur.execute("UPDATE closure_records SET active=TRUE WHERE closure_key=%s AND source_version_id=%s",
                (e["closure_key"], vid))
    cur.execute("""UPDATE closure_records cr SET active=FALSE
                   FROM source_versions sv WHERE cr.source_version_id=sv.id
                   AND sv.superseded_by IS NOT NULL AND cr.closure_key=%s""",
                (e["closure_key"],))
    cur.execute("""UPDATE closure_records cr SET active=FALSE WHERE cr.closure_key=%s
                   AND cr.source_version_id<>%s AND cr.active=TRUE
                   AND NOT EXISTS (SELECT 1 FROM source_versions sv WHERE sv.id=cr.source_version_id
                                   AND sv.priority > %s)""",
                (e["closure_key"], vid, bundle.priority))
    if not existed:
        changes.append({"change_type": "closure_added", "entity_type": e["target_type"],
                        "entity_key": e["target_code"], "closure_key": e["closure_key"],
                        "starts_at": e["starts_at"], "ends_at": e["ends_at"]})


def _upsert_tide(cur, e, vid, bundle, changes):
    cur.execute(
        """INSERT INTO tide_records (tide_point_code,observed_at,level_cm,kind,source_version_id)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (tide_point_code,observed_at,source_version_id) DO UPDATE SET
             level_cm=EXCLUDED.level_cm, kind=EXCLUDED.kind""",
        (e["tide_point_code"], e["observed_at"], e["level_cm"], e["kind"], vid))
    cur.execute("UPDATE tide_records SET active=TRUE WHERE tide_point_code=%s AND observed_at=%s AND source_version_id=%s",
                (e["tide_point_code"], e["observed_at"], vid))
    cur.execute("""UPDATE tide_records tr SET active=FALSE
                   FROM source_versions sv WHERE tr.source_version_id=sv.id
                   AND sv.superseded_by IS NOT NULL
                   AND tr.tide_point_code=%s AND tr.observed_at=%s""",
                (e["tide_point_code"], e["observed_at"]))
    if e.get("level_cm") is None:
        changes.append({"change_type": "tide_missing", "entity_type": "tide",
                        "entity_key": _entity_key("tides", e)})


def rollback_source(cur, source_key: str, to_revision: str | None = None) -> int:
    """旧缓存回滚：把某来源恢复到指定 revision（或上一版）。

    实现方式：将目标版本标记为当前（superseded_by 置空，其后版本反向链接），
    并按其 payload 重建该来源负责的权威数据行。
    """
    cur.execute("SELECT * FROM source_versions WHERE source_key=%s ORDER BY id", (source_key,))
    versions = cur.fetchall()
    if not versions:
        raise ValueError(f"未知来源: {source_key}")
    if to_revision:
        target = next((v for v in versions if v["revision"] == to_revision), None)
        if not target:
            raise ValueError(f"版本不存在: {to_revision}")
    else:
        cur.execute(
            "SELECT * FROM source_versions WHERE source_key=%s AND superseded_by IS NULL", (source_key,))
        current = cur.fetchone()
        target = next((v for v in reversed(versions) if v["id"] != (current or {}).get("id")), None)
        if not target:
            raise ValueError("没有可回滚的旧版本")

    # 版本链：目标之后的版本全部标记为被目标取代；目标复活
    cur.execute("UPDATE source_versions SET superseded_by=NULL WHERE id=%s", (target["id"],))
    cur.execute("UPDATE source_versions SET superseded_by=%s WHERE source_key=%s AND id>%s",
                (target["id"], source_key, target["id"]))

    # 停用该来源「晚于目标」版本写入的业务行（回滚旧缓存）
    cur.execute("SELECT id FROM source_versions WHERE source_key=%s AND id>%s",
                (source_key, target["id"]))
    stale_vids = [r["id"] for r in cur.fetchall()]
    if stale_vids:
        for table in ("ferry_records", "closure_records", "tide_records"):
            cur.execute(f"UPDATE {table} SET active=FALSE WHERE source_version_id = ANY(%s)",
                        (stale_vids,))

    # 用目标 payload 覆盖该来源此前写入的行
    bundle = SourceBundle(source_key=source_key, revision=target["revision"], kind="mixed",
                          priority=target["priority"], payload=target["payload"])
    changes: list[dict] = []
    for e in target["payload"].get("places", []):
        _upsert_place(cur, e, target["id"], bundle, changes)
    for e in target["payload"].get("tide_points", []):
        _upsert_tide_point(cur, e, target["id"], bundle, changes)
    for etype in ("ferries", "shuttles"):
        for e in target["payload"].get(etype, []):
            _upsert_ferry(cur, e, target["id"], bundle, changes)
    for e in target["payload"].get("closures", []):
        _upsert_closure(cur, e, target["id"], bundle, changes)
    for e in target["payload"].get("tides", []):
        _upsert_tide(cur, e, target["id"], bundle, changes)
    return target["id"]


def latest_version_ids(cur) -> dict[str, int]:
    cur.execute("""SELECT DISTINCT ON (source_key) source_key, id
                   FROM source_versions ORDER BY source_key, id DESC""")
    return {r["source_key"]: r["id"] for r in cur.fetchall()}


def diff_baseline(cur, based_on: dict) -> list[dict]:
    """对比行程生成时的基线版本与当前版本，推断影响行程的变更。"""
    changes: list[dict] = []
    if not based_on:
        return changes
    cur.execute("SELECT id FROM source_versions WHERE superseded_by IS NULL")
    current_ids = {r["id"] for r in cur.fetchall()}
    if set(based_on.values()) == current_ids:
        return changes

    def _current_version_id(source_key):
        cur.execute("SELECT id FROM source_versions WHERE source_key=%s AND superseded_by IS NULL",
                    (source_key,))
        r = cur.fetchone()
        return r["id"] if r else None

    # 1) 班次：取消/时刻（同一来源当前版本 vs 基线版本）
    for key, base_vid in based_on.items():
        cur_vid = _current_version_id(key)
        if not cur_vid or cur_vid == base_vid:
            continue
        cur.execute("""SELECT route_code,status,depart_at,arrive_at FROM ferry_records
                       WHERE active=TRUE AND source_version_id=%s""", (cur_vid,))
        new_rows = {r["route_code"]: r for r in cur.fetchall()}
        cur.execute("SELECT route_code,status,depart_at,arrive_at FROM ferry_records WHERE source_version_id=%s",
                    (base_vid,))
        old_rows = {r["route_code"]: r for r in cur.fetchall()}
        for rc, nr in new_rows.items():
            if nr["status"] == "cancelled":
                changes.append({"change_type": "ferry_cancel", "entity_type": "ferry",
                                "route_code": rc})
            elif rc in old_rows and old_rows[rc]["status"] != "cancelled" and (
                    nr["depart_at"] != old_rows[rc]["depart_at"]
                    or nr["arrive_at"] != old_rows[rc]["arrive_at"]):
                changes.append({"change_type": "ferry_time", "entity_type": "ferry",
                                "route_code": rc})

    # 2) 封闭：当前生效封闭中，基线版本集里不存在同一封闭键的 -> 视为新增
    cur.execute("SELECT closure_key,target_code,target_type FROM closure_records WHERE active=TRUE")
    current_closures = cur.fetchall()
    for cc in current_closures:
        cur.execute("""SELECT 1 FROM closure_records
                       WHERE closure_key=%s AND source_version_id = ANY(%s) LIMIT 1""",
                    (cc["closure_key"], list(based_on.values())))
        if not cur.fetchone():
            changes.append({"change_type": "closure_added", "entity_type": cc["target_type"],
                            "entity_key": cc["target_code"], "closure_key": cc["closure_key"]})

    # 3) 潮汐缺测：当前活跃缺测点，且基线版本对应观测不是缺测
    cur.execute("SELECT DISTINCT tide_point_code,observed_at FROM tide_records WHERE active=TRUE AND level_cm IS NULL")
    for r in cur.fetchall():
        cur.execute("""SELECT level_cm FROM tide_records
                       WHERE tide_point_code=%s AND observed_at=%s
                         AND source_version_id = ANY(%s)""",
                    (r["tide_point_code"], r["observed_at"], list(based_on.values())))
        old = cur.fetchone()
        if old is None or old["level_cm"] is not None:
            changes.append({"change_type": "tide_missing", "entity_type": "tide",
                            "entity_key": r["tide_point_code"]})

    # 4) 坐标/时区修订：比较每个地点的基线版本行与当前行
    cur.execute("""SELECT p.code, p.lat, p.lon, p.timezone, p.source_version_id
                   FROM places p""")
    cur_places = cur.fetchall()
    for cp in cur_places:
        base_vid = based_on.get(_source_key_of_version(cur, cp["source_version_id"]))
        if not base_vid or base_vid == cp["source_version_id"]:
            continue
        cur.execute("SELECT lat,lon,timezone FROM places WHERE code=%s AND source_version_id=%s",
                    (cp["code"], base_vid))
        bp = cur.fetchone()
        if bp and (cp["lat"] != bp["lat"] or cp["lon"] != bp["lon"]
                   or cp["timezone"] != bp["timezone"]):
            changes.append({"change_type": "place_coord", "entity_type": "place",
                            "entity_key": cp["code"]})

    # 去重
    seen, uniq = set(), []
    for c in changes:
        k = (c["change_type"], c.get("entity_key") or c.get("route_code"))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _source_key_of_version(cur, vid):
    cur.execute("SELECT source_key FROM source_versions WHERE id=%s", (vid,))
    r = cur.fetchone()
    return r["source_key"] if r else None

