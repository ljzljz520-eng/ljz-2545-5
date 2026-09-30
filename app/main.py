"""海岛灯塔导览站 · FastAPI 入口。

页面：
  /            故事页（交通、观景点、开放资料）
  /plan        行程界面（时段组合访问段；锁定/候选/不可行原因）
  /admin       后台（来源版本、冲突裁决、回滚、离线包管理）
  /offline     离线包页（冻结日期、更新缺口、不保证现实交通可用）
  /docs-site   站内设计文档（Markdown 渲染）
API：见各 /api 路由
"""
from __future__ import annotations
import csv
import io
import json
from pathlib import Path

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, SCENARIO_DIR, OPEN_DATA_NOTICE
from .db import tx
from .adapters import JsonFileAdapter, DictAdapter, AdapterError
from .services import ingest as ingest_svc
from .services import plans as plans_svc
from .services import offline as offline_svc
from .services import trace as trace_svc
from .services.query import story_payload, plan_payload, admin_payload

app = FastAPI(title="海岛灯塔导览站")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ---------- 页面 ----------
@app.get("/", response_class=HTMLResponse)
def page_story(request: Request):
    with tx() as (conn, cur):
        data = story_payload(cur)
    return templates.TemplateResponse(request, "story.html",
                                      {**data, "notice": OPEN_DATA_NOTICE})


@app.get("/plan", response_class=HTMLResponse)
def page_plan(request: Request, plan_id: int | None = None):
    with tx() as (conn, cur):
        if plan_id is None:
            cur.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            plan_id = row["id"] if row else None
        payload = plan_payload(cur, plan_id) if plan_id else None
    return templates.TemplateResponse(request, "plan.html",
                                      {"p": payload, "plan_id": plan_id})


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request):
    with tx() as (conn, cur):
        data = admin_payload(cur)
    return templates.TemplateResponse(request, "admin.html", data)


@app.get("/offline", response_class=HTMLResponse)
def page_offline(request: Request, date: str = "2026-10-03"):
    bundle = None
    try:
        bundle = offline_svc.load_bundle(date)
    except FileNotFoundError:
        bundle = None
    return templates.TemplateResponse(request, "offline.html",
                                      {"bundle": bundle, "date": date})


@app.get("/docs-site", response_class=HTMLResponse)
def page_docs(request: Request):
    md_path = BASE_DIR / "docs" / "design.md"
    html = markdown.markdown(md_path.read_text(encoding="utf-8"),
                             extensions=["tables", "fenced_code", "toc"])
    return templates.TemplateResponse(request, "docs.html", {"html": html})


# ---------- 行程 API ----------
@app.post("/api/plans/generate")
def api_generate(body: dict):
    date = body.get("date", "2026-10-03")
    with tx() as (conn, cur):
        plan_id = plans_svc.generate_static(cur, date)
    return {"plan_id": plan_id, "mode": "static"}


@app.post("/api/plans/{plan_id}/recompute")
def api_recompute(plan_id: int, body: dict):
    with tx() as (conn, cur):
        # 若带 source_key/revision，先推送该数据源，再用其版本变更触发局部重算
        trigger_version_id = None
        changes = []
        if "source" in body:
            adapter = DictAdapter(body["source"])
            bundles = list(adapter.bundles())
            for b in bundles:
                out = ingest_svc.ingest_bundle(cur, b)
                trigger_version_id = out["source_version_id"]
                changes.extend(out["changes"])
        else:
            # 自动检测「生成基线 → 当前版本」的差异
            cur.execute("SELECT based_on_versions FROM plans WHERE id=%s", (plan_id,))
            plan_row = cur.fetchone()
            changes = ingest_svc.diff_baseline(cur, plan_row["based_on_versions"] or {})
            cur.execute("SELECT id FROM source_versions WHERE superseded_by IS NULL "
                        "ORDER BY id DESC LIMIT 1")
            trigger_version_id = cur.fetchone()["id"]
        if not changes:
            return {"plan_id": plan_id, "changed": False, "reason": "未检测到影响行程的变更"}
        result = plans_svc.recompute_incremental(cur, plan_id, changes, trigger_version_id)
    return result


@app.post("/api/segments/{segment_id}/lock")
def api_lock(segment_id: int, body: dict):
    with tx() as (conn, cur):
        out = plans_svc.lock_segment(cur, segment_id, bool(body.get("locked", True)))
    return out


