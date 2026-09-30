// 行程规划与局部重算引擎
//
// 两种模式：
//  1) staticPlan  : 一次生成静态行程——按当时的数据版本水位计算，结果整体冻结；
//  2) recompute   : 依赖数据变更后只重算受影响访问段（局部重算），
//                   未变段保持；被用户锁定的段不改写，只标注连带影响。
//
// 硬性规则：
//  * 潮汐坐标/时区/适用范围不匹配，或缺测覆盖不足 => unknown => 不可推断可通行；
//  * 临时封闭仅使对应访问段进入 pending_adjustment，其他段只收接驳连带说明；
//  * 不同来源矛盾 => 保守处理（有取消即取消；时刻冲突即不可信）。
import { one, query } from '../db.js';
import {
  localToUtc, utcToLocal, addMinutes, distanceM, evaluateTide, findTideWindows, isoUtc,
} from './util.js';

export const CONNECT_MIN_MIN = 10; // 接驳最小间隔
const dateStr = (v) => (v instanceof Date ? v.toISOString().slice(0, 10) : String(v).slice(0, 10));
const timeStr = (v) => /^\d{2}:\d{2}/.test(String(v)) ? String(v).slice(0, 5) : '00:00';

// ----------------------- 行程模板（按时段组合访问段） -----------------------
// dayOffset 支持跨日接驳模板。
const TEMPLATES = {
  // 日间灯塔游（基线日 2026-10-01）
  'lighthouse-day': {
    tz: 'Asia/Shanghai', offsetMin: 480,
    slots: [
      { key: 's1', type: 'access', title: '去程客轮', schedule: 'F101' },
      { key: 's2', type: 'access', title: '码头→北堤接驳巴士', schedule: 'B31' },
      { key: 's3', type: 'access', title: '步行穿越北堤潮间堤道', foot: 'causeway-north', to: 'viewpoint-lighthouse', needMin: 100, bufferBeforeMin: CONNECT_MIN_MIN },
      { key: 's4', type: 'visit', title: '百年灯塔观景台游览(避开正午高潮)', place: 'viewpoint-lighthouse', durationMin: 250, bufferBeforeMin: 0 },
      { key: 's5', type: 'access', title: '步行返回北堤入口(下午低潮窗)', foot: 'causeway-north', from: 'viewpoint-lighthouse', reverse: true, needMin: 90, bufferBeforeMin: 0 },
      { key: 's6', type: 'visit', title: '游客服务中心简餐与开放资料', place: 'facility-center', durationMin: 20, bufferBeforeMin: 20 },
      { key: 's7', type: 'access', title: '北堤→码头接驳巴士', schedule: 'B32', bufferBeforeMin: CONNECT_MIN_MIN },
      { key: 's8', type: 'access', title: '回程客轮', schedule: 'F201' },
    ],
  },
  // 跨日接驳模板（用于验证跨日）：傍晚船去、末班船跨 00:00 返
  'cross-day': {
    tz: 'Asia/Shanghai', offsetMin: 480,
    slots: [
      { key: 'x1', type: 'access', title: '傍晚去程客轮', schedule: 'F900' },
      { key: 'x2', type: 'visit', title: '夜宿灯台岛码头驿站', place: 'facility-center', durationMin: 50, bufferBeforeMin: 20 },
      { key: 'x3', type: 'access', title: '跨午夜末班回程(次日抵达)', schedule: 'F901', bufferBeforeMin: 185 },
    ],
  },
};

export function listTemplates() {
  return Object.fromEntries(Object.entries(TEMPLATES).map(([k, v]) => [k, { tz: v.tz, slots: v.slots.length }]));
}

