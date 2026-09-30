// 生成示例潮位数据：逐时整点，2026-10-01 起 48 小时（UTC 存储）。
// 本地(Asia/Shanghai, +08:00)设计两条低潮步行窗口：
//   窗口A ~09:00-12:00（含 10:00-11:30 步行段）
//   窗口B ~14:30-16:00（替代候选窗口）
// 阈值 0.7m；其余时段高潮不可通行。
import { writeFileSync } from 'node:fs';

const OFFSET = 480;
function localToIso(day, hh, mm = 0) {
  const total = hh * 60 + mm - OFFSET;
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCMinutes(d.getUTCMinutes() + total);
  return d.toISOString();
}
// [本地时, 本地分, 水位m]；窗口A 09:00-12:00，13:00 高潮，窗口B 14:00-17:00
const env = [
  [0, 0, 1.40], [8, 0, 0.45], [11, 30, 0.45],
  [12, 30, 1.40], [13, 30, 1.40],
  [14, 0, 0.45], [17, 30, 0.45], [18, 0, 1.40], [23, 59, 1.40]
];
const anchors = env.map(([h, m, v]) => [h * 60 + m, v]);
function heightAt(minOfDay) {
  for (let i = 0; i < anchors.length - 1; i++) {
    const [a, h0] = anchors[i]; const [b, h1] = anchors[i + 1];
    if (minOfDay >= a && minOfDay <= b) {
      const t = (minOfDay - a) / (b - a);
      return +(h0 + (h1 - h0) * t).toFixed(2);
    }
  }
  return 1.4;
}
const points = [];
for (const day of ['2026-10-01', '2026-10-02', '2026-10-03']) {
  for (let h = 0; h < 24; h++) {
    points.push({ ts: localToIso(day, h), height_m: heightAt(h * 60), kind: 'prediction' });
  }
}
const doc = {
  kind: 'tide', effective_from: '2026-10-01', tz: 'Asia/Shanghai',
  stations: [{
    id: 'ts-causeway', name: '北堤潮位站', lat: 29.9608, lng: 122.4258, tz: 'Asia/Shanghai',
    applies_to: ['causeway-north', 'viewpoint-lighthouse'],
    max_match_distance_m: 800
  }],
  points
};
writeFileSync('src/data-source/tides-2026-10.json', JSON.stringify(doc, null, 2));
console.log('points:', points.length);
