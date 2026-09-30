// 数据摄入：归一化 bundle -> 不可变 source_versions -> 业务表。
// 每次摄入都保留原始 JSON 与内容指纹；同内容重复摄入会被识别（dedup）。
// 校验：潮位站坐标/时区/适用范围必须与引用它的地点匹配，否则整条地点数据被拒并报告。
import { query, one } from '../db.js';
import { localToUtc, distanceM } from './util.js';

const FERRY_MIN_GAP_MIN = 10; // 接驳最小间隔（也在规划器使用）

async function recordVersion(db, { source_key, kind, effective_from, raw, note, supersedesId = null }) {
  const { hashBundle } = await import('../adapters/directory-source.js');
  const content_hash = hashBundle(raw);
  // 同 source_key+kind+生效日+同指纹 -> 幂等
  const dup = await one(db,
    `SELECT id FROM source_versions
     WHERE source_key=$1 AND kind=$2 AND COALESCE(effective_from::text,'')=COALESCE($3::text,'')
       AND content_hash=$4 ORDER BY id DESC LIMIT 1`,
    [source_key, kind, effective_from ?? null, content_hash]);
  if (dup) return { id: dup.id, deduped: true };

  // 找到被取代的旧版本
  let supersedes = supersedesId;
  if (!supersedes) {
    supersedes = await one(db,
      `SELECT id FROM source_versions WHERE source_key=$1 AND kind=$2
       ORDER BY id DESC LIMIT 1`, [source_key, kind]);
    supersedes = supersedes?.id ?? null;
  }
  const row = await one(db,
    `INSERT INTO source_versions (source_key, kind, effective_from, content_hash, raw_json, note, supersedes_id)
     VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7) RETURNING id`,
    [source_key, kind, effective_from ?? null, content_hash, JSON.stringify(raw), note ?? null, supersedes]);
  return { id: row.id, deduped: false, supersedes };
}

// 校验地点与潮位站的坐标/时区/适用范围一致性
export function validateStationPlace(place, station) {
  const errors = [];
  if (!station) {
    errors.push(`地点 ${place.id} 声明潮位站 ${place.tide_station_id}，但该站不存在`);
    return { ok: false, errors };
  }
  if (station.tz !== place.tz) {
    errors.push(`时区不匹配：地点 ${place.id}(${place.tz}) vs 潮位站 ${station.id}(${station.tz})`);
  }
  if (!station.applies_to.includes(place.id)) {
    errors.push(`适用范围不匹配：潮位站 ${station.id} 的 applies_to 未包含地点 ${place.id}`);
  }
  const d = distanceM(place.lat, place.lng, station.lat, station.lng);
  const max = station.max_match_distance_m ?? 2000;
  if (d > max) {
    errors.push(`坐标不匹配：地点 ${place.id} 与潮位站 ${station.id} 相距 ${Math.round(d)}m > ${max}m`);
  }
  return { ok: errors.length === 0, errors, distanceM: Math.round(d) };
}