// ----------------------- 数据装载（按版本水位） -----------------------
async function loadWorld(db, planDate, versionHighWater = null)
{
  const vw = (t) => versionHighWater && versionHighWater[t] ? `AND sv.id <= ${Number(versionHighWater[t])}` : '';
  const latestSchedule = async (id) =>
    one(db, `
      SELECT s.*, sv.id AS svid, sv.source_key FROM schedules s
      JOIN source_versions sv ON sv.id=s.source_version_id
      WHERE s.id=$1 AND s.dep_date = $2::date ${vw('ferry')}
      ORDER BY s.source_version_id DESC LIMIT 1`, [id, planDate]);
  const activeClosures = async () =>
    query(db, `
      SELECT c.* FROM closures c WHERE ${versionHighWater?.closure ? `c.source_version_id <= ${Number(versionHighWater.closure)} AND` : ''} true`);
  const place = async (id) => one(db, `SELECT * FROM places WHERE id=$1`, [id]);
  const stationLatest = async (id) =>
    one(db, `SELECT * FROM tide_stations WHERE id=$1 ${versionHighWater?.tide ? `AND source_version_id <= ${Number(versionHighWater.tide)}` : ''}
             ORDER BY source_version_id DESC LIMIT 1`, [id]);
  const tidePoints = async (stationId, stationVer) => {
    const rows = await query(db,
      `SELECT ts_utc, height_m, kind, source_version_id FROM tide_points
       WHERE station_id=$1 AND station_version_id=$2 ORDER BY ts_utc`, [stationId, stationVer]);
    return rows.map((r) => ({ ts: new Date(r.ts_utc).getTime(), height: r.height_m, kind: r.kind, svid: r.source_version_id }));
  };
  const threshold = async (placeId) =>
    one(db, `SELECT * FROM tide_thresholds WHERE place_id=$1`, [placeId]);

  // 同业务日、同班次的所有来源版本（用于矛盾检测）
  const conflictingSchedules = async (id) =>
    query(db, `
      SELECT s.*, sv.source_key, sv.id svid FROM schedules s
      JOIN source_versions sv ON sv.id=s.source_version_id
      WHERE s.id=$1 AND s.dep_date = $2::date ${vw('ferry')}
      ORDER BY s.source_version_id`, [id, planDate]);

  return { planDate, latestSchedule, activeClosures, place, stationLatest, tidePoints, threshold, conflictingSchedules };
}

// 取班次“有效时刻/状态”，含跨源矛盾保守判定
async function resolveSchedule(world, id, dateYmd, offsetMin) {
  const rows = await world.conflictingSchedules(id);
  const evidence = [];
  if (!rows.length) return { found: false, evidence: [{ condition_type: 'schedule', verdict: 'fail', detail: { schedule_id: id, reason: 'missing' } }] };
  const latest = rows[rows.length - 1];
  const sources = [...new Set(rows.map((r) => r.source_key))];
  const times = new Set(rows.map((r) => `${dateStr(r.dep_date)} ${timeStr(r.dep_local)}-${timeStr(r.arr_local)}`));
  // 规则：最新版本若声明取消 => 权威取消（旧来源仍排船不构成“矛盾”，只是被取代）。
  // 仅当【同为最新时间点的不同来源】给出互斥的“在航”信息（不同时刻且都未取消）时，才判矛盾 => 保守不可信。
  const scheduledRows = rows.filter((r) => r.status !== 'cancelled');
  const multiSourceLive = new Set(scheduledRows.map((r) => r.source_key)).size > 1;
  const timeConflict = multiSourceLive && new Set(scheduledRows.map((r) =>
    `${dateStr(r.dep_date)} ${timeStr(r.dep_local)} ${timeStr(r.arr_local)}`)).size > 1;

  for (const r of rows) {
    evidence.push({
      condition_type: 'schedule', ref_type: 'schedule', source_version_id: r.svid,
      detail: { schedule_id: id, source: r.source_key, dep: `${dateStr(r.dep_date)} ${timeStr(r.dep_local)}`, arr: timeStr(r.arr_local), status: r.status },
      verdict: r.status === 'cancelled' ? 'fail' : 'pass',
    });
  }
  if (latest.status === 'cancelled') {
    return { found: true, status: 'cancelled', schedule: latest, evidence,
      infeasibleReason: { code: 'cancelled', message: `班次 ${id}（${latest.route_name}）已被最新来源 ${latest.source_key} 取消` } };
  }
  if (timeConflict) {
    evidence.push({ condition_type: 'source_conflict', verdict: 'fail', detail: { schedule_id: id, variants: [...times] } });
    return { found: true, status: 'conflict', schedule: latest, evidence,
      infeasibleReason: { code: 'source_conflict', message: `班次 ${id} 在多个来源间时刻矛盾（${[...new Set(scheduledRows.map(r=>r.source_key))].join('、')}），且无取消声明，按保守原则不予采用` } };
  }
  // 以班次自身 dep_date（可能跨日模板中落在次日），没有则用 planDate
  const depDate = dateStr(latest.dep_date) || dateYmd;
  const depUTC = localToUtc(depDate, String(latest.dep_local).slice(0, 5), offsetMin);
  const { dateYmd: arrDate, hhmm: arrHH, dayShift } = addMinutes(depDate, timeStr(latest.dep_local),
    minutesBetween(timeStr(latest.dep_local), timeStr(latest.arr_local)));
  const arrUTC = localToUtc(arrDate, arrHH, offsetMin);
  return { found: true, status: 'scheduled', schedule: latest, depUTC, arrUTC, depDate, dayShift, evidence };
}
function minutesBetween(depHH, arrHH) {
  const [dh, dm] = depHH.split(':').map(Number);
  const [ah, am] = arrHH.split(':').map(Number);
  let gap = ah * 60 + am - (dh * 60 + dm);
  if (gap < 0) gap += 1440; // 到达挂钟早于出发 => 跨日
  return gap;
}

