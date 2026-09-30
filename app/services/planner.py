"""行程规划器。

两套模式（需求明确要求比较）：
- static：一次生成静态行程。从时段(slot)模板组装访问段，一次性判定全部段与接驳。
- incremental：数据变更后只重算「受影响集合」(直接影响段 + 其后连带段)，
  用户锁定段不被移动，只标注。

关键安全规则：
- 潮汐坐标/时区/适用范围必须与地点匹配，否则该段不可行；缺测(level NULL) 不可推断为可通行。
- 临时封闭只让对应访问段「待调整 pending_adjustment」，不是全局作废；
  并重新计算其后接驳时间，记录连带影响。
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timedelta

from ..config import MIN_TRANSFER_MINUTES, OVERNIGHT_GAP_HOURS

WALK_IN_MINUTES = 10  # 下车步行进入参观/潮间带

# 时段模板：按时段组合访问段
SLOT_TEMPLATE = [
    {"slot_code": "morning_ferry", "kind": "ferry", "title": "上午船班进岛",
     "from_place": "MAIN_DOCK", "to_place": "ISLAND_DOCK", "route_hint": "F1",
     "window": ("06:00", "11:00"), "allow_overnight": True},
    {"slot_code": "dock_to_lh", "kind": "shuttle", "title": "码头→灯塔接驳",
     "from_place": "ISLAND_DOCK", "to_place": "LIGHTHOUSE", "route_hint": "S1",
     "window": ("08:30", "12:00"), "allow_overnight": True},
    {"slot_code": "lighthouse_visit", "kind": "visit", "title": "北礁灯塔参观",
     "place": "LIGHTHOUSE", "duration_min": 60, "window": ("09:35", "14:00")},
    {"slot_code": "reef_window", "kind": "tide_visit", "title": "五虎礁潮间带（低潮窗口）",
     "place": "REEF", "duration_min": 75, "window": ("10:30", "15:00")},
    {"slot_code": "lh_to_dock", "kind": "shuttle", "title": "灯塔→码头接驳",
     "from_place": "LIGHTHOUSE", "to_place": "ISLAND_DOCK", "route_hint": "S2",
     "window": ("14:30", "16:30")},
    {"slot_code": "return_ferry", "kind": "ferry", "title": "下午船班返陆",
     "from_place": "ISLAND_DOCK", "to_place": "MAIN_DOCK", "route_hint": "F2",
     "window": ("15:30", "19:00")},
]

# 潮汐点与地点坐标匹配阈值（公里）
TIDE_MATCH_KM = 2.0


def _parse_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class DataView:
    """一次规划看到的「权威数据」快照（裁决后的当前行 + 版本 id）。"""

    def __init__(self, cur, at_date: str):
        self.at_date = at_date
        cur.execute("SELECT * FROM places")
        self.places = {r["code"]: r for r in cur.fetchall()}
        cur.execute("SELECT * FROM tide_points")
        self.tide_points = {r["code"]: r for r in cur.fetchall()}
        cur.execute("SELECT * FROM ferry_records WHERE date=%s AND active=TRUE", (at_date,))
        rows = cur.fetchall()
        # 按来源优先级裁决：同一业务键保留最高优先级来源的行
        self.transports = []
        best: dict[str, dict] = {}
        for r in rows:
            cur.execute("SELECT priority FROM source_versions WHERE id=%s", (r["source_version_id"],))
            pri = cur.fetchone()["priority"]
            r = dict(r)
            r["_priority"] = pri
            key = f"{r['mode']}|{r['route_code']}|{r['date']}"
            if key not in best or pri > best[key]["_priority"]:
                best[key] = r
        self.transports = list(best.values())
        cur.execute("SELECT * FROM closure_records WHERE active=TRUE AND starts_at::date <= %s AND ends_at::date >= %s",
                    (at_date, at_date))
        self.closures = cur.fetchall()
        cur.execute("SELECT * FROM tide_records WHERE active=TRUE")
        self.tides = cur.fetchall()


def _active_closures(view: DataView, target_type: str, target_code: str):
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    day = datetime.fromisoformat(view.at_date).replace(tzinfo=tz)
    out = []
    for c in view.closures:
        st = c["starts_at"]; en = c["ends_at"]
        if c["target_type"] == target_type and c["target_code"] == target_code:
            if st <= day + timedelta(days=1) and en >= day:
                out.append(c)
    return out


def _find_transport(view: DataView, mode: str, frm: str, to: str,
                    window: tuple[str, str] | None = None,
                    not_before: datetime | None = None,
                    allow_overnight: bool = False):
    """在时段窗口（及到达锚定 not_before）内选班次。

    返回 (pick, cands)：cands 为该 OD 当日全部班次（含取消），
    pick 为窗口+锚定之后最早的一班 scheduled。
    """
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    wstart = wend = None
    if window:
        wh1, wh2 = window
        wstart = datetime.fromisoformat(view.at_date).replace(
            hour=int(wh1[:2]), minute=int(wh1[3:]), tzinfo=tz)
        wend = datetime.fromisoformat(view.at_date).replace(
            hour=int(wh2[:2]), minute=int(wh2[3:]), tzinfo=tz)
    cands = []
    for t in view.transports:
        if t["mode"] != mode or t["depart_place"] != frm or t["arrive_place"] != to:
            continue
        dep = t["depart_at"]
        in_window = wstart <= dep <= wend if wstart else True
        if wstart and not in_window:
            if not allow_overnight:
                continue
            same_day_evening = (dep.date() == wstart.date() and dep.hour >= 18)
            next_early = (dep.date() == wstart.date() + timedelta(days=1) and dep.hour < 6)
            if not (same_day_evening or next_early):
                continue
        if not_before and dep < not_before:
            continue
        cands.append(t)
    cands.sort(key=lambda t: t["depart_at"])
    scheduled = [c for c in cands if c["status"] == "scheduled"]
    pick = scheduled[0] if scheduled else None
    return pick, cands


def evaluate_tide_segment(view: DataView, seg_tmpl: dict, anchor_arrival=None) -> dict:
    """潮汐段判定：坐标/时区/适用范围匹配 + 窗口内存在安全低潮；缺测绝不放行。"""
    place = view.places.get(seg_tmpl["place"])
    ev = {"state": "infeasible", "reason_codes": [], "evidence": [], "record_ref": None,
          "starts_at": None, "ends_at": None}
    if not place:
        ev["reason_codes"].append("place_not_found")
        return ev
    tp_code = place.get("tide_point_code")
    tp = view.tide_points.get(tp_code) if tp_code else None
    if not tp:
        ev["reason_codes"].append("no_tide_point_bound")
        ev["evidence"].append({"type": "place", "code": place["code"],
                               "source_version_id": place["source_version_id"]})
        return ev
    # 1) 适用范围
    if place["code"] not in (tp["applies_to"] or []):
        ev["reason_codes"].append("tide_applies_to_mismatch")
    # 2) 时区
    if tp["timezone"] != place["timezone"]:
        ev["reason_codes"].append("tide_timezone_mismatch")
    # 3) 坐标匹配
    dist = _haversine_km(float(place["lat"]), float(place["lon"]),
                         float(tp["lat"]), float(tp["lon"]))
    if dist > TIDE_MATCH_KM:
        ev["reason_codes"].append("tide_coord_mismatch")
    ev["evidence"].append({"type": "place", "code": place["code"],
                           "lat": float(place["lat"]), "lon": float(place["lon"]),
                           "timezone": place["timezone"],
                           "source_version_id": place["source_version_id"]})
    ev["evidence"].append({"type": "tide_point", "code": tp["code"],
                           "lat": float(tp["lat"]), "lon": float(tp["lon"]),
                           "timezone": tp["timezone"], "applies_to": list(tp["applies_to"]),
                           "distance_km": round(dist, 3), "max_safe_cm": tp["max_safe_cm"],
                           "source_version_id": tp["source_version_id"]})
    if ev["reason_codes"]:
        return ev

    # 窗口内低潮判定
    wh1, wh2 = seg_tmpl["window"]
    tzname = place["timezone"]
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tzname)
    wstart = datetime.fromisoformat(view.at_date).replace(hour=int(wh1[:2]), minute=int(wh1[3:]), tzinfo=tz)
    wend = datetime.fromisoformat(view.at_date).replace(hour=int(wh2[:2]), minute=int(wh2[3:]), tzinfo=tz)
    lows = [t for t in view.tides
            if t["tide_point_code"] == tp["code"] and t["kind"] == "low"
            and wstart <= _parse_dt(t["observed_at"]) <= wend]
    # 缺测：只要该潮位点窗口内缺测，绝不推断可通行
    missing = [t for t in lows if t["level_cm"] is None]
    valid = [t for t in lows if t["level_cm"] is not None]
    for t in lows:
        ev["evidence"].append({"type": "tide_obs", "point": t["tide_point_code"],
                               "observed_at": _parse_dt(t["observed_at"]).isoformat(),
                               "level_cm": t["level_cm"], "kind": t["kind"],
                               "source_version_id": t["source_version_id"]})
    if not lows:
        ev["reason_codes"].append("no_low_tide_in_window")
        return ev
    if missing and not valid:
        ev["reason_codes"].append("tide_data_missing")
        return ev
    safe = [t for t in valid if t["level_cm"] <= tp["max_safe_cm"]]
    if not safe:
        ev["reason_codes"].append("tide_level_unsafe")
        return ev
    chosen = min(safe, key=lambda t: t["level_cm"])
    start = _parse_dt(chosen["observed_at"])
    if anchor_arrival and start < anchor_arrival:
        later_safe = [t for t in safe if _parse_dt(t["observed_at"]) >= anchor_arrival]
        if later_safe:
            chosen = min(later_safe, key=lambda t: t["level_cm"])
            start = _parse_dt(chosen["observed_at"])
    ev.update(state="feasible",
              starts_at=start,
              ends_at=start + timedelta(minutes=seg_tmpl["duration_min"]),
              record_ref={"tide_record_point": chosen["tide_point_code"],
                          "observed_at": chosen["observed_at"].isoformat(),
                          "level_cm": chosen["level_cm"],
                          "source_version_id": chosen["source_version_id"]})
    return ev



def _alt_record(i, c, tmpl):
    return {
        "rank": i + 1, "kind": tmpl["kind"],
        "title": f"备选班次 {c['route_code']}（{c['depart_at']:%m-%d %H:%M}）",
        "starts_at": c["depart_at"], "ends_at": c["arrive_at"],
        "record_ref": {"mode": c["mode"], "route_code": c["route_code"],
                       "source_version_id": c["source_version_id"],
                       "depart_at": c["depart_at"].isoformat(),
                       "arrive_at": c["arrive_at"].isoformat()},
        "evidence": [{"type": "transport", "route_code": c["route_code"],
                      "status": c["status"], "source_version_id": c["source_version_id"]}]}


def evaluate_segment(cur, view: DataView, tmpl: dict, anchor_arrival=None,
                     rewindow: bool = False) -> dict:
    """评估单段，返回状态/时间/证据/候选。"""
    result = {"slot_code": tmpl["slot_code"], "kind": tmpl["kind"], "title": tmpl["title"],
              "from_place": tmpl.get("from_place"), "to_place": tmpl.get("to_place"),
              "state": "infeasible", "infeasible_reason": None, "reason_codes": [],
              "starts_at": None, "ends_at": None, "record_ref": None,
              "evidence": [], "alternatives": []}
    rcs = result["reason_codes"]

    if tmpl["kind"] in ("ferry", "shuttle"):
        # 两段择班：先找不早于到达的窗口班次；采用替代下游时若没有，
        # 再退回窗口内任意计划班次（接驳会如实标记时间冲突）
        pick, cands = _find_transport(view, tmpl["kind"], tmpl["from_place"],
                                      tmpl["to_place"], window=tmpl.get("window"),
                                      not_before=anchor_arrival,
                                      allow_overnight=tmpl.get("allow_overnight", False))
        if rewindow and not pick:
            pick, cands = _find_transport(view, tmpl["kind"], tmpl["from_place"],
                                          tmpl["to_place"], window=tmpl.get("window"),
                                          allow_overnight=tmpl.get("allow_overnight", False))
        # 该 OD 当日全部班次（含窗口外/取消），用于证据与替代候选展示
        _, all_cands = _find_transport(view, tmpl["kind"], tmpl["from_place"],
                                       tmpl["to_place"])
        closures = _active_closures(view, "route",
                                    f"{tmpl['kind']}:{tmpl['from_place']}->{tmpl['to_place']}")
        for c in all_cands:
            result["evidence"].append({
                "type": "transport", "mode": c["mode"], "route_code": c["route_code"],
                "depart_at": c["depart_at"].isoformat(), "arrive_at": c["arrive_at"].isoformat(),
                "status": c["status"], "source_version_id": c["source_version_id"],
                "priority": c["_priority"], "in_window": c in cands})
        if closures:
            cl = closures[0]
            result["state"] = "pending_adjustment"
            rcs.append("route_closure")
            result["infeasible_reason"] = f"线路临时封闭：{cl['reason'] or cl['closure_key']}（{cl['starts_at']:%m-%d %H:%M} 至 {cl['ends_at']:%m-%d %H:%M}），该段待调整"
            result["evidence"].append({"type": "closure", "closure_key": cl["closure_key"],
                                       "source_version_id": cl["source_version_id"]})
            return result
        if not cands:
            rcs.append("no_service")
            result["infeasible_reason"] = "时段窗口内无任何班次记录"
            return result
        # 窗口内取消班次 = 该时段原定服务被取消
        cancelled_in_window = [c for c in cands if c["status"] == "cancelled"]
        pick_route = pick["route_code"] if pick else None
        cancelled_routes = {c["route_code"] for c in cancelled_in_window}
        earlier_cancel = [c for c in cancelled_in_window
                          if not pick or c["depart_at"] <= pick["depart_at"]]

        def _is_alt(c):
            if c["status"] != "scheduled":
                return False
            if c["route_code"] in cancelled_routes:
                return False
            # 若存在取消班次，则任何其它计划班次（含最早计划班）都是替代候选
            if cancelled_in_window:
                return True
            return c["route_code"] != pick_route

        other_scheduled = [c for c in all_cands if _is_alt(c)]
        if not pick or earlier_cancel:
            cancelled = earlier_cancel or cancelled_in_window
            rcs.append("ferry_cancelled" if tmpl["kind"] == "ferry" else "shuttle_cancelled")
            names = ", ".join(c["route_code"] for c in cancelled) or "该时段班次"
            tail = f"，替代候选 {', '.join(c['route_code'] for c in other_scheduled)} 可调整" \
                if other_scheduled else "，且无当日备选"
            result["infeasible_reason"] = f"班次 {names} 已取消{tail}；该段不可直接通行"
            for i, c in enumerate(other_scheduled):
                result["alternatives"].append(_alt_record(i, c, tmpl))
            return result
        result["state"] = "feasible"
        result["starts_at"] = pick["depart_at"]
        result["ends_at"] = pick["arrive_at"]
        result["record_ref"] = {"mode": pick["mode"], "route_code": pick["route_code"],
                                "source_version_id": pick["source_version_id"],
                                "depart_at": pick["depart_at"].isoformat(),
                                "arrive_at": pick["arrive_at"].isoformat()}
        # 备选：同 OD 其它计划班次
        for i, c in enumerate(other_scheduled):
            result["alternatives"].append(_alt_record(i, c, tmpl))
        return result

    if tmpl["kind"] == "visit":
        place = view.places.get(tmpl["place"])
        closures = _active_closures(view, "place", tmpl["place"])
        result["evidence"].append({"type": "place", "code": tmpl["place"],
                                   "source_version_id": place["source_version_id"] if place else None})
        if closures:
            cl = closures[0]
            result["state"] = "pending_adjustment"
            rcs.append("place_closure")
            result["infeasible_reason"] = f"{place['name'] if place else tmpl['place']} 临时封闭（{cl['reason'] or cl['closure_key']}），该段待调整"
            result["evidence"].append({"type": "closure", "closure_key": cl["closure_key"],
                                       "source_version_id": cl["source_version_id"]})
            return result
        # 时间由相邻接驳推算；静态时取「到达锚定」与窗口起点的较晚者
        import zoneinfo
        tz = zoneinfo.ZoneInfo(place["timezone"])
        wh1 = tmpl["window"][0]
        base = datetime.fromisoformat(view.at_date).replace(
            hour=int(wh1[:2]), minute=int(wh1[3:]), tzinfo=tz)
        st = max(base, anchor_arrival + timedelta(minutes=MIN_TRANSFER_MINUTES)) if anchor_arrival else base
        result["state"] = "feasible"
        result["starts_at"] = st
        result["ends_at"] = st + timedelta(minutes=tmpl["duration_min"])
        return result

    if tmpl["kind"] == "tide_visit":
        ev = evaluate_tide_segment(view, tmpl, anchor_arrival=anchor_arrival)
        result["state"] = ev["state"]
        rcs.extend(ev["reason_codes"])
        result["evidence"] = ev["evidence"]
        result["record_ref"] = ev["record_ref"]
        result["starts_at"] = ev["starts_at"]
        result["ends_at"] = ev["ends_at"]
        if ev["state"] == "infeasible":
            labels = {
                "no_tide_point_bound": "地点未绑定验潮站",
                "tide_applies_to_mismatch": "验潮站适用范围不含该地点",
                "tide_timezone_mismatch": "潮汐时区与地点不一致",
                "tide_coord_mismatch": "潮汐点坐标与地点不匹配（超阈值）",
                "no_low_tide_in_window": "时段内无低潮记录",
                "tide_data_missing": "潮汐缺测：不能推断可通行",
                "tide_level_unsafe": "低潮水位高于安全线",
                "place_not_found": "地点不存在"}
            result["infeasible_reason"] = "；".join(labels.get(c, c) for c in ev["reason_codes"])
        return result

    return result


def evaluate_connections(segments: list[dict]) -> list[dict]:
    """根据相邻段实际时刻计算接驳；标记跨日与不可行。"""
    conns = []
    for a, b in zip(segments, segments[1:]):
        conn = {"from_segment_ref": a.get("id"), "to_segment_ref": b.get("id"),
                "from_slot": a["slot_code"], "to_slot": b["slot_code"],
                "feasible": False, "gap_minutes": None, "cross_day": False, "detail": "",
                "knock_on": False, "knock_on_detail": None}
        if not a.get("ends_at") or not b.get("starts_at"):
            conn["detail"] = "上游段时间未定，接驳无法成立"
            conns.append(conn)
            continue
        gap = (b["starts_at"] - a["ends_at"]).total_seconds() / 60
        conn["gap_minutes"] = int(gap)
        conn["cross_day"] = a["ends_at"].date() != b["starts_at"].date()
        if gap < 0:
            conn["detail"] = f"时间冲突：下一段出发比上一段到达早 {abs(int(gap))} 分钟"
        elif gap < MIN_TRANSFER_MINUTES:
            conn["detail"] = f"接驳缓冲仅 {int(gap)} 分钟，少于 {MIN_TRANSFER_MINUTES} 分钟"
        else:
            conn["feasible"] = True
            tag = "（跨日接驳）" if conn["cross_day"] else ""
            overnight = ""
            if conn["cross_day"]:
                overnight = f"，跨日等待 {int(gap)} 分钟，需确认夜间接驳/末班"
            conn["detail"] = f"缓冲 {int(gap)} 分钟{tag}{overnight}"
        conns.append(conn)
    return conns
