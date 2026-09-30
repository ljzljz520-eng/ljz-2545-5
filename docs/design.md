# 海岛灯塔导览站 · 站内设计文档

版本：v1.0　｜　维护：导览站建设组　｜　最后更新：2026-09-30

## 1. 目标与范围

为海岛灯塔（北礁灯塔 + 五虎礁潮间带）建设一个可公开访问的导览站，包含：

1. **故事页**：展示交通方式、观景点、开放资料（Open Data，CC BY 4.0）。
2. **行程界面**：按时段（slot）组合访问段（segment），呈现每段状态、接驳、锁定与替代候选。
3. **数据后台**：从**可替换示例数据源**（文件 / 在线推送 / 内存桩）接收班次与封闭记录。
4. **持久化**：PostgreSQL 保存**来源版本**、**矛盾裁决**与**影响关系**（impact edge）。
5. **离线包**：冻结日期的快照，显式展示**更新缺口**，并声明**不保证现实交通可用**。

### 1.1 不可妥协的安全规则

- 潮汐信息的**坐标、时区、适用范围**必须与地点匹配，否则潮间带段直接判不可行。
- 潮汐**缺测（level_cm 为 NULL）不能推断为可通行**。
- **临时封闭**只使对应的访问段进入「待调整」，**不**作废整条行程；其后接驳时间的连带影响必须单独说明。
- 页面必须能**追溯每个建议采用的条件**（地点/班次/潮汐记录及其来源版本）。

## 2. 技术架构

| 层 | 选型 | 说明 |
|---|---|---|
| 前端 | Jinja2 模板 + 原生 JS/Fetch | 故事页、行程页、后台、离线页、文档页 |
| API | FastAPI（Python 3.11） | 页面与 JSON API 同源 |
| 数据源 | 适配器契约 `BaseSourceAdapter` | `JsonFileAdapter` / `DictAdapter`，可替换 |
| 数据库 | PostgreSQL 16 | 版本链、冲突表、影响边、行程快照 |
| 测试 | pytest + TestClient + 真实 PG | 五类场景 + 适配验收 |

```
示例场景 JSON ─┐
在线推送 JSON ─┼─► Adapter ─► ingest(版本化/裁决/差异) ─► PostgreSQL
内存测试桩 ───┘                                        │
                                                       ├─► planner.static（一次生成）
                       plans/segments/connections ◄────┴─► planner.incremental（局部重算）
                                                       │
                              offline bundle（冻结+缺口）└─► trace（条件追溯）
```

## 3. 领域模型（PostgreSQL）

- **source_versions**：每次接收一个不可变版本（source_key + revision + sha256 + priority），
  通过 `superseded_by` 形成版本链，支撑回滚与缺口计算。
- **places / tide_points**：地点与验潮站；地点经 `tide_point_code` 绑定验潮站。
- **ferry_records**：班次（mode=ferry/shuttle），带日期、状态、来源版本。
- **closure_records**：临时封闭（target_type=place/route + 时间区间）。
- **tide_records**：潮汐观测（low/high，level_cm 可空=缺测）。
- **data_conflicts**：矛盾实体的胜/败版本快照与裁决理由。
- **plans / segments / connections**：行程、访问段、相邻接驳。
- **impact_edges**：数据版本 → 段/接驳的直接/连带影响。
- **alternatives**：每段的替代候选（可被用户采用）。
- **offline_bundles**：冻结时间、覆盖日、版本集合、缺口来源。

## 4. 数据适配契约（验收对象）

任何来源必须输出：

```json
{
  "source_key": "ferry.official",
  "revision": "2026-10-03-r2",
  "kind": "mixed",
  "priority": 10,
  "payload": {
    "ferries":  [ { "mode":"ferry", "route_code":"F1", "date":"2026-10-03",
                    "depart_place":"MAIN_DOCK","arrive_place":"ISLAND_DOCK",
                    "depart_at":"2026-10-03T08:00:00+08:00",
                    "arrive_at":"2026-10-03T08:45:00+08:00",
                    "status":"scheduled|cancelled|delayed" } ],
    "shuttles": [ ],
    "closures": [ { "closure_key":"CL-1", "target_type":"place|route",
                    "target_code":"LIGHTHOUSE",
                    "starts_at":"...+08:00","ends_at":"...+08:00","reason":"..." } ],
    "places": [ ], "tide_points": [ ], "tides": [ ]
  }
}
```

### 4.1 数据适配验收标准（Acceptance Criteria）

| 编号 | 验收项 | 通过判据 |
|---|---|---|
| AC-1 | 必填字段校验 | 缺 `source_key/revision/payload` 返回 422 |
| AC-2 | 幂等接收 | 同 key+revision+内容重复接收，版本号不增加（noop=true） |
| AC-3 | 版本链 | 新 revision 写入后，旧版本 `superseded_by` 指向新版本 |
| AC-4 | 矛盾裁决 | 同实体两来源不一致时，priority 高者生效，`data_conflicts` 记录双方快照 |
| AC-5 | 取消识别 | status=cancelled 产生 `ferry_cancel` 变更 |
| AC-6 | 封闭识别 | 新封闭产生 `closure_added` 变更 |
| AC-7 | 缺测识别 | tide.level_cm=null 产生 `tide_missing` 变更，规划端拒绝放行 |
| AC-8 | 回滚 | 回滚后权威行恢复旧 revision 内容，版本链反转 |
| AC-9 | 可替换 | 文件、在线推送、内存桩三种适配器通过同一契约被消费 |

## 5. 规划器：静态生成 vs 局部重算

### 5.1 时段模板（slot）

`morning_ferry → dock_to_lh → lighthouse_visit → reef_window → lh_to_dock → return_ferry`