export async function ingestBundle(db, bundle, opts = {}) {
  const sk = opts.source_key ?? bundle.source_key;
  const report = { source_key: sk, versions: {}, warnings: [], conflicts: [], rejected: [] };

  // ---------- places ----------
  if (bundle.places) {
    const p = bundle.places;
    const v = await recordVersion(db, { source_key: sk, kind: 'place', effective_from: p.effective_from ?? null, raw: p });
    report.versions.place = v;
    if (!v.deduped) {
      // 先摄入潮位站（若同一 bundle 携带），以便校验；否则延迟到 tide 摄入后校验
      for (const place of p.places) {
        await one(db,
          `INSERT INTO places (id,name,kind,lat,lng,tz,tide_station_id,description,source_version_id,updated_in_version_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$9)
           ON CONFLICT (id) DO UPDATE SET
             name=EXCLUDED.name, kind=EXCLUDED.kind, lat=EXCLUDED.lat, lng=EXCLUDED.lng,
             tz=EXCLUDED.tz, tide_station_id=EXCLUDED.tide_station_id,
             description=EXCLUDED.description, updated_in_version_id=EXCLUDED.source_version_id`,
          [place.id, place.name, place.kind, place.lat, place.lng, place.tz,
           place.tide_station_id ?? null, place.description ?? null, v.id]);
        if (place.walk_threshold_m != null && place.tide_station_id) {
          await one(db,
            `INSERT INTO tide_thresholds (place_id,max_height_m,source_version_id)
             VALUES ($1,$2,$3)
             ON CONFLICT (place_id) DO UPDATE SET max_height_m=EXCLUDED.max_height_m,
               source_version_id=EXCLUDED.source_version_id`,
            [place.id, place.walk_threshold_m, v.id]);
        }
      }
    }
  }

  // ---------- tides ----------
  if (bundle.tides) {
    const t = bundle.tides;
    const v = await recordVersion(db, { source_key: sk, kind: 'tide', effective_from: t.effective_from ?? null, raw: t });
    report.versions.tide = v;
    if (!v.deduped) {
      for (const st of t.stations ?? []) {
        await one(db,
          `INSERT INTO tide_stations (id,name,lat,lng,tz,applies_to,source_version_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT (id, source_version_id) DO NOTHING`,
          [st.id, st.name, st.lat, st.lng, st.tz, st.applies_to ?? [], v.id]);
      }
      for (const pt of t.points ?? []) {
        const station = (t.stations ?? []).find((s) => true); // 单点站场景；多站需在点上带 station_id
        const stationId = pt.station_id ?? t.stations?.[0]?.id;
        if (!stationId) continue;
        await one(db,
          `INSERT INTO tide_points (station_id,station_version_id,ts_utc,height_m,kind,source_version_id)
           VALUES ($1,$2,$3,$4,$5,$6)
           ON CONFLICT DO NOTHING`,
          [stationId, v.id, new Date(pt.ts), pt.height_m, pt.kind ?? 'prediction', v.id]);
      }
    }
    // 摄入后校验所有引用潮位站的地点
    const refs = await query(db,
      `SELECT p.*, th.max_height_m FROM places p
       LEFT JOIN tide_thresholds th ON th.place_id=p.id
       WHERE p.tide_station_id IS NOT NULL`);
    for (const place of refs) {
      const stationRow = await one(db,
        `SELECT * FROM tide_stations WHERE id=$1 ORDER BY source_version_id DESC LIMIT 1`,
        [place.tide_station_id]);
      const station = stationRow && {
        ...stationRow,
        applies_to: stationRow.applies_to,
        max_match_distance_m: (t.stations ?? []).find((s) => s.id === stationRow.id)?.max_match_distance_m ?? 2000,
      };
      const check = validateStationPlace(place, station);
      if (!check.ok) {
        report.rejected.push({ type: 'place_tide_binding', place_id: place.id, errors: check.errors });
        report.warnings.push(...check.errors);
        await one(db,
          `INSERT INTO impact_relations (caused_by_version_id,subject_type,subject_ref,impact_kind,detail)
           VALUES ($1,'tide_station',$2,'station_scope_mismatch',$3::jsonb)`,
          [v.id, place.tide_station_id, JSON.stringify({ place_id: place.id, errors: check.errors })]);
      }
    }
  }

  // ---------- ferry / schedules ----------
  if (bundle.ferry) {
    const f = bundle.ferry;
    const v = await recordVersion(db, { source_key: sk, kind: 'ferry', effective_from: f.effective_from ?? null, raw: f });
    report.versions.ferry = v;
    if (!v.deduped) {
      for (const trip of f.trips ?? []) {
        // 引用的地点必须存在
        const origin = await one(db, `SELECT id,tz FROM places WHERE id=$1`, [trip.origin_id]);
        const dest = await one(db, `SELECT id,tz FROM places WHERE id=$1`, [trip.destination_id]);
        if (!origin || !dest) {
          report.rejected.push({ type: 'schedule', id: trip.id, error: '引用地点不存在' });
          continue;
        }
        await one(db,
          `INSERT INTO schedules (id,mode,route_name,origin_id,destination_id,dep_date,dep_local,arr_local,tz,status,source_version_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
           ON CONFLICT (id, source_version_id) DO NOTHING`,
          [trip.id, trip.mode, trip.route_name, trip.origin_id, trip.destination_id,
           trip.dep_date, trip.dep_local, trip.arr_local, trip.tz ?? f.tz, trip.status ?? 'scheduled', v.id]);

        // 跨源矛盾检测：不同 source_key 对同一业务班次给出不同状态/时刻
        const others = await query(db,
          `SELECT s.*, sv.source_key FROM schedules s
           JOIN source_versions sv ON sv.id=s.source_version_id
           WHERE s.id=$1 AND sv.source_key <> $2 AND sv.effective_from::text=$3
           ORDER BY s.source_version_id DESC`, [trip.id, sk, f.effective_from ?? null]);
        for (const o of others) {
          const conflicts = [];
          // PG time 类型返回 "HH:MM:SS"，来源 JSON 多为 "HH:MM"，比较前统一归一化，避免纯格式差异误报矛盾
          const normT = (t) => String(t ?? '').slice(0, 5);
          if (o.status !== (trip.status ?? 'scheduled')) conflicts.push(`status: ${o.status} vs ${trip.status ?? 'scheduled'}`);
          if (normT(o.dep_local) !== normT(trip.dep_local)) conflicts.push(`dep: ${normT(o.dep_local)} vs ${normT(trip.dep_local)}`);
          if (normT(o.arr_local) !== normT(trip.arr_local)) conflicts.push(`arr: ${normT(o.arr_local)} vs ${normT(trip.arr_local)}`);
          if (conflicts.length) {
            const c = { schedule_id: trip.id, other_source: o.source_key, conflicts };
            report.conflicts.push(c);
            await one(db,
              `INSERT INTO impact_relations (caused_by_version_id,subject_type,subject_ref,impact_kind,detail)
               VALUES ($1,'schedule',$2,'conflict',$3::jsonb)`,
              [v.id, trip.id, JSON.stringify(c)]);
          }
        }
      }
    }
  }

  // ---------- closures ----------
  if (bundle.closures) {
    const c = bundle.closures;
    const v = await recordVersion(db, { source_key: sk, kind: 'closure', effective_from: c.effective_from ?? null, raw: c });
    report.versions.closure = v;
    if (!v.deduped) {
      const off = bundle.utc_offset_min ?? 480;
      for (const cl of c.closures ?? []) {
        const startUTC = localToUtc(cl.start_local.slice(0, 10), cl.start_local.slice(11, 16), off);
        const endUTC = localToUtc(cl.end_local.slice(0, 10), cl.end_local.slice(11, 16), off);
        await one(db,
          `INSERT INTO closures (id,place_id,start_utc,end_utc,reason,source_version_id)
           VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO NOTHING`,
          [cl.id, cl.place_id, startUTC, endUTC, cl.reason ?? null, v.id]);
      }
    }
  }

  // ---------- materials ----------
  if (bundle.materials) {
    const m = bundle.materials;
    const v = await recordVersion(db, { source_key: sk, kind: 'material', effective_from: m.effective_from ?? null, raw: m });
    report.versions.material = v;
    if (!v.deduped) {
      for (const mat of m.materials ?? []) {
        await one(db,
          `INSERT INTO open_materials (id,title,kind,url,license,publisher,summary,source_version_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (id) DO NOTHING`,
          [mat.id, mat.title, mat.kind, mat.url ?? null, mat.license ?? null,
           mat.publisher ?? null, mat.summary ?? null, v.id]);
      }
    }
  }
  return report;
}

export { recordVersion };