// ----------------------- 段评估 -----------------------
async function evalSlot(ctx, slot, prevEndUTC, prevTitle) {
  const { db, world, dateYmd, tpl, collect } = ctx;
  const reasons = [], evidence = [];
  let startUTC = null, endUTC = null, placeId = null, scheduleId = null;

  const pushEv = (e) => evidence.push(e);

  if (slot.schedule) {
    const r = await resolveSchedule(world, slot.schedule, dateYmd, tpl.offsetMin);
    evidence.push(...r.evidence);
    scheduleId = slot.schedule;
    if (!r.found || r.status !== 'scheduled') {
      reasons.push(r.infeasibleReason ?? { code: 'missing', message: `班次 ${slot.schedule} 无数据` });
    } else {
      startUTC = r.depUTC; endUTC = r.arrUTC;
    }
  } else {
    // visit / foot：起点从前一段结束 + buffer 推算
    const place = await world.place(slot.place || slot.foot);
    placeId = place?.id ?? (slot.place || slot.foot);
    if (!place) {
      reasons.push({ code: 'place_missing', message: `地点 ${placeId} 不存在` });
    } else {
      const base = prevEndUTC;
      if (base) {
        startUTC = new Date(base.getTime() + (slot.bufferBeforeMin ?? 0) * 60000);
        const dur = slot.needMin ?? slot.durationMin ?? 60;
        endUTC = new Date(startUTC.getTime() + dur * 60000);
      }
      // base 为空时由文末统一接驳检查给出 no_anchor 原因与证据
      // 封闭检查（仅对地点类段）
      if (startUTC && endUTC) {
        const closures = await world.activeClosures();
        const hit = closures.find((c) => c.place_id === placeId &&
          startUTC < new Date(c.end_utc) && new Date(c.start_utc) < endUTC);
        if (hit) {
          pushEv({ condition_type: 'closure', ref_type: 'closure', source_version_id: hit.source_version_id,
            verdict: 'fail', detail: { closure_id: hit.id, place_id: hit.place_id, reason: hit.reason,
              window: `${hit.start_utc} ~ ${hit.end_utc}` } });
          reasons.push({ code: 'closed', message: `${place.name} 临时封闭：${hit.reason ?? '未注明原因'}（${new Date(hit.start_utc).toISOString()} 至 ${new Date(hit.end_utc).toISOString()}）`, closure_id: hit.id });
        }
      }
      // 潮汐检查（步行穿越堤道）
      if (slot.foot && startUTC && endUTC) {
        const tideCheck = await checkTide(world, place, startUTC, endUTC, pushEv);
        if (tideCheck.verdict !== 'pass') {
          reasons.push(tideCheck.reason);
        }
      }
    }
  }

  // 与上一段的接驳约束
  let connection = null;
  if (!startUTC && !slot.schedule) {
    // 时间锚定段无法确定开始时刻（上游班次失效等）
    pushEv({ condition_type: 'connection', verdict: 'fail', detail: { to: slot.title, reason: '上游时间锚缺失' } });
    reasons.push({ code: 'no_anchor', message: `「${slot.title}」依赖上游班次时刻，但接驳链已断，时间无法确定，需先调整上游段` });
  }
  if (startUTC && prevEndUTC) {
    const gapMin = (startUTC - prevEndUTC) / 60000;
    const min = slot.bufferBeforeMin ?? 0;
    const verdict = gapMin + 1e-9 >= min ? 'pass' : 'fail';
    connection = { gapMin: Math.round(gapMin), requiredMin: min, verdict };
    pushEv({ condition_type: 'connection', verdict,
      detail: { from: prevTitle, to: slot.title, gapMin: Math.round(gapMin), requiredMin: min } });
    if (verdict === 'fail') {
      reasons.push({ code: 'connection_tight', message: `与上一段「${prevTitle}」接驳仅 ${Math.round(gapMin)} 分钟，不足 ${min} 分钟` });
    }
  }

  // 不可行原因里附带接驳连带影响（供后续段说明）
  const status = reasons.length ? 'infeasible' : 'feasible';
  const seg = {
    slot_key: slot.key, type: slot.type, title: slot.title, place_id: placeId,
    schedule_id: scheduleId, start_utc: startUTC ? isoUtc(startUTC) : null,
    end_utc: endUTC ? isoUtc(endUTC) : null,
    status, infeasible_reasons: reasons, evidence, connection,
    local: startUTC ? {
      start: utcToLocal(startUTC, tpl.offsetMin),
      end: endUTC ? utcToLocal(endUTC, tpl.offsetMin) : null,
    } : null,
  };
  return seg;
}