@app.post("/api/segments/{segment_id}/alternative/{alternative_id}")
def api_apply_alt(segment_id: int, alternative_id: int):
    with tx() as (conn, cur):
        try:
            out = plans_svc.apply_alternative(cur, segment_id, alternative_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
    return out


@app.get("/api/trace/{segment_id}")
def api_trace(segment_id: int):
    with tx() as (conn, cur):
        try:
            out = trace_svc.trace_segment(cur, segment_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
    return JSONResponse(json.loads(json.dumps(out, ensure_ascii=False, default=str)))


# ---------- 后台：数据接收 / 回滚 / 离线包 ----------
@app.post("/api/admin/ingest-file")
def api_ingest_file(body: dict):
    """从可替换示例数据源接收：指定场景目录或单个文件路径。"""
    path = body.get("path")
    if not path:
        raise HTTPException(400, "需要 path")
    p = Path(path)
    files = sorted(p.glob("*.json")) if p.is_dir() else [p]
    all_changes, versions = [], []
    with tx() as (conn, cur):
        for f in files:
            try:
                for b in JsonFileAdapter(f).bundles():
                    out = ingest_svc.ingest_bundle(cur, b)
                    all_changes.extend(out["changes"])
                    versions.append({"file": f.name, "source_key": b.source_key,
                                     "version_id": out["source_version_id"],
                                     "noop": out["noop"]})
            except AdapterError as e:
                raise HTTPException(422, f"{f.name}: {e}")
    return {"ingested": versions, "changes": all_changes}


@app.post("/api/admin/ingest-push")
def api_ingest_push(body: dict):
    """外部系统直接推送一个来源包（适配器契约的在线形态）。"""
    with tx() as (conn, cur):
        try:
            bundles = list(DictAdapter(body).bundles())
        except AdapterError as e:
            raise HTTPException(422, str(e))
        out_all = []
        for b in bundles:
            out = ingest_svc.ingest_bundle(cur, b)
            out_all.append({"source_key": b.source_key, "version_id": out["source_version_id"],
                            "noop": out["noop"], "changes": out["changes"]})
    return {"ok": True, "results": out_all}


@app.post("/api/admin/rollback")
def api_rollback(body: dict):
    """旧缓存回滚：恢复来源到旧 revision，再局部重算现有行程。"""
    source_key = body["source_key"]
    to_revision = body.get("to_revision")
    with tx() as (conn, cur):
        target_id = ingest_svc.rollback_source(cur, source_key, to_revision)
        cur.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1")
        plan_row = cur.fetchone()
        recomputed = None
        if plan_row:
            recomputed = plans_svc.recompute_incremental(
                cur, plan_row["id"],
                [{"change_type": "ferry_time"}, {"change_type": "ferry_cancel"},
                 {"change_type": "place_coord"}, {"change_type": "closure_added"},
                 {"change_type": "tide_missing"}],
                target_id)
    return {"rolled_back_to_version": target_id, "recompute": recomputed}


@app.post("/api/admin/offline-bundle")
def api_make_bundle(body: dict):
    date = body.get("date", "2026-10-03")
    with tx() as (conn, cur):
        out = offline_svc.build_bundle(cur, date)
    return JSONResponse(json.loads(json.dumps(out, ensure_ascii=False, default=str)))


@app.get("/api/admin/conflicts")
def api_conflicts():
    with tx() as (conn, cur):
        data = admin_payload(cur)
    return {"conflicts": data["conflicts"]}


# ---------- 开放资料下载 ----------
@app.get("/open-data/places.csv")
def open_places_csv():
    with tx() as (conn, cur):
        cur.execute("SELECT code,name,kind,lat,lon,timezone FROM places ORDER BY code")
        rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "name", "kind", "lat", "lon", "timezone"])
    for r in rows:
        w.writerow([r["code"], r["name"], r["kind"], float(r["lat"]), float(r["lon"]), r["timezone"]])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=places.csv"})


@app.get("/open-data/ferries.json")
def open_ferries_json():
    with tx() as (conn, cur):
        cur.execute("SELECT mode,route_code,date,depart_place,arrive_place,depart_at,arrive_at,status "
                    "FROM ferry_records ORDER BY depart_at")
        rows = cur.fetchall()
    out = [{"mode": r["mode"], "route_code": r["route_code"], "date": str(r["date"]),
            "depart_place": r["depart_place"], "arrive_place": r["arrive_place"],
            "depart_at": r["depart_at"].isoformat(), "arrive_at": r["arrive_at"].isoformat(),
            "status": r["status"]} for r in rows]
    return JSONResponse({"license": "CC BY 4.0", "notice": OPEN_DATA_NOTICE, "records": out})


@app.get("/health")
def health():
    return {"ok": True}