每段绑定窗口、地点、类型（ferry/shuttle/visit/tide_visit）。

### 5.2 一次生成静态行程（static）

- 对全部 6 段按当前权威数据**一次性**评估；
- visit/tide 段以上游到达时间为锚，取「窗口起点 vs 到达」较晚者；
- 计算全部相邻接驳（缓冲、跨日、冲突）；
- 记录基线来源版本集合 `based_on_versions`。

特点：简单、结果确定；任何数据变化都**不会**自动反映——必须显式重算。

### 5.3 数据变更后局部重算（incremental）

- ingest 返回差异（`ferry_cancel / closure_added / place_coord / tide_missing / ferry_time`）；
- 映射到直接命中槽位，从**最早命中段**起向下游传播；
- 重算段并写入 `impact_edges`（direct/knock_on）；
- **用户锁定段不移动、不改时**，仅追加说明；若新数据下该段不可行，列入 `locked_blocked`，
  传播在该段被阻挡（锚点保持旧时刻）；
- 接驳整体重建，受影响接驳打 `knock_on` 标记并解释对接驳时间的连带影响。

> 比较结论：静态模式 O(全量) 且结果与数据变化绝缘；增量模式 O(命中段及其下游)，
> 保留锁定意图并给出不可行原因与替代候选，但实现复杂度更高、需维护影响集。

### 5.4 段状态机

`proposed → feasible / infeasible / pending_adjustment`；用户锁定后显示 `locked`。

- **infeasible**：班次取消且无备选 / 无班次 / 潮汐不匹配或缺测 / 水位不安全；给出结构化原因码。
- **pending_adjustment**：仅封闭触发（place 或 route）；提供替代候选，等待调整。
- **替代候选**：同日其它计划班次；用户可一键采用并连带重算下游。

### 5.5 接驳规则

- 最小缓冲 15 分钟；负值=时间冲突，0–14 分钟=缓冲不足；
- 相邻段日期不同即标记 **cross_day（跨日接驳）**，提示确认夜间接驳/末班；
- 上游段时间未定，接驳标记为不可成立。

## 6. 潮汐匹配细则

对 `tide_visit` 段依次校验：

1. 地点绑定了验潮站；
2. 验潮站 `applies_to` 包含该地点 code；
3. 时区一致；
4. 坐标距离 ≤ 2 km（Haversine）；
5. 窗口内存在 `kind=low` 观测；
6. **该低潮 level_cm 不为 NULL（缺测不放行）**；
7. 最低安全水位 ≤ 验潮站 `max_safe_cm`。

证据（地点、验潮站、距离、每条低潮观测、来源版本）整段写入 `segments.evidence`，供追溯。

## 7. 追溯（Traceability）

`GET /api/trace/{segment_id}` 返回：

- 段的状态/时刻/`record_ref`/`evidence`；
- 证据中引用的全部**来源版本**；
- 指向该段的 **impact_edges**（哪个版本的什么变更、直接还是连带）；
- 与段实体相关的**冲突裁决**；
- 替代候选与相邻接驳可行性。

页面「追溯该建议采用的条件」按钮直接展示该 JSON。

## 8. 离线包

- `POST /api/admin/offline-bundle` 冻结当时所有当前来源版本与业务数据；
- 包内字段：`frozen_at`（冻结日期）、`covers_date`、版本清单、`update_gap`；
- `update_gap` = 冻结版本之后又出现新版本的来源（更新缺口）；
- 页面固定声明：**冻结快照，不保证现实交通可用**。

## 9. 测试场景（pytest）

| 场景 | 数据操作 | 预期 |
|---|---|---|
| 船班取消 | 接入 ferry_cancel 场景 | 进岛段 infeasible（ferry_cancelled），出现 F1B 候选；采用后下游时刻连带重算 |
| 来源矛盾 | 官方(10) vs 社区(5) | 官方时刻生效；data_conflicts 有留痕；行程采用官方证据 |
| 地点改坐标 | REEF 漂移约 20km | reef_window infeasible，原因码 tide_coord_mismatch；不推断可通行 |
| 旧缓存回滚 | 接入 r3 后回滚到 r1 | 权威数据恢复 r1；增量重算恢复段状态 |
| 跨日接驳 | crossday 夜航场景 | F1→S1 接驳标记 cross_day=true 且缓冲被正确计算 |
| 潮汐缺测 | 将低潮置 NULL | reef_window infeasible，原因码 tide_data_missing |
| 临时封闭 | 推送 LIGHTHOUSE 封闭 | 仅灯塔段 pending_adjustment，其它段不取消；其后接驳标注 knock_on |

## 10. API 速览

```
POST /api/plans/generate                 一次静态生成
POST /api/plans/{id}/recompute           局部重算（可带 source 推送）
POST /api/segments/{id}/lock             锁定/解锁段
POST /api/segments/{id}/alternative/{a}  采用替代候选
GET  /api/trace/{segment_id}             条件追溯
POST /api/admin/ingest-file              从文件/目录接收
POST /api/admin/ingest-push              在线推送来源包
POST /api/admin/rollback                 旧缓存回滚
POST /api/admin/offline-bundle           冻结离线包
GET  /open-data/places.csv,/open-data/ferries.json  开放资料
```

## 11. 本地运行

```bash
# 1) 用户态 PostgreSQL（conda 环境）
./scripts/pg_ctl.sh init && ./scripts/pg_ctl.sh start
# 2) 建表 + 装载默认场景
export LIGHTHOUSE_DSN="postgresql://node@/lighthouse?host=/tmp"
python scripts/init_db.py default
# 3) 启动
uvicorn app.main:app --reload
# 4) 测试
pytest -q
```