async function checkTide(world, place, startUTC, endUTC, pushEv) {
  const stationId = place.tide_station_id;
  if (!stationId) {
    pushEv({ condition_type: 'tide_missing', verdict: 'unknown', detail: { place_id: place.id, reason: '地点未绑定潮位站' } });
    return { verdict: 'unknown', reason: { code: 'tide_unknown', message: `${place.name} 未绑定潮位站，不能推断潮汐可通行` } };
  }
  const station = await world.stationLatest(stationId);
  if (!station) {
    pushEv({ condition_type: 'tide_missing', verdict: 'unknown', detail: { station_id: stationId, reason: '潮位站无数据' } });
    return { verdict: 'unknown', reason: { code: 'tide_unknown', message: `潮位站 ${stationId} 无数据，不能推断可通行` } };
  }
  // 坐标/时区/适用范围校验
  const errs = [];
  if (station.tz !== place.tz) errs.push(`时区(${station.tz} vs ${place.tz})`);
  if (!station.applies_to.includes(place.id)) errs.push('适用范围未覆盖该地点');
  const d = distanceM(place.lat, place.lng, station.lat, station.lng);
  if (d > (station.max_match_distance_m ?? 2000)) errs.push(`坐标相距 ${Math.round(d)}m 超出匹配范围`);
  pushEv({ condition_type: 'coords_match', verdict: errs.length ? 'fail' : 'pass',
    source_version_id: station.source_version_id,
    detail: { place: { lat: place.lat, lng: place.lng, tz: place.tz }, station: { id: station.id, lat: station.lat, lng: station.lng, tz: station.tz, applies_to: station.applies_to }, distanceM: Math.round(d) } });
  if (errs.length) {
    pushEv({ condition_type: 'station_scope', verdict: 'fail', detail: { errors: errs } });
    return { verdict: 'unknown', reason: { code: 'tide_station_mismatch', message: `潮位信息与地点不匹配（${errs.join('；')}），不推断可通行` } };
  }
  const th = await world.threshold(place.id);
  if (!th) {
    pushEv({ condition_type: 'tide_missing', verdict: 'unknown', detail: { reason: '缺少通行水位阈值' } });
    return { verdict: 'unknown', reason: { code: 'tide_unknown', message: `${place.name} 缺少通行水位阈值` } };
  }
  const points = await world.tidePoints(stationId, station.source_version_id);
  const res = evaluateTide(points, startUTC, endUTC, th.max_height_m);
  pushEv({ condition_type: 'tide_window', source_version_id: station.source_version_id,
    verdict: res.verdict === 'pass' ? 'pass' : res.verdict === 'fail' ? 'fail' : 'unknown',
    detail: { station_id: stationId, threshold_m: th.max_height_m, need_window: [isoUtc(startUTC), isoUtc(endUTC)], result: res } });
  if (res.verdict === 'unknown') {
    return { verdict: 'unknown', reason: { code: 'tide_unknown', message: `潮位缺测/未覆盖步行时段（${res.gap || '覆盖不足'}），按规则不推断可通行` } };
  }
  if (res.verdict === 'fail') {
    return { verdict: 'fail', reason: { code: 'tide_too_high', message: `步行窗口内最高水位 ${res.maxHeightSeen}m 超过阈值 ${th.max_height_m}m，堤道被淹` } };
  }
  return { verdict: 'pass' };
}

// ----------------------- 一次生成静态行程 -----------------------
export async function staticPlan(db, { templateKey = 'lighthouse-day', date = '2026-10-01', note = null }) {
  const tpl = TEMPLATES[templateKey];
  if (!tpl) throw new Error(`未知模板 ${templateKey}`);
  const dateYmd = String(date);
  // 记录当前各类数据版本水位
  const hw = {};
  for (const k of ['ferry', 'closure', 'tide', 'place', 'material']) {
    const r = await one(db, `SELECT MAX(id) id FROM source_versions WHERE kind=$1`, [k]);
    hw[k] = r?.id ?? null;
  }
  const world = await loadWorld(db, dateYmd, hw);
  const ctx = { db, world, dateYmd, tpl, collect: true };
  const segs = [];
  let prevEnd = null, prevTitle = null;
  for (const slot of tpl.slots) {
    const seg = await evalSlot(ctx, slot, prevEnd, prevTitle);
    seg.idx = segs.length;
    segs.push(seg);
    if (seg.end_utc) { prevEnd = new Date(seg.end_utc); prevTitle = slot.title; }
    else { prevEnd = null; } // 断链：后续时间锚定段也会失败
  }
  // 接驳连带影响传播（给失败段之后的段加注，不改变其可行性，除非断锚）
  annotateKnockOn(segs);

  const snap = { template_key: templateKey, date: dateYmd, tz: tpl.tz, version_high_water: hw, segments: segs };
  const row = await one(db,
    `INSERT INTO itineraries (plan_date,tz,created_from_version_id,static_snapshot,note)
     VALUES ($1,$2,$3,$4::jsonb,$5) RETURNING id`,
    [dateYmd, tpl.tz, hw.ferry ?? null, JSON.stringify(snap), note]);
  const itId = row.id;
  await persistSegments(db, itId, segs, 'static');
  return { id: itId, ...snap };
}

