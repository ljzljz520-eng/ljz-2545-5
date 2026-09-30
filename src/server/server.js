// 海岛灯塔导览站 HTTP 服务（零框架，Node 内置 http）
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getDb, query, one } from './db.js';
import { staticPlan, recompute, lockSegment, applyAlternative, listTemplates } from './domain/planner.js';
import { rollbackItinerary } from './domain/rollback.js';
import { buildOfflineBundle } from './domain/offline.js';
import { ingestBundle } from './domain/ingest.js';
import { loadBundle } from './adapters/directory-source.js';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const WEB = join(__dirname, '..', 'web');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

function json(res, code, body) {
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { return {}; }
}

// ---------- 序列化辅助 ----------
async function itineraryDetail(db, id) {
  const it = await one(db, `SELECT * FROM itineraries WHERE id=$1`, [id]);
  if (!it) return null;
  const snap = typeof it.static_snapshot === 'string' ? JSON.parse(it.static_snapshot) : it.static_snapshot;
  const rows = await query(db, `SELECT * FROM itinerary_segments WHERE itinerary_id=$1 ORDER BY idx`, [id]);
  const segments = [];
  for (const r of rows) {
    const evs = await query(db, `SELECT * FROM segment_evidence WHERE segment_id=$1 ORDER BY id`, [r.id]);
    segments.push({
      idx: r.idx, slot_key: r.slot_key, type: r.type, title: r.title, place_id: r.place_id,
      schedule_id: r.schedule_id,
      static: { start: r.static_start_utc ? new Date(r.static_start_utc).toISOString() : null,
                end: r.static_end_utc ? new Date(r.static_end_utc).toISOString() : null, status: r.static_status },
      current: { start: r.start_utc ? new Date(r.start_utc).toISOString() : null,
                 end: r.end_utc ? new Date(r.end_utc).toISOString() : null, status: r.current_status,
                 locked: r.locked, locked_reason: r.locked_reason,
                 infeasible_reasons: parseJ(r.infeasible_reasons),
                 alternatives: parseJ(r.alternatives), adjust_advice: r.adjust_advice },
      evidence: evs.map((e) => ({ scope: e.scope, type: e.condition_type, ref: e.ref_type,
        source_version_id: e.source_version_id, verdict: e.verdict, detail: parseJ(e.detail) })),
    });
  }
  const impacts = await query(db, `SELECT * FROM impact_relations WHERE itinerary_id=$1 ORDER BY id`, [id]);
  return {
    id: it.id, plan_date: it.plan_date instanceof Date ? it.plan_date.toISOString().slice(0,10) : String(it.plan_date).slice(0,10), tz: it.tz,
    frozen_at: new Date(it.frozen_at).toISOString(),
    created_from_version_id: it.created_from_version_id,
    version_high_water: snap.version_high_water,
    rollback_to_id: it.rollback_to_id, note: it.note,
    static_vs_current: snap.segments.map((s, i) => ({
      slot: s.slot_key, title: s.title,
      static_status: s.status, current_status: segments[i].current.status })),
    segments, impacts,
  };
}
function parseJ(v) { return typeof v === 'string' ? JSON.parse(v) : v; }

