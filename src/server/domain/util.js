// 时间与地理工具。
// 时区处理原则：所有时刻内部以 UTC(ISO) 保存；本地挂钟时间通过
// 数据源 manifest 中显式声明的 IANA tz + 固定 UTC 偏移换算，
// 不依赖运行机器的本地时区。

export const pad = (n) => String(n).padStart(2, '0');

export function ymd(date) {
  if (date instanceof Date) return date.toISOString().slice(0, 10);
  return String(date);
}

// 本地挂钟时间 + 偏移(分钟) -> UTC Date
export function localToUtc(dateYmd, hhmm, offsetMin) {
  const [h, m] = hhmm.split(':').map(Number);
  const iso = `${dateYmd}T${pad(h)}:${pad(m ?? 0)}:00.000${formatOffset(offsetMin)}`;
  return new Date(iso);
}
// UTC Date + 偏移 -> "YYYY-MM-DDTHH:MM"（该时区挂钟）
export function utcToLocal(date, offsetMin) {
  const ms = date.getTime() + offsetMin * 60000;
  return new Date(ms).toISOString().slice(0, 16);
}
function formatOffset(totalMin) {
  const sign = totalMin >= 0 ? '+' : '-';
  const a = Math.abs(totalMin);
  return `${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}

// 给 HH:MM 加分钟数，返回 { dateYmd, hhmm }，自动跨日（跨日接驳核心）
export function addMinutes(dateYmd, hhmm, mins) {
  const [h, m] = hhmm.split(':').map(Number);
  let total = h * 60 + m + mins;
  const d = new Date(dateYmd + 'T00:00:00Z');
  const dayShift = Math.floor(total / 1440);
  total = ((total % 1440) + 1440) % 1440;
  d.setUTCDate(d.getUTCDate() + dayShift);
  return { dateYmd: ymd(d), hhmm: `${pad(Math.floor(total / 60))}:${pad(total % 60)}`, dayShift };
}

export function isoUtc(date) { return date.toISOString(); }

// Haversine 距离（米）
export function distanceM(lat1, lng1, lat2, lng2) {
  const R = 6371000;
  const toRad = (x) => (x * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// 潮汐判定：仅在 [needStart, needEnd] 被潮位点【完全覆盖】时才允许判定。
// 缺测 / 覆盖不足 -> unknown（绝不推断可通行）。
// 返回 { verdict:'pass'|'fail'|'unknown', maxHeight, covering:[...] , gap }
export function evaluateTide(points, needStart, needEnd, maxHeight) {
  const inside = points
    .filter((p) => p.ts >= needStart.getTime() && p.ts <= needEnd.getTime())
    .sort((a, b) => a.ts - b.ts);
  // 允许纳入需求起点之前、终点之后的相邻点用于区间内线性插值
  const before = points.filter((p) => p.ts < needStart.getTime()).sort((a,b)=>b.ts-a.ts)[0];
  const after = points.filter((p) => p.ts > needEnd.getTime()).sort((a,b)=>a.ts-b.ts)[0];
  const covering = [...(before ? [before] : []), ...inside, ...(after ? [after] : [])]
    .sort((a, b) => a.ts - b.ts);
  if (covering.length < 2) {
    return { verdict: 'unknown', reason: 'tide_missing', covering: covering.length, gap: '需要至少两个潮位点' };
  }
  // 关键规则：绝不外推——需求区间必须完整落在已有点位的[首,末]范围之内。
  // 区间内相邻点位之间允许【线性插值】得到端点时刻水位（非外推、非编造）。
  const first = covering[0], last = covering[covering.length - 1];
  if (first.ts > needStart.getTime() || last.ts < needEnd.getTime()) {
    return { verdict: 'unknown', reason: 'tide_missing', gap: '需求时段超出潮位点覆盖范围（禁止外推）',
      covered: [new Date(first.ts).toISOString(), new Date(last.ts).toISOString()] };
  }
  const valueAt = (ts) => {
    if (ts <= first.ts) return first.height;
    if (ts >= last.ts) return last.height;
    for (let i = 0; i < covering.length - 1; i++) {
      const a = covering[i], b = covering[i + 1];
      if (ts >= a.ts && ts <= b.ts) {
        const t = (ts - a.ts) / (b.ts - a.ts || 1);
        return a.height + (b.height - a.height) * t;
      }
    }
    return last.height;
  };
  const startV = valueAt(needStart.getTime());
  const endV = valueAt(needEnd.getTime());
  // 峰值只统计【需求区间内部】实测点 + 两端插值；锚点(before/after)仅用于插值，不计入
  const insideHeights = inside.map((p) => p.height);
  const maxSeen = Math.max(startV, endV, ...insideHeights);
  if (maxSeen > maxHeight) {
    return { verdict: 'fail', reason: 'tide_too_high', maxHeightSeen: +maxSeen.toFixed(2),
      endpoints: { start: +startV.toFixed(2), end: +endV.toFixed(2) } };
  }
  return { verdict: 'pass', maxHeightSeen: +maxSeen.toFixed(2),
    endpoints: { start: +startV.toFixed(2), end: +endV.toFixed(2) } };
}

// 找满足水位阈值的通行窗口（候选替代）。同样要求完全落在已有点位范围内。
export function findTideWindows(points, maxHeight, minMin = 20, spanMin = 1440) {
  const sorted = [...points].sort((a, b) => a.ts - b.ts);
  const windows = [];
  let runStart = null, runPrev = null;
  for (const p of sorted) {
    if (p.height <= maxHeight) {
      if (runStart === null) runStart = p.ts;
      runPrev = p.ts;
    } else {
      if (runStart !== null && (runPrev - runStart) / 60000 >= minMin) {
        windows.push({ start: new Date(runStart), end: new Date(runPrev) });
      }
      runStart = null;
    }
  }
  if (runStart !== null && (runPrev - runStart) / 60000 >= minMin) {
    windows.push({ start: new Date(runStart), end: new Date(runPrev) });
  }
  return windows;
}

export function overlap(startA, endA, startB, endB) {
  return startA < endB && startB < endA;
}