function annotateKnockOn(segs) {
  let firstBroken = null;
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    if (s.status === 'infeasible' && !firstBroken) firstBroken = i;
    if (firstBroken !== null && i > firstBroken && s.status !== 'infeasible') {
      // 仅当时间链依赖前段（非 schedule 自带时刻的段也会重新锚定到更早可行段）
      s.adjust_advice = `上游段「${segs[firstBroken].title}」待调整，本段时间可能随接驳顺延，需复核接驳时间。`;
    }
  }
}

async function persistSegments(db, itId, segs, mode) {
  for (const s of segs) {
    const row = await one(db,
      `INSERT INTO itinerary_segments
       (itinerary_id,slot_key,idx,type,title,place_id,start_utc,end_utc,schedule_id,
        static_start_utc,static_end_utc,static_status,current_status,infeasible_reasons,alternatives,adjust_advice)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$7,$8,$10,$10,$11::jsonb,$12::jsonb,$13)
       RETURNING id`,
      [itId, s.slot_key, s.idx, s.type, s.title, s.place_id ?? null,
       s.start_utc ? new Date(s.start_utc) : null, s.end_utc ? new Date(s.end_utc) : null,
       s.schedule_id ?? null, s.status, JSON.stringify(s.infeasible_reasons ?? []),
       JSON.stringify(s.alternatives ?? []), s.adjust_advice ?? null]);
    for (const e of s.evidence ?? []) {
      await one(db,
        `INSERT INTO segment_evidence (segment_id,scope,condition_type,ref_type,source_version_id,detail,verdict)
         VALUES ($1,'static',$2,$3,$4,$5::jsonb,$6)`,
        [row.id, e.condition_type, e.ref_type ?? null, e.source_version_id ?? null,
         JSON.stringify(e.detail ?? {}), e.verdict]);
    }
  }
}

