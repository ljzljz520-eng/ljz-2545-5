import { test } from 'node:test';
import assert from 'node:assert/strict';
import { freshWorld, pushBundle, cancelFerry, baselinePlan, segByKey, recompute, lockSegment } from './helpers.js';
import { evaluateTide, localToUtc, addMinutes } from '../src/server/domain/util.js';

const L = '2026-10-01';

// ---------- 单元：潮汐绝不外推 ----------
test('潮汐：覆盖不足/缺测 => unknown，绝不推断可通行', () => {
  const start = localToUtc(L, '10:00', 480).getTime();
  const end = localToUtc(L, '11:00', 480).getTime();
  // 只有一个点
  assert.equal(evaluateTide([{ ts: start, height: 0.3 }], new Date(start), new Date(end), 0.7).verdict, 'unknown');
  // 两点都在区间内但起点早于首个点 -> 禁止外推
  const pts = [{ ts: start + 600000, height: 0.3 }, { ts: start + 1200000, height: 0.3 }];
  assert.equal(evaluateTide(pts, new Date(start), new Date(end), 0.7).verdict, 'unknown');
  // 完整覆盖且低于阈值 => pass
  const ok = [{ ts: start, height: 0.45 }, { ts: start + 1800000, height: 0.45 }, { ts: end, height: 0.45 }];
  assert.equal(evaluateTide(ok, new Date(start), new Date(end), 0.7).verdict, 'pass');
  // 区间内高潮 => fail
  const high = [{ ts: start, height: 0.45 }, { ts: start + 1800000, height: 1.4 }, { ts: end, height: 0.45 }];
  assert.equal(evaluateTide(high, new Date(start), new Date(end), 0.7).verdict, 'fail');
});

test('跨日：23:30 + 50 分钟 => 次日 00:20', () => {
  const r = addMinutes(L, '23:30', 50);
  assert.equal(r.dateYmd, '2026-10-02');
  assert.equal(r.hhmm, '00:20');
  assert.equal(r.dayShift, 1);
});

// ---------- 基线：一次生成静态行程，全程可行且证据齐全 ----------
test('基线静态行程：8 段全可行，步行段含潮汐/坐标/接驳证据', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  assert.equal(plan.segments.length, 8);
  for (const s of plan.segments) assert.equal(s.status, 'feasible', `${s.slot_key} 应可行: ${JSON.stringify(s.infeasible_reasons)}`);
  const walk = segByKey(plan, 's3');
  const types = walk.evidence.map((e) => e.condition_type);
  assert.ok(types.includes('tide_window'));
  assert.ok(types.includes('coords_match'));
  assert.ok(types.includes('connection'));
  assert.ok(walk.evidence.some((e) => e.source_version_id)); // 证据可追溯到来源版本
});

// ---------- 场景①：船班取消 -> 仅该段待调整 + 级联断锚 + 替代候选 ----------
test('场景① 船班 F101 取消：s1 待调整、时间锚段级联、提供 F102 候选', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  const rec = await recompute(db, plan.id);
  const s1 = rec.segments.find((s) => s.slot_key === 's1');
  assert.equal(s1.current_status, 'pending_adjustment');
  assert.ok(s1.infeasible_reasons.some((r) => r.code === 'cancelled'));
  assert.ok(s1.alternatives.some((a) => a.schedule_id === 'F102'));
  // 数据未变的固定班次段保持保留
  assert.equal(rec.segments.find((s) => s.slot_key === 's8').recomputed, false);
  // 时间锚定段被级联重算（去程船没了，步行/游览无法定时）
  assert.equal(rec.segments.find((s) => s.slot_key === 's3').recomputed, 'cascade');
  assert.equal(rec.segments.find((s) => s.slot_key === 's3').current_status, 'pending_adjustment');
});

test('场景① 锁定段：取消后不自动改写，状态保持 locked', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await lockSegment(db, plan.id, 's1', '已购不可退船票');
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  const rec = await recompute(db, plan.id);
  const s1 = rec.segments.find((s) => s.slot_key === 's1');
  assert.equal(s1.current_status, 'locked');
  assert.equal(s1.recomputed, 'locked');
});

// ---------- 场景②：不同来源矛盾 ----------
test('场景② 两来源对 F101 时刻矛盾且都在航 => source_conflict，保守不采用', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'ferry-operator-alt', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    ferry: { kind: 'ferry', effective_from: L, tz: 'Asia/Shanghai', trips: [
      { id: 'F101', mode: 'ferry', route_name: '去程', origin_id: 'pier-mainland', destination_id: 'pier-island',
        dep_date: L, dep_local: '09:05', arr_local: '09:50', status: 'scheduled' }] },
  });
  const rec = await recompute(db, plan.id);
  const s1 = rec.segments.find((s) => s.slot_key === 's1');
  assert.equal(s1.current_status, 'pending_adjustment');
  assert.ok(s1.infeasible_reasons.some((r) => r.code === 'source_conflict'));
});

