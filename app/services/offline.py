"""离线包：冻结数据快照、标注冻结日期与更新缺口。

离线包是静态快照：显示冻结日期(frozen_at)、覆盖行程日(covers_date)、
各来源采用版本，以及自冻结以来的更新缺口（被废弃版本/缺口来源）。
页面必须声明：不保证现实交通可用。
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import OFFLINE_DIR
from ..db import tx


def build_bundle(cur, covers_date: str) -> dict:
    cur.execute("""SELECT DISTINCT ON (source_key) id, source_key, revision, payload, received_at
                   FROM source_versions ORDER BY source_key, id DESC""")
    current = cur.fetchall()
    version_ids = [r["id"] for r in current]

    # 更新缺口：相对「上一个冻结包」，本次新出现的来源版本
    cur.execute("SELECT version_ids FROM offline_bundles ORDER BY id DESC LIMIT 1")
    prev = cur.fetchone()
    gaps = []
    if prev:
        frozen_ids = set(prev["version_ids"])
        # 逐来源比较：当前最新版本与上次冻结版本是否不同
        for r in current:
            cur.execute("""SELECT revision FROM source_versions
                           WHERE source_key=%s AND id = ANY(%s) ORDER BY id DESC LIMIT 1""",
                        (r["source_key"], list(frozen_ids)))
            frow = cur.fetchone()
            if frow and frow["revision"] != r["revision"]:
                gaps.append({"source_key": r["source_key"], "frozen_rev": frow["revision"],
                             "newer_rev": r["revision"], "newer_at": r["received_at"]})
    gap_sources = sorted({g["source_key"] for g in gaps})

    snapshot = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "covers_date": covers_date,
        "disclaimer": "离线包为冻结快照，不保证现实交通可用；出行前请重新同步并核对潮汐与班次。",
        "versions": [{"source_key": r["source_key"], "revision": r["revision"],
                      "version_id": r["id"], "received_at": r["received_at"].isoformat()}
                     for r in current],
        "update_gap": [{"source_key": g["source_key"], "frozen_revision": g["frozen_rev"],
                        "newer_revision": g["newer_rev"], "newer_at": g["newer_at"].isoformat()}
                       for g in gaps],
    }
    # 业务数据快照（供纯静态页面渲染）
    cur.execute("SELECT code,name,kind,lat,lon,timezone,tide_point_code,source_version_id FROM places")
    snapshot["places"] = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM ferry_records WHERE date=%s", (covers_date,))
    snapshot["transports"] = [{**dict(r),
                               "depart_at": r["depart_at"].isoformat(),
                               "arrive_at": r["arrive_at"].isoformat()}
                              for r in cur.fetchall()]
    cur.execute("SELECT * FROM closure_records")
    snapshot["closures"] = [{**dict(r), "starts_at": r["starts_at"].isoformat(),
                             "ends_at": r["ends_at"].isoformat()} for r in cur.fetchall()]
    cur.execute("SELECT * FROM tide_records")
    snapshot["tides"] = [{**dict(r), "observed_at": r["observed_at"].isoformat()}
                         for r in cur.fetchall()]
    cur.execute("SELECT * FROM tide_points")
    snapshot["tide_points"] = [dict(r) for r in cur.fetchall()]

    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    checksum = hashlib.sha256(body.encode()).hexdigest()
    snapshot["checksum"] = checksum
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    path = OFFLINE_DIR / f"bundle_{covers_date}.json"
    path.write_text(body, encoding="utf-8")

    cur.execute(
        """INSERT INTO offline_bundles (frozen_at,covers_date,version_ids,gap_sources,
           bundle_path,checksum_sha256,note)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (snapshot["frozen_at"], covers_date, version_ids, gap_sources, str(path), checksum,
         snapshot["disclaimer"]))
    bundle_id = cur.fetchone()["id"]
    return {"bundle_id": bundle_id, "path": str(path), "checksum": checksum,
            "frozen_at": snapshot["frozen_at"], "gap_sources": gap_sources,
            "update_gap": snapshot["update_gap"], "versions": snapshot["versions"]}


def load_bundle(covers_date: str) -> dict:
    path = OFFLINE_DIR / f"bundle_{covers_date}.json"
    if not path.exists():
        raise FileNotFoundError("离线包尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))
