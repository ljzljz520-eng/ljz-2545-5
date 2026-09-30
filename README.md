# 海岛灯塔导览站（Lighthouse Island Guide）

潮汐 / 船班 / 临时封闭数据驱动、**每个建议都可追溯到来源版本与采用条件**的海岛灯塔导览系统。

## 快速开始

```bash
npm install
npm run init-db      # 建表 + 从可替换示例数据源摄入基线数据（--reset 清库）
npm start            # http://localhost:8080/story.html
npm test             # 17 项领域/系统验收测试
```

- 故事页：`/story.html`（交通、观景点、潮位与适用范围、开放资料）
- 行程：`/itinerary.html`（一次生成静态行程 ↔ 数据变更后局部重算、锁定、替代候选）
- 后台：`/admin.html`（摄入、场景注入、来源版本、影响关系）
- 离线包：`/offline.html`（冻结日期 + 更新缺口）
- 文档：`/docs/design.md`、`/docs/acceptance.md`

## 核心规则

- 潮位信息的**坐标、时区、适用范围**必须匹配地点；缺测/超范围**绝不推断可通行**；
- **临时封闭仅使对应访问段待调整**，并说明对下游接驳时间的连带影响；
- 船班取消/跨源矛盾/迁站/缺测/跨日接驳均有结构化不可行原因与替代候选；
- 用户锁定段不被数据变更自动改写；
- PG 保存**不可变来源版本**与「版本 → 行程段」的**影响关系**；
- 静态行程快照永不改变，可回滚旧缓存；
- 离线包是历史快照，**不保证现实交通可用**。

## 目录

```
db/schema.sql                 PostgreSQL schema（兼容独立 PG / PGlite）
src/data-source/              可替换的示例数据源（manifest + JSON）
src/server/adapters/          数据源适配器（可换成 HTTP/GTFS/CSV）
src/server/domain/            摄入/规划/局部重算/回滚/离线包
src/web/                      故事页、行程、后台、离线页与站内文档
test/                         验收测试（node:test + 内存 PG）
scripts/                      初始化、演示、潮汐数据生成
```
