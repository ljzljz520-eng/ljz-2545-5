import { test } from 'node:test';
import assert from 'node:assert/strict';
import { freshWorld, pushBundle, cancelFerry, baselinePlan, recompute } from './helpers.js';
import { rollbackItinerary } from '../src/server/domain/rollback.js';
import { buildOfflineBundle } from '../src/server/domain/offline.js';
import { query, one } from '../src/server/db.js';

// ---------- 场景④：旧缓存回滚 ----------
test('场景④ 旧缓存回滚：重算后的待调整状态可恢复为一次生成的静态快照', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  await recompute(db, plan.id);
  const after = await one(db, `SELECT current_status FROM itinerary_segments WHERE itinerary_id=$1 AND slot_key='s1'`, [plan.id]);
  assert.equal(after.current_status, 'pending_adjustment');

  const rb = await rollbackItinerary(db, plan.id, '恢复到出海前缓存');
  assert.equal(rb.rolled_back_from, plan.id);
  const restored = await one(db, `SELECT current_status, static_status FROM itinerary_segments WHERE itinerary_id=$1 AND slot_key='s1'`, [rb.new_id]);
  assert.equal(restored.current_status, 'feasible');
  assert.equal(restored.static_status, 'feasible');
  // 回滚行程保留静态证据且 rollback_to_id 指回
  const it = await one(db, `SELECT rollback_to_id FROM itineraries WHERE id=$1`, [rb.new_id]);
  assert.equal(it.rollback_to_id, plan.id);
  const ev = await query(db, `SELECT count(*)::int n FROM segment_evidence WHERE segment_id IN
    (SELECT id FROM itinerary_segments WHERE itinerary_id=$1) AND scope='static'`, [rb.new_id]);
  assert.ok(ev[0].n > 0);
});

// ---------- 离线包：冻结日期 + 更新缺口 + 免责 ----------
test('离线包：显示冻结日期、更新缺口，并声明不保证现实交通可用', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  // 冻结日期(09-28)之后再摄入一个新版本 => 应出现在更新缺口
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  const b = await buildOfflineBundle(db, { frozenDate: '2026-09-28', itineraryId: plan.id });
  assert.equal(b.frozen_date, '2026-09-28');
  assert.match(b.disclaimer, /不保证现实交通可用/);
  assert.ok(b.update_gap.count >= 1);
  assert.ok(b.update_gap.newer_versions.some((v) => v.kind === 'ferry'));
  assert.ok(b.itinerary); // 含冻结行程快照
  // 无缺口场景
  const b2 = await buildOfflineBundle(db, { frozenDate: '2026-12-31' });
  assert.equal(b2.update_gap.count, 0);
});

// ---------- PG 保存来源版本与影响关系 ----------
test('来源版本不可变 + 影响关系可回溯（版本 -> 行程/段）', async () => {
  const { db } = await freshWorld();
  const plan = await baselinePlan(db);
  await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  await recompute(db, plan.id);
  const v = await one(db, `SELECT * FROM source_versions WHERE source_key='ferry-operator-live' AND kind='ferry'`);
  const raw = typeof v.raw_json === 'string' ? JSON.parse(v.raw_json) : v.raw_json;
  assert.equal(raw.trips[0].status, 'cancelled');
  const imp = await query(db, `SELECT * FROM impact_relations WHERE caused_by_version_id=$1 AND itinerary_id=$2`, [v.id, plan.id]);
  assert.ok(imp.some((r) => r.impact_kind === 'cancelled'));
  // 版本不可变：没有 update 接口；指纹存在
  assert.ok(v.content_hash.length === 64);
});

// ---------- 摄入幂等：同内容重复摄入不新增版本 ----------
test('摄入幂等：相同内容重复摄入返回同一版本', async () => {
  const { db, report } = await freshWorld();
  const v1 = report.versions.ferry.id;
  const again = await ingestAgain(db);
  assert.equal(again, v1);
});
async function ingestAgain(db) {
  const { loadBundle } = await import('../src/server/adapters/directory-source.js');
  const { ingestBundle } = await import('../src/server/domain/ingest.js');
  const bundle = await loadBundle(new URL('../src/data-source/', import.meta.url).pathname);
  return (await ingestBundle(db, bundle, {})).versions.ferry.id;
}

// ---------- 跨源矛盾检测：时间格式差异不得误报，真实差异必须报告 ----------
test('跨源矛盾：HH:MM 与 HH:MM:SS 纯格式差异不误报；状态/时刻真差异才报', async () => {
  const { db } = await freshWorld();
  // 与基线完全一致的 F101（仅来源不同、时刻写法 HH:MM）=> 不应产生任何矛盾
  const same = await pushBundle(db, 'ferry-operator-mirror', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    ferry: { kind: 'ferry', effective_from: '2026-10-01', tz: 'Asia/Shanghai', trips: [
      { id: 'F101', mode: 'ferry', route_name: '去程', origin_id: 'pier-mainland', destination_id: 'pier-island',
        dep_date: '2026-10-01', dep_local: '06:30', arr_local: '07:15', status: 'scheduled' }] },
  });
  assert.deepEqual(same.conflicts, []);
  // 状态不同 => 只报 status，不夹带 dep/arr 伪矛盾
  const diff = await pushBundle(db, 'ferry-operator-live', cancelFerry('F101'));
  const c = diff.conflicts.find((x) => x.schedule_id === 'F101');
  assert.ok(c);
  assert.deepEqual(c.conflicts, ['status: scheduled vs cancelled']);
  // 时刻真不同 => 报 dep
  const moved = await pushBundle(db, 'ferry-operator-alt', {
    tz: 'Asia/Shanghai', utc_offset_min: 480,
    ferry: { kind: 'ferry', effective_from: '2026-10-01', tz: 'Asia/Shanghai', trips: [
      { id: 'F101', mode: 'ferry', route_name: '去程', origin_id: 'pier-mainland', destination_id: 'pier-island',
        dep_date: '2026-10-01', dep_local: '09:05', arr_local: '09:50', status: 'scheduled' }] },
  });
  const c2 = moved.conflicts.find((x) => x.schedule_id === 'F101');
  assert.ok(c2.conflicts.some((x) => x.startsWith('dep:')));
});
