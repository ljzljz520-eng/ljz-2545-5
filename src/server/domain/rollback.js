// 旧缓存回滚：行程的 static_snapshot 永不被重算改写，
// 因此可以把“当前(current)”状态恢复回一次生成时的静态状态。
// 回滚本身也记录为一个新行程行（rollback_to_id 指回原行程），保留审计轨迹。
import { one, query } from '../db.js';

export async function rollbackItinerary(db, itineraryId, reason) {
  const it = await one(db, `SELECT * FROM itineraries WHERE id=$1`, [itineraryId]);
  if (!it) throw new Error('行程不存在');
  const snap = typeof it.static_snapshot === 'string' ? JSON.parse(it.static_snapshot) : it.static_snapshot;

  const row = await one(db,
    `INSERT INTO itineraries (plan_date,tz,created_from_version_id,static_snapshot,rollback_to_id,note)
     VALUES ($1,$2,$3,$4::jsonb,$5,$6) RETURNING id`,
    [it.plan_date, it.tz, it.created_from_version_id, JSON.stringify(snap), itineraryId,
     `回滚自 #${itineraryId}：${reason ?? '恢复一次生成静态行程'}`]);
  const newId = row.id;

  const segs = await query(db, `SELECT * FROM itinerary_segments WHERE itinerary_id=$1 ORDER BY idx`, [itineraryId]);
  for (const s of segs) {
    const nr = await one(db,
      `INSERT INTO itinerary_segments
       (itinerary_id,slot_key,idx,type,title,place_id,start_utc,end_utc,schedule_id,
        static_start_utc,static_end_utc,static_status,current_status,locked,
        infeasible_reasons,alternatives,adjust_advice,locked_reason)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12,FALSE,'[]'::jsonb,'[]'::jsonb,NULL,NULL)
       RETURNING id`,
      [newId, s.slot_key, s.idx, s.type, s.title, s.place_id, s.static_start_utc, s.static_end_utc,
       s.schedule_id, s.static_start_utc, s.static_end_utc, s.static_status]);
    // 复制静态证据
    const evs = await query(db, `SELECT * FROM segment_evidence WHERE segment_id=$1 AND scope='static'`, [s.id]);
    for (const e of evs) {
      await one(db,
        `INSERT INTO segment_evidence (segment_id,scope,condition_type,ref_type,source_version_id,detail,verdict)
         VALUES ($1,'static',$2,$3,$4,$5::jsonb,$6)`,
        [nr.id, e.condition_type, e.ref_type, e.source_version_id,
         JSON.stringify(typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail), e.verdict]);
    }
  }
  return { new_id: newId, rolled_back_from: itineraryId, reason, restored_segments: segs.length };
}
