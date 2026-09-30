"""建表 + 初始化场景数据。用法: python scripts/init_db.py [scenario_name]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import tx
from app.adapters import JsonFileAdapter
from app.services import ingest as ingest_svc

BASE = Path(__file__).resolve().parent.parent
SCHEMA = BASE / "app" / "schema.sql"


def init_schema(cur):
    # 干净重建（切换场景时避免 source_key+revision 唯一约束冲突）
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    cur.execute(SCHEMA.read_text(encoding="utf-8"))


def load_scenario(cur, scenario: str):
    d = BASE / "data" / "samples" / scenario
    if not d.exists():
        raise SystemExit(f"场景不存在: {d}")
    results = []
    for f in sorted(d.glob("*.json")):
        for b in JsonFileAdapter(f).bundles():
            out = ingest_svc.ingest_bundle(cur, b)
            results.append((f.name, b.source_key, b.revision, out["source_version_id"], out["changes"]))
    return results


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "default"
    with tx() as (conn, cur):
        init_schema(cur)
        results = load_scenario(cur, scenario)
    for f, key, rev, vid, changes in results:
        print(f"  ingest {f:32s} {key:24s} {rev:16s} -> version {vid} changes={len(changes)}")
    print(f"场景 {scenario} 初始化完成")