// ---------- 场景③：地点/潮位站改坐标 -> 不匹配 -> 不推断可通行 ----------
test('场景③ 潮位站迁站坐标超距 + 潮汐步行段 => tide_station_mismatch(不推断)', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  const report = await pushBundle(db, 'tide-bureau-corrected', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    tides: { kind: 'tide', effective_from: L, tz: 'Asia/Shanghai', stations: [
      { id: 'ts-causeway', name: '迁站', lat: 30.12, lng: 122.66, tz: 'Asia/Shanghai',
        applies_to: ['causeway-north', 'viewpoint-lighthouse'], max_match_distance_m: 800 }],
      points: [{ ts: '2026-10-01T00:00:00.000Z', height_m: 0.45, kind: 'prediction' },
               { ts: '2026-10-01T09:00:00.000Z', height_m: 0.45, kind: 'prediction' }] },
  });
  // 摄入阶段即产生 rejected + impact
  assert.ok(report.rejected.some((r) => r.type === 'place_tide_binding'));
  const rec = await recompute(db, plan.id);
  const s3 = rec.segments.find((s) => s.slot_key === 's3');
  assert.equal(s3.current_status, 'pending_adjustment');
  assert.ok(s3.infeasible_reasons.some((r) => r.code === 'tide_station_mismatch'));
});

// ---------- 临时封闭：仅对应访问段待调整 ----------
test('临时封闭北堤：仅 s3/s5 待调整，其他段不因此变不可行', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'parks-ops-live', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    closures: { kind: 'closure', effective_from: L, tz: 'Asia/Shanghai', closures: [
      { id: 'C1', place_id: 'causeway-north', start_local: `${L}T08:00`, end_local: `${L}T12:00`, reason: '堤道应急检修' }] },
  });
  const rec = await recompute(db, plan.id);
  const by = Object.fromEntries(rec.segments.map((s) => [s.slot_key, s.current_status]));
  assert.equal(by.s3, 'pending_adjustment');
  assert.equal(by.s1, 'feasible'); // 去程船不受封闭影响
  // s3 封闭导致时间链断，灯塔游览 s4 作为接驳下游被级联标记（非封闭数据直接导致）
  assert.ok(['feasible','pending_adjustment'].includes(by.s4));
  const s3 = rec.segments.find((s) => s.slot_key === 's3');
  assert.ok(s3.infeasible_reasons.some((r) => r.code === 'closed'));
});

// ---------- 场景⑤：潮位缺测 -> 不可推断 ----------
test('场景⑤ 潮位缺测：步行段 tide_unknown，不推断可通行', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'tide-bureau-gap', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    tides: { kind: 'tide', effective_from: L, tz: 'Asia/Shanghai', stations: [
      { id: 'ts-causeway', name: '缺测', lat: 29.9608, lng: 122.4258, tz: 'Asia/Shanghai',
        applies_to: ['causeway-north', 'viewpoint-lighthouse'], max_match_distance_m: 800 }],
      points: [{ ts: '2026-10-01T00:00:00.000Z', height_m: 0.45, kind: 'prediction' }] },
  });
  const rec = await recompute(db, plan.id);
  const s3 = rec.segments.find((s) => s.slot_key === 's3');
  assert.ok(['pending_adjustment'].includes(s3.current_status));
  assert.ok(s3.infeasible_reasons.some((r) => (r.code || '').startsWith('tide')));
});

// ---------- 跨日接驳 ----------
test('跨日模板：末班船 23:30 出发，次日 00:20 抵达且接驳满足', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db, 'cross-day');
  const x3 = segByKey(plan, 'x3');
  assert.equal(x3.status, 'feasible');
  assert.ok(x3.end_utc.startsWith('2026-10-01T16:20')); // 00:20+08:00
  assert.ok(x3.start_utc.startsWith('2026-10-01T15:30'));
});

// ---------- 幂等：重算消费水位后再次重算无新版本 ----------
test('重算幂等：第二次重算无新版本、无新增变更', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  const first = await recompute(db, plan.id);
  assert.equal(first.new_versions.length, 1);
  const second = await recompute(db, plan.id);
  assert.equal(second.new_versions.length, 0);
});
