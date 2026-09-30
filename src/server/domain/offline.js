// 离线包：把某一冻结日期的数据与行程快照打包。
// 明确展示：冻结日期(frozen_date)、数据截止水位、更新缺口(update gap)，
// 并附“不保证现实交通可用”声明。
import { one, query } from '../db.js';

export async function buildOfflineBundle(db, { frozenDate = '2026-09-30', itineraryId = null }) {
  const versions = await query(db,
    `SELECT id,source_key,kind,fetched_at,effective_from,content_hash,supersedes_id
     FROM source_versions ORDER BY id`);
  const latestByKind = {};
  for (const v of versions) latestByKind[v.kind] = v;
  // 更新缺口：生效日期晚于冻结日期的数据版本（离线包未包含，回到线上应追补）；
  // 同时纳入冻结后才被摄入(fetched_at)的应急更新（如临时封闭）。
  const gap = await query(db,
    `SELECT id,source_key,kind,fetched_at,effective_from FROM source_versions
     WHERE effective_from::date > $1::date OR fetched_at::date > $1::date
     ORDER BY id`, [frozenDate]);

  const payload = {
    frozen_date: frozenDate,
    built_at: new Date().toISOString(),
    disclaimer: '本离线包为冻结示例数据，仅用于离线浏览；不保证现实交通可用（船班/潮汐/封闭可能已变化），出行前请以官方实时信息为准。',
    latest_versions: Object.fromEntries(
      Object.entries(latestByKind).map(([k, v]) => [k, { id: v.id, source_key: v.source_key, fetched_at: new Date(v.fetched_at).toISOString() }])),
    update_gap: {
      count: gap.length,
      newer_versions: gap.map((g) => ({ id: g.id, kind: g.kind, source_key: g.source_key, fetched_at: new Date(g.fetched_at).toISOString() })),
      note: gap.length ? `冻结日期之后有 ${gap.length} 个数据版本未包含在本包内（可能影响行程，回线需追补）` : '冻结日期之后暂无更新',
    },
  };
  if (itineraryId) {
    const it = await one(db, `SELECT * FROM itineraries WHERE id=$1`, [itineraryId]);
    if (it) payload.itinerary = typeof it.static_snapshot === 'string' ? JSON.parse(it.static_snapshot) : it.static_snapshot;
  }
  const row = await one(db,
    `INSERT INTO offline_bundles (frozen_date,payload,source_version_ids)
     VALUES ($1,$2::jsonb,$3) RETURNING id`,
    [frozenDate, JSON.stringify(payload), versions.map((v) => v.id)]);
  return { id: row.id, ...payload };
}