// ----------------------- 数据变更 -> 局部重算 -----------------------
// 扫描“行程冻结版本水位之后”的新来源版本，仅重算受影响槽位。
export async function recompute(db, itineraryId, opts = {}) {
  const it = await one(db, `SELECT * FROM itineraries WHERE id=$1`, [itineraryId]);
  if (!it) throw new Error('行程不存在');
  const snap = typeof it.static_snapshot === 'string' ? JSON.parse(it.static_snapshot) : it.static_snapshot;
  const tpl = TEMPLATES[snap.template_key];
  const hw = snap.version_high_water;
  const dateYmd = it.plan_date instanceof Date ? it.plan_date.toISOString().slice(0,10) : String(it.plan_date).slice(0,10);

  // 找出所有高于水位的新版本
  const newVersions = await query(db,
    `SELECT sv.* FROM source_versions sv
      WHERE (sv.kind='ferry' AND sv.id > $1)
         OR (sv.kind='closure' AND sv.id > $2)
         OR (sv.kind='tide' AND sv.id > $3)
         OR (sv.kind='place' AND sv.id > $4)
      ORDER BY sv.id`,
    [hw.ferry ?? 0, hw.closure ?? 0, hw.tide ?? 0, hw.place ?? 0]);

  const world = await loadWorld(db, dateYmd); // 不限制水位：看当前世界
  const existing = await query(db, `SELECT * FROM itinerary_segments WHERE itinerary_id=$1 ORDER BY idx`, [itineraryId]);

  const changedSlots = new Set();
  const changeLog = [];
  for (const v of newVersions) {
    const raw = typeof v.raw_json === 'string' ? JSON.parse(v.raw_json) : v.raw_json;
    if (v.kind === 'ferry') {
      for (const trip of raw.trips ?? []) {
        const hitSlot = tpl.slots.find((s) => s.schedule === trip.id);
        if (hitSlot) { changedSlots.add(hitSlot.key); changeLog.push({ version: v.id, kind: 'ferry', ref: trip.id, slot: hitSlot.key, change: trip.status === 'cancelled' ? 'cancelled' : 'schedule_update' }); }
      }
    }
    if (v.kind === 'closure') {
      for (const cl of raw.closures ?? []) {
        const hitSlots = tpl.slots.filter((s) => (s.place || s.foot) === cl.place_id);
        for (const hs of hitSlots) { changedSlots.add(hs.key); changeLog.push({ version: v.id, kind: 'closure', ref: cl.id, slot: hs.key, change: 'closed' }); }
      }
    }
    if (v.kind === 'tide' || v.kind === 'place') {
      // 潮汐/地点变化影响所有步行段与该地点游览段
      for (const s of tpl.slots) if (s.foot || (v.kind === 'place' && s.place)) changedSlots.add(s.key);
      changeLog.push({ version: v.id, kind: v.kind, change: v.kind === 'tide' ? 'tide_update' : 'place_update' });
    }
  }

  // 逐槽位重算。规则：
  //  * 只有数据发生变化的槽位才进入“重算”，其余槽位默认保留（局部重算）；
  //  * 当上游时间链断裂（如去程船取消）时，依赖时间锚定的后续槽位(visit/foot)
  //    必须级联重算以暴露断链——这是“接驳连带影响”，不是数据直接变化；
  //  * 自带固定时刻的班次段不因断锚而变不可行，只接驳校验失败并给连带说明；
  //  * 用户锁定段永不被自动改写。
  const results = [];
  let prevEnd = null, prevTitle = null, chainBroken = false;
  for (let i = 0; i < tpl.slots.length; i++) {
    const slot = tpl.slots[i];
    const row = existing[i];
    const dataChanged = changedSlots.has(slot.key);
    const anchored = !slot.schedule;
    const cascade = !dataChanged && chainBroken && anchored && !row.locked;
    const mustRecompute = dataChanged || cascade;

    if (!mustRecompute) {
      // 断链期间，保留的固定时刻段（如接驳巴士）不能充当时间锚：
      // 它自身时刻有效，但人无法在前置班次取消的情况下到达它。
      if (row.end_utc && !chainBroken) { prevEnd = new Date(row.end_utc); prevTitle = slot.title; }
      let advice = row.adjust_advice;
      if (chainBroken) {
        advice = advice ?? (slot.schedule
          ? `上游接驳已断（前段待调整）；本固定班次时刻本身仍有效，但能否衔接取决于前段如何调整。`
          : `上游时间链已断；该段为保留/锁定状态，未自动改时，需人工确认接驳。`);
        await one(db, `UPDATE itinerary_segments SET adjust_advice=$2 WHERE id=$1`, [row.id, advice]);
      }
      // 注意：保留的固定时刻段不会修复上游断链（它不能把你从取消的船运到岛上）。
      // 断链只在“数据变化并重算为可行的班次段”（如已采用替代船）处清除。
      results.push({ ...mapRow(row, { recomputed: false }), adjust_advice: advice });
      continue;
    }

    if (row.locked && dataChanged) {
      const advice = buildLockAdvice(slot, prevTitle);
      await one(db, `UPDATE itinerary_segments SET current_status='locked', adjust_advice=$2 WHERE id=$1`,
        [row.id, advice]);
      if (row.end_utc) { prevEnd = new Date(row.end_utc); prevTitle = slot.title; }
      results.push({ ...mapRow(row, { recomputed: 'locked' }), adjust_advice: advice });
      for (const cv of newVersions) await recordImpactForSlot(db, itineraryId, cv.id, slot, 'new_candidate', { status: 'locked', locked: true });
      continue;
    }

    const seg = await evalSlot({ db, world, dateYmd, tpl }, slot, prevEnd, prevTitle);
    seg.idx = i;
    if (cascade) seg.adjust_advice = `因上游时间链断裂而级联重算：${seg.infeasible_reasons[0]?.message ?? '接驳时间无法确定'}。`;
    seg.alternatives = await buildAlternatives(db, world, tpl, slot, dateYmd, seg);
    const newStatus = seg.status === 'feasible' ? 'feasible' : 'pending_adjustment';
    await one(db,
      `UPDATE itinerary_segments SET start_utc=$2,end_utc=$3,current_status=$4,
         infeasible_reasons=$5::jsonb, alternatives=$6::jsonb, adjust_advice=$7 WHERE id=$1`,
      [row.id, seg.start_utc ? new Date(seg.start_utc) : null, seg.end_utc ? new Date(seg.end_utc) : null,
       newStatus, JSON.stringify(seg.infeasible_reasons), JSON.stringify(seg.alternatives), seg.adjust_advice ?? null]);
    await one(db, `DELETE FROM segment_evidence WHERE segment_id=$1 AND scope='current'`, [row.id]);
    for (const e of seg.evidence ?? []) {
      await one(db,
        `INSERT INTO segment_evidence (segment_id,scope,condition_type,ref_type,source_version_id,detail,verdict)
         VALUES ($1,'current',$2,$3,$4,$5::jsonb,$6)`,
        [row.id, e.condition_type, e.ref_type ?? null, e.source_version_id ?? null, JSON.stringify(e.detail ?? {}), e.verdict]);
    }
    const impactKind = seg.infeasible_reasons.some(r => r.code === 'cancelled') ? 'cancelled'
      : seg.infeasible_reasons.some(r => (r.code || '').startsWith('tide')) ? 'tide_window_lost'
      : seg.infeasible_reasons.some(r => r.code === 'closed') ? 'closed' : 'time_changed';
    if (dataChanged) for (const cv of newVersions) await recordImpactForSlot(db, itineraryId, cv.id, slot, impactKind, { status: newStatus, reasons: seg.infeasible_reasons, cascade: !!cascade });

    if (seg.status === 'infeasible') {
      if (slot.schedule || anchored) chainBroken = true;
      prevEnd = null;
    } else if (seg.end_utc) {
      prevEnd = new Date(seg.end_utc); prevTitle = slot.title;
      // 只有真正“换了班次/数据更新”且可行的班次段，才能重新锚定整条链
      if (slot.schedule && dataChanged) chainBroken = false;
    }
    results.push(mapRow(await one(db, `SELECT * FROM itinerary_segments WHERE id=$1`, [row.id]),
      { recomputed: cascade ? 'cascade' : true }));
  }
  annotateKnockOnRows(results);

  // 提升水位（记录为重算已消费），保留 static_snapshot 不变
  const newHw = { ...hw };
  for (const v of newVersions) {
    const map = { ferry: 'ferry', closure: 'closure', tide: 'tide', place: 'place' };
    const key = map[v.kind];
    if (key) newHw[key] = Math.max(newHw[key] ?? 0, v.id);
  }
  await one(db, `UPDATE itineraries SET static_snapshot=jsonb_set(static_snapshot,'{version_high_water}', $2::jsonb) WHERE id=$1`,
    [itineraryId, JSON.stringify(newHw)]);

  return { itinerary_id: itineraryId, new_versions: newVersions.map((v) => ({ id: v.id, kind: v.kind, source_key: v.source_key })),
    change_log: changeLog, segments: results };
}

