# 海岛灯塔导览站（Lighthouse Guide Station）

一个面向「海岛灯塔 + 潮间带」的可导览站点：故事页展示交通、观景点与开放资料；
行程界面按时段（slot）组合访问段；后台从**可替换示例数据源**接收班次与封闭记录；
PostgreSQL 保存**来源版本、矛盾裁决与影响关系**；并提供**冻结离线包**与**建议条件追溯**。

## 核心安全规则（需求约束）

- 潮汐信息的**坐标 / 时区 / 适用范围**必须与地点匹配，否则潮间带段不可行。
- 潮汐**缺测绝不推断为可通行**（原因码 `tide_data_missing`）。
- **临时封闭**只让对应访问段进入「待调整（pending_adjustment）」，其它段不取消；
  其后**接驳时间的连带影响**单独记录并展示。
- 每个建议都可在页面「追溯该建议采用的条件」查看：地点/班次/潮汐证据、来源版本、
  冲突裁决、影响链。
- 离线包显示**冻结日期**与**更新缺口**，明确声明**不保证现实交通可用**。

## 静态生成 vs 局部重算

- `POST /api/plans/generate`：**一次生成静态行程**（全量评估，结果与后续数据变化绝缘）。
- `POST /api/plans/{id}/recompute`：数据变更后**局部重算**——仅重算直接命中段及其下游，
  写入 `impact_edges`（direct / knock_on）；**用户锁定段不移动**，冲突时返回 `locked_blocked`；
  不可行段给出结构化原因码与**替代候选**，可一键采用并连带重算下游接驳。

## 目录结构

```
app/
  main.py               FastAPI 页面 + API
  schema.sql            PostgreSQL 16 表结构
  adapters/             可替换数据源：Base / JsonFile / Dict(在线推送/测试桩)
  services/
    ingest.py           接收、版本化、矛盾裁决、回滚、基线差异
    planner.py          时段模板、潮汐匹配、择班、接驳、状态机
    plans.py            静态生成 / 局部重算 / 锁定 / 采用候选
    offline.py          冻结快照 + 更新缺口
    trace.py            建议条件追溯
    query.py            页面查询
data/samples/           default + ferry_cancel / conflict / coord_change / crossday
tests/                  适配验收(AC-1..9) + 5 类场景 + 锁定/候选/追溯/离线
docs/design.md          站内设计文档（页面 /docs-site 渲染）
scripts/                pg_ctl.sh、init_db.py
```

## 本地运行

```bash
# 首次：用户态 conda 环境（含 postgresql），详见下文「环境准备」
./scripts/pg_ctl.sh init && ./scripts/pg_ctl.sh start
export LIGHTHOUSE_DSN="host=/tmp user=node dbname=lighthouse port=5432"
python scripts/init_db.py default      # 可选其它场景名
uvicorn app.main:app --reload
# 或：./run.sh default
```

## 测试

```bash
pytest -q
```

覆盖：船班取消（不可行原因 + 替代候选 + 采用后连带重算）、不同来源矛盾（优先级裁决 + 留痕）、
地点改坐标（潮汐坐标不匹配）、旧缓存回滚、跨日接驳、潮汐缺测不放行、临时封闭仅一段待调整、
用户锁定段、离线包更新缺口、开放资料下载、全部页面渲染。

## 数据适配验收

见站内 `/docs-site`（或 `docs/design.md` §4.1），AC-1..AC-9；
对应自动测试 `tests/test_adapter_acceptance.py`。

## 环境准备（无 root 时）

本仓库在无 root 的 Debian 上用 Miniforge 装齐依赖：

```bash
curl -sL -o /tmp/mf.sh \
  https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash /tmp/mf.sh -b -p "$HOME/miniforge3"
$HOME/miniforge3/bin/conda create -y -n lighthouse python=3.11 \
  fastapi uvicorn psycopg2 pytest httpx jinja2 python-multipart pyyaml markdown
$HOME/miniforge3/bin/conda install -y -n lighthouse -c conda-forge postgresql=16
```

## 开放资料

`GET /open-data/places.csv`、`GET /open-data/ferries.json`（CC BY 4.0，附免责声明）。