export async function createApp(db) {
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const p = url.pathname;

      // ---------- API ----------
      if (p.startsWith('/api/')) {
        // 故事页：交通、观景点、开放资料
        if (p === '/api/story' && req.method === 'GET') {
          const places = await query(db, `SELECT * FROM places ORDER BY array_position(ARRAY['pier','causeway','viewpoint','facility']::text[], kind), id`);
          const ferries = await query(db,
            `SELECT s.*, sv.source_key FROM schedules s JOIN source_versions sv ON sv.id=s.source_version_id
             WHERE s.source_version_id=(SELECT MAX(id) FROM source_versions WHERE kind='ferry')
             ORDER BY s.dep_date, s.dep_local`);
          const materials = await query(db,
            `SELECT m.*, sv.source_key FROM open_materials m JOIN source_versions sv ON sv.id=m.source_version_id`);
          const stations = await query(db,
            `SELECT ts.*, (SELECT json_agg(json_build_object('ts_utc',tp.ts_utc,'height_m',tp.height_m,'kind',tp.kind))
                          FROM tide_points tp WHERE tp.station_id=ts.id AND tp.station_version_id=ts.source_version_id) points
             FROM tide_stations ts WHERE ts.source_version_id=(SELECT MAX(id) FROM source_versions WHERE kind='tide')`);
          return json(res, 200, { places, ferries, materials, stations, notice: '示例数据，不保证现实交通可用' });
        }

        if (p === '/api/templates' && req.method === 'GET') return json(res, 200, listTemplates());

        // 行程列表
        if (p === '/api/itineraries' && req.method === 'GET') {
          const rows = await query(db, `SELECT id,plan_date,tz,frozen_at,rollback_to_id,note,created_from_version_id FROM itineraries ORDER BY id`);
          return json(res, 200, rows.map((r) => ({ ...r, plan_date: r.plan_date instanceof Date ? r.plan_date.toISOString().slice(0,10) : String(r.plan_date).slice(0,10), frozen_at: new Date(r.frozen_at).toISOString() })));
        }
        // 一次生成静态行程
        if (p === '/api/itineraries' && req.method === 'POST') {
          const b = await readBody(req);
          const out = await staticPlan(db, { templateKey: b.template_key ?? 'lighthouse-day', date: b.date ?? '2026-10-01', note: b.note });
          return json(res, 201, { id: out.id, segments: out.segments.length });
        }
        const mIt = p.match(/^\/api\/itineraries\/(\d+)\/?$/);
        if (mIt && req.method === 'GET') return json(res, 200, await itineraryDetail(db, Number(mIt[1])));

        // 局部重算
        const mRec = p.match(/^\/api\/itineraries\/(\d+)\/recompute$/);
        if (mRec && req.method === 'POST') {
          const out = await recompute(db, Number(mRec[1]));
          return json(res, 200, out);
        }
        // 用户锁定段
        const mLock = p.match(/^\/api\/itineraries\/(\d+)\/segments\/([\w-]+)\/lock$/);
        if (mLock && req.method === 'POST') {
          const b = await readBody(req);
          return json(res, 200, await lockSegment(db, Number(mLock[1]), mLock[2], b.reason));
        }
        // 应用替代候选
        const mAlt = p.match(/^\/api\/itineraries\/(\d+)\/segments\/([\w-]+)\/alternative$/);
        if (mAlt && req.method === 'POST') {
          const b = await readBody(req);
          return json(res, 200, await applyAlternative(db, Number(mAlt[1]), mAlt[2], b.candidate));
        }
        // 回滚
        const mRb = p.match(/^\/api\/itineraries\/(\d+)\/rollback$/);
        if (mRb && req.method === 'POST') {
          const b = await readBody(req);
          return json(res, 200, await rollbackItinerary(db, Number(mRb[1]), b.reason));
        }

        // 来源版本与影响关系
        if (p === '/api/admin/versions' && req.method === 'GET') {
          const rows = await query(db,
            `SELECT sv.id,sv.source_key,sv.kind,sv.fetched_at,sv.effective_from,sv.content_hash,sv.supersedes_id,sv.note,
                    (SELECT count(*) FROM impact_relations ir WHERE ir.caused_by_version_id=sv.id) impacts
             FROM source_versions sv ORDER BY sv.id`);
          return json(res, 200, rows.map((r) => ({ ...r,
              fetched_at: new Date(r.fetched_at).toISOString(),
              effective_from: r.effective_from ? (r.effective_from instanceof Date ? r.effective_from.toISOString().slice(0,10) : String(r.effective_from).slice(0,10)) : null,
              content_hash: r.content_hash.slice(0, 12) })));
        }
        if (p === '/api/admin/impacts' && req.method === 'GET') {
          const rows = await query(db, `SELECT * FROM impact_relations ORDER BY id DESC LIMIT 100`);
          return json(res, 200, rows.map((r) => ({ ...r, detail: parseJ(r.detail) })));
        }
        // 后台摄入：从可替换示例数据源目录重新读取（也支持 ?dir=）
        if (p === '/api/admin/ingest' && req.method === 'POST') {
          const b = await readBody(req);
          const dir = b.dir ?? join(__dirname, '..', 'data-source');
          const bundle = await loadBundle(dir);
          const report = await ingestBundle(db, b.bundle ?? bundle, {});
          return json(res, 200, report);
        }
        // 直接摄入任意内存 bundle（测试/替换源）
        if (p === '/api/admin/ingest-bundle' && req.method === 'POST') {
          const b = await readBody(req);
          if (!b.bundle) return json(res, 400, { error: '需要 bundle' });
          const report = await ingestBundle(db, b.bundle, { source_key: b.source_key });
          return json(res, 200, report);
        }
        if (p === '/api/admin/closures' && req.method === 'GET') {
          return json(res, 200, await query(db, `SELECT * FROM closures ORDER BY start_utc`));
        }
        // 离线包
        if (p === '/api/offline-bundle' && req.method === 'POST') {
          const b = await readBody(req);
          return json(res, 200, await buildOfflineBundle(db, { frozenDate: b.frozen_date ?? '2026-09-30', itineraryId: b.itinerary_id ?? null }));
        }
        if (p === '/api/offline-bundle/latest' && req.method === 'GET') {
          const r = await one(db, `SELECT * FROM offline_bundles ORDER BY id DESC LIMIT 1`);
          if (!r) return json(res, 404, { error: '尚无离线包' });
          return json(res, 200, parseJ(r.payload));
        }
        return json(res, 404, { error: 'not found' });
      }

      // ---------- 静态页 ----------
      let filePath = p === '/' ? '/story.html' : p;
      filePath = normalize(filePath).replace(/^(\.\.[/\\])+/, '');
      const abs = join(WEB, filePath);
      if (!abs.startsWith(WEB)) { res.writeHead(403); return res.end(); }
      const data = await readFile(abs);
      res.writeHead(200, { 'content-type': MIME[extname(abs)] ?? 'application/octet-stream' });
      res.end(data);
    } catch (e) {
      console.error(e);
      json(res, 500, { error: String(e?.message ?? e) });
    }
  });
  return server;
}

const PORT = process.env.PORT || 8080;
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const db = await getDb();
  const server = await createApp(db);
  server.listen(PORT, () => console.log(`海岛灯塔导览站: http://localhost:${PORT}/ (故事页)`));
}