function v0(arr) { return arr[0]?.id ?? null; }
async function recordImpactForSlot(db, itineraryId, versionId, slot, impactKind, detail) {
  if (!versionId) return;
  await one(db,
    `INSERT INTO impact_relations (caused_by_version_id,subject_type,subject_ref,impact_kind,itinerary_id,detail)
     VALUES ($1,$2,$3,$4,$5,$6::jsonb)`,
    [versionId,
     slot.schedule ? 'schedule' : slot.foot ? 'tide' : slot.place ? 'place' : 'closure',
     slot.schedule || slot.foot || slot.place || slot.key,
     impactKind, itineraryId, JSON.stringify({ slot: slot.key, ...detail })]);
}

function buildLockAdvice(slot, prevTitle) {
  return `该段已被用户锁定，数据变更不自动改写；请人工确认。其调整将连带影响后续接驳时间` +
    (prevTitle ? `（上一段：${prevTitle}）` : '') + '。';
}

// 替代候选
async function buildAlternatives(db, world, tpl, slot, dateYmd, seg) {
  const alts = [];
  if (slot.schedule) {
    // 找同 OD、同模式的其他班次
    const rows = await query(db,
      `SELECT s.*, sv.source_key FROM schedules s
       JOIN source_versions sv ON sv.id=s.source_version_id
       WHERE s.mode IN ('ferry','bus') AND s.dep_date <= $1::date AND s.status='scheduled' AND s.id <> $2
       ORDER BY s.dep_date, s.dep_local`, [dateYmd, slot.schedule]);
    const cur = await one(db, `SELECT origin_id,destination_id FROM schedules WHERE id=$1 ORDER BY source_version_id DESC LIMIT 1`, [slot.schedule]);
    for (const r of rows.filter((x) => !cur || (x.origin_id === cur.origin_id && x.destination_id === cur.destination_id)).slice(0, 4)) {
      alts.push({ kind: 'schedule', schedule_id: r.id, route: r.route_name,
        dep: `${dateStr(r.dep_date)} ${timeStr(r.dep_local)}`, arr: timeStr(r.arr_local), source_key: r.source_key,
        note: '换用该班次会顺延/提前下游接驳，需逐段复核。' });
    }
  }
  if (slot.foot) {
    const place = await world.place(slot.foot);
    const station = place?.tide_station_id ? await world.stationLatest(place.tide_station_id) : null;
    const th = place ? await world.threshold(place.id) : null;
    if (station && th) {
      const points = await world.tidePoints(station.id, station.source_version_id);
      const wins = findTideWindows(points, th.max_height_m, slot.needMin ?? 20);
      for (const w of wins.slice(0, 4)) {
        alts.push({ kind: 'tide_window', station_id: station.id,
          window_utc: [isoUtc(w.start), isoUtc(w.end)],
          window_local: [utcToLocal(w.start, tpl.offsetMin), utcToLocal(w.end, tpl.offsetMin)],
          note: '改用该低潮窗口需重排接驳巴士与回程船班。' });
      }
    }
  }
  return alts;
}

