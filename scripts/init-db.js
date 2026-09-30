// 初始化数据库：建表 + 从可替换示例数据源目录摄入基线数据。
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getDb, resetDb } from '../src/server/db.js';
import { ingestBundle } from '../src/server/domain/ingest.js';
import { loadBundle } from '../src/server/adapters/directory-source.js';

const root = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const db = await getDb();
if (process.argv.includes('--reset')) await resetDb(db);
const schema = await readFile(join(root, 'db', 'schema.sql'), 'utf8');
await db.exec(schema);
const bundle = await loadBundle(join(root, 'src', 'data-source'));
const report = await ingestBundle(db, bundle, {});
console.log('初始化完成：', JSON.stringify(report.versions, null, 0));
if (report.warnings.length) console.log('校验告警：', report.warnings);
if (report.conflicts.length) console.log('来源矛盾：', report.conflicts);
process.exit(0);
