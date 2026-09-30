// 数据库访问层。
// 开发/测试环境使用 PGlite（WASM 中的真实 PostgreSQL）；
// 仅驱动不同，SQL 与标准 PostgreSQL 完全一致，可平移到独立 PG 实例。
import { PGlite } from '@electric-sql/pglite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const DB_PATH = process.env.PGLITE_PATH || './.pglite-data';
let _db;

export async function getDb() {
  if (_db) return _db;
  mkdirSync(dirname(DB_PATH), { recursive: true });
  _db = await PGlite.create(DB_PATH);
  return _db;
}

// 测试用内存库
export async function newMemoryDb() {
  return await PGlite.create();
}

export async function query(db, text, params = []) {
  const res = await db.query(text, params);
  return res.rows;
}
export async function one(db, text, params = []) {
  const rows = await query(db, text, params);
  return rows[0] ?? null;
}

// 重置（仅脚本/测试）
export async function resetDb(db) {
  await db.exec(`
    DROP TABLE IF EXISTS segment_evidence, itinerary_segments, impact_relations,
      itineraries, offline_bundles, closures, schedules, tide_thresholds,
      tide_points, tide_stations, open_materials, places, source_versions CASCADE;
  `);
}