function annotateKnockOnRows(rows) {
  const firstBroken = rows.findIndex((r) => ['infeasible', 'pending_adjustment', 'locked'].includes(r.current_status));
  if (firstBroken >= 0) {
    for (let i = firstBroken + 1; i < rows.length; i++) {
      if (rows[i].current_status === 'feasible' && !rows[i].adjust_advice) {
        rows[i].adjust_advice = `上游段「${rows[firstBroken].title}」待调整/锁定，接驳时间可能变化，登船截止时间需重新核对。`;
      }
    }
  }
}

function mapRow(r, meta = {}) {
  return {
    idx: r.idx, slot_key: r.slot_key, type: r.type, title: r.title, place_id: r.place_id,
    schedule_id: r.schedule_id,
    start_utc: r.start_utc ? new Date(r.start_utc).toISOString() : null,
    end_utc: r.end_utc ? new Date(r.end_utc).toISOString() : null,
    static_status: r.static_status, current_status: r.current_status, locked: r.locked,
    infeasible_reasons: typeof r.infeasible_reasons === 'string' ? JSON.parse(r.infeasible_reasons) : r.infeasible_reasons,
    alternatives: typeof r.alternatives === 'string' ? JSON.parse(r.alternatives) : r.alternatives,
    adjust_advice: r.adjust_advice, ...meta,
  };
}

// ----------------------- 用户锁定 / 应用替代 -----------------------
export async function lockSegment(db, itineraryId, slotKey, reason) {
  const row = await one(db, `SELECT * FROM itinerary_segments WHERE itinerary_id=$1 AND slot_key=$2`,
    [itineraryId, slotKey]);
  if (!row) throw new Error('段不存在');
  await one(db, `UPDATE itinerary_segments SET locked=TRUE, locked_reason=$2, current_status='locked' WHERE id=$1`,
    [row.id, reason ?? '用户锁定']);
  await one(db,
    `INSERT INTO segment_evidence (segment_id,scope,condition_type,detail,verdict)
     VALUES ($1,'current','lock',$2::jsonb,'warn')`,
    [row.id, JSON.stringify({ reason: reason ?? '用户锁定', at: new Date().toISOString() })]);
  return mapRow(await one(db, `SELECT * FROM itinerary_segments WHERE id=$1`, [row.id]));
}

// 应用一个替代候选到段（写回并解锁/记录），返回更新后的段（接驳仍需逐段确认）
export async function applyAlternative(db, itineraryId, slotKey, candidate) {
  const row = await one(db, `SELECT * FROM itinerary_segments WHERE itinerary_id=$1 AND slot_key=$2`,
    [itineraryId, slotKey]);
  if (!row) throw new Error('段不存在');
  if (candidate.kind === 'schedule') {
    const s = await one(db, `SELECT * FROM schedules WHERE id=$1 ORDER BY source_version_id DESC LIMIT 1`, [candidate.schedule_id]);
    const itRow = await one(db, `SELECT static_snapshot FROM itineraries WHERE id=$1`, [itineraryId]);
    const snap = typeof itRow.static_snapshot === 'string' ? JSON.parse(itRow.static_snapshot) : itRow.static_snapshot;
    const tpl = TEMPLATES[snap.template_key];
    const off = tpl?.offsetMin ?? 480;
    const depDate = s.dep_date instanceof Date ? s.dep_date.toISOString().slice(0,10) : String(s.dep_date).slice(0,10);
    const depLocal = String(s.dep_local).slice(0,5), arrLocal = String(s.arr_local).slice(0,5);
    const depUTC = localToUtc(depDate, depLocal, off);
    const arr = addMinutes(depDate, depLocal,
      (() => { const [a, b] = arrLocal.split(':').map(Number); const [c, d] = depLocal.split(':').map(Number); let g=a*60+b-(c*60+d); return g<0?g+1440:g; })());
    const arrUTC = localToUtc(arr.dateYmd, arr.hhmm, off);
    await one(db,
      `UPDATE itinerary_segments SET schedule_id=$2,start_utc=$3,end_utc=$4,current_status='feasible',
         locked=FALSE,infeasible_reasons='[]'::jsonb,adjust_advice='替代班次已应用，下游接驳需逐段复核。'
       WHERE id=$1`, [row.id, s.id, depUTC, arrUTC]);
  }
  if (candidate.kind === 'tide_window') {
    const [s, e] = candidate.window_utc;
    await one(db,
      `UPDATE itinerary_segments SET start_utc=$2,end_utc=$3,current_status='pending_adjustment',
         adjust_advice='已改用备选低潮窗口；接驳巴士/船班尚未重排，需确认。'
       WHERE id=$1`, [row.id, new Date(s), new Date(e)]);
  }
  return mapRow(await one(db, `SELECT * FROM itinerary_segments WHERE id=$1`, [row.id]));
}
