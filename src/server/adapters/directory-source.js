// 示例数据源适配器：从目录读取 manifest + 各数据文件。
// 这是“可替换的示例数据源”边界——真实项目可在此换成 HTTP/GTFS/CSV 适配器，
// 只要输出同样的归一化 bundle 结构，摄入/规划/页面无需改动。
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

export async function loadBundle(dir) {
  const manifest = JSON.parse(await readFile(join(dir, 'manifest.json'), 'utf8'));
  const files = manifest.files || {};
  const load = async (key) =>
    files[key] ? JSON.parse(await readFile(join(dir, files[key]), 'utf8')) : null;
  const bundle = {
    source_key: manifest.source_key,
    tz: manifest.tz,
    utc_offset_min: manifest.utc_offset_min,
    notice: manifest.notice ?? null,
    ferry: await load('ferry'),
    closures: await load('closures'),
    tides: await load('tides'),
    places: await load('places'),
    materials: await load('materials'),
  };
  bundle.content_hash = hashBundle(bundle);
  return bundle;
}

// 亦可直接接收内存中的 bundle（测试/替换源走同一条代码路径）
export function withSourceKey(bundle, source_key) {
  return { ...bundle, source_key, content_hash: hashBundle({ ...bundle, source_key }) };
}

export function hashBundle(bundle) {
  return createHash('sha256').update(JSON.stringify(sortKeys(bundle))).digest('hex');
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]));
  }
  return v;
}
