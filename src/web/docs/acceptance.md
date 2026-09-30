# 数据适配验收（Data Adaptor Acceptance）

> 与《站内设计文档》配套。本文档定义“可替换示例数据源”接入时的验收标准、
> 验收用例与回归命令。对应自动化测试见 `test/`（`npm test`，当前 17 项全通过）。

## 1. 数据源契约

目录示例：`src/data-source/`

| 文件 | 必需 | 关键字段 | 验收要点 |
|---|---|---|---|
| `manifest.json` | 是 | `source_key`,`tz`,`utc_offset_min`,`files` | 时区必须是 IANA；偏移与时区一致 |
| `places.json` | 是 | `id,kind,lat,lng,tz,tide_station_id,walk_threshold_m` | 坐标合法；潮位依赖地点必须绑定站点 |
| `ferry-*.json` | 是 | 班次 `id,origin_id,destination_id,dep_date,dep_local,arr_local,status` | OD 必须引用存在地点；本地时间 24h `HH:MM`，允许跨日 |
| `closures-*.json` | 否 | `place_id,start_local,end_local,reason` | 用数据源时区换算 UTC；end>start |
| `tides-*.json` | 条件 | `stations[].{id,lat,lng,tz,applies_to,max_match_distance_m}` + `points[].{ts(UTC),height_m}` | 站点必须通过“三匹配”；点用 UTC ISO |
| `materials.json` | 否 | `title,kind,url,license,publisher` | 开放许可信息可展示 |

### 1.1 潮位站“三匹配”验收（必须全部满足，否则拒绝推断）
1. **时区匹配**：`station.tz === place.tz`；
2. **适用范围匹配**：`place.id ∈ station.applies_to`；
3. **坐标匹配**：`haversine(place, station) ≤ station.max_match_distance_m`。

任一项失败：摄入报告 `rejected[].type='place_tide_binding'`，
落库 `impact_relations.impact_kind='station_scope_mismatch'`，
规划时该步行段判 `tide_station_mismatch`（不推断可通行）。

### 1.2 潮汐覆盖验收
- 需求窗口必须被潮位点**完全覆盖**；范围内可线性插值，**禁止外推**；
- 覆盖不足/缺测/单一点 ⇒ `tide_unknown`。

## 2. 版本与幂等

- 每次摄入生成不可变 `source_versions`（SHA-256 指纹，记录 `supersedes_id`）；
- 同 `source_key+kind+生效日+同指纹` 重复摄入必须**幂等**（返回同一版本 id）；
- 不同 `source_key` 视为不同来源；其对同一班次的分歧进入矛盾检测。

## 3. 验收场景（与测试一一对应）

| # | 场景 | 操作 | 期望结果 | 测试 |
|---|---|---|---|---|
| ① | 船班取消 | 新来源推 F101 `cancelled` 后局部重算 | s1 `pending_adjustment`（原因 `cancelled`）；给 F102/F900 候选；固定时刻的回程段保留；时间锚定步行段级联 `no_anchor` | `planner.test.js` 场景① |
| ①b| 用户锁定段 | 先锁定 s1 再取消并重算 | s1 保持 `locked`，不自动改时，附接驳连带说明 | 场景①锁定 |
| ② | 不同来源矛盾 | 第二来源把 F101 改成 09:05 在航 | s1 原因 `source_conflict`，保守不采用，证据列出全部变体 | 场景② |
| ②b| 时刻格式归一 | 第二来源以 `HH:MM` 重报同一班次（库内为 `HH:MM:SS`） | 不产生伪矛盾；仅状态/时刻真差异才报 | `system.test.js` 跨源矛盾 |
| ③ | 地点改坐标/迁站 | 潮位站坐标改到 30.12,122.66（超距） | 摄入即 `rejected`+`station_scope_mismatch`；重算后步行段 `tide_station_mismatch`，不推断 | 场景③ |
| ④ | 旧缓存回滚 | 取消→重算→rollback | 新行程段 current 恢复为 static `feasible`，`rollback_to_id` 指回，静态证据保留 | `system.test.js` 回滚 |
| ⑤ | 跨日接驳 | 生成 `cross-day` 模板 | 末班船 23:30 出发、**次日** 00:20 到达；接驳缓冲满足 | 跨日模板 |
| ⑥ | 临时封闭 | 北堤 08:00–12:00 封闭后重算 | 仅 s3（及时间链下游）待调整；去程船 s1 仍 feasible；原因 `closed` | 封闭用例 |
| ⑦ | 潮位缺测 | 新站点仅 1 个潮位点 | 步行段 `tide_unknown`，不推断可通行 | 场景⑤缺测 |
| ⑧ | 更新缺口 | 冻结日早于数据生效日生成离线包 | `update_gap.count>0` 且列出版本；含“不保证现实交通可用” | 离线包 |
| ⑨ | 幂等 | 基线包摄入两次 | ferry 版本 id 不变 | 幂等用例 |

## 4. 人工验收步骤（UI）

1. `npm run init-db` 后 `npm start`，打开 `http://localhost:8080/story.html`：
   核对地点坐标/时区、班次、潮位站适用范围、开放资料与免责提示。
2. 行程页：选择 `lighthouse-day / 2026-10-01` → 一次生成，确认 8 段全部可行，
   展开 s3/s5 证据看到 `coords_match:pass` 与 `tide_window:pass`。
3. 后台页依次点 ①~⑤ 注入场景数据 → 回行程页「局部重算」：
   - 观察仅相关槽位变化、其他槽位显示“保留”；
   - 锁定某段后再次重算，确认其不被改写；
   - 采用 F102 候选，确认 s1 恢复可行并提示复核下游接驳。
4. 行程页「回滚到静态快照」，确认状态恢复且出现回滚来源标注。
5. 离线页以 2026-09-30 生成离线包，确认冻结日期与更新缺口数量、免责声明。

## 5. 适配器替换检查清单（对接真实数据时）

- [ ] 输出字段名/单位与契约一致（水位米、坐标 WGS84、时间本地+显式时区）
- [ ] 潮位点时间序列为 UTC，覆盖目标行程窗口前后至少各一个点
- [ ] 每个潮依赖地点都能通过三匹配，否则明确不提供潮汐结论（而不是猜）
- [ ] 班次取消/延误使用受控状态枚举；跨源差异通过矛盾检测而非静默覆盖
- [ ] 封闭记录使用数据源时区，闭合区间且 end>start
- [ ] 全量回归：`npm test` 全部通过
