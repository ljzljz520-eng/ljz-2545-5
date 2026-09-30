// 测试公共工具：内存 PG + 建表 + 摄入基线包
import { readFile } from 'node:fs/promises';
import { newMemoryDb } from '../src/server/db.js';
import { ingestBundle } from '../src/server/domain/ingest.js';
import { loadBundle } from '../src/server/adapters/directory-source.js';
import { staticPlan, recompute, lockSegment } from '../src/server/domain/planner.js';

export async function freshWorld() {
  const db = await newMemoryDb();
  await db.exec(await readFile(new URL('../db/schema.sql', import.meta.url), 'utf8'));
  const bundle = await loadBundle(new URL('../src/data-source/', import.meta.url).pathname);
  const report = await ingestBundle(db, bundle, {});
  return { db, report };
}

// 直接摄入一个“新来源”的局部 bundle
export async function pushBundle(db, source_key, bundle) {
  return ingestBundle(db, bundle, { source_key });
}
export const cancelFerry = (id, over = {}) => ({
  tz: 'Asia/Shanghai', utc_offset_min: 480,
  ferry: { kind: 'ferry', effective_from: '2026-10-01', tz: 'Asia/Shanghai',
    trips: [{ id, mode: 'ferry', route_name: '去程', origin_id: 'pier-mainland', destination_id: 'pier-island',
      dep_date: '2026-10-01', dep_local: '06:30', arr_local: '07:15', status: 'cancelled', ...over }] },
});
export async function baselinePlan(db, templateKey = 'lighthouse-day', date = '2026-10-01') {
  return staticPlan(db, { templateKey, date });
}
export const segByKey = (plan, key) => plan.segments.find((s) => s.slot_key === key);
export { recompute, lockSegment };
