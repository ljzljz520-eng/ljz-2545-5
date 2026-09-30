-- ============================================================
-- 海岛灯塔导览站 - PostgreSQL schema (兼容 PostgreSQL 14+/PGlite)
-- 核心原则：
--   * 来源版本(source_versions)不可变(immutable)，摄入即冻结
--   * 每条业务数据都带 source_version_id，可回溯
--   * 数据变更 -> 影响关系(impact_relations) -> 行程段
--   * 行程段保留 static(一次生成) 与 current(局部重算) 两套视图
-- ============================================================

-- 不可变来源版本
CREATE TABLE IF NOT EXISTS source_versions (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_key      TEXT NOT NULL,                 -- 数据源标识，如 demo/ferry-2026Q4
  kind            TEXT NOT NULL CHECK (kind IN ('ferry','closure','tide','place','material','bundle','admin')),
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_from  DATE,                          -- 数据生效日期（业务日）
  content_hash    TEXT NOT NULL,                 -- 内容指纹，重复内容可识别
  raw_json        JSONB NOT NULL,
  note            TEXT,
  supersedes_id   BIGINT REFERENCES source_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_sv_lookup ON source_versions(source_key, kind, effective_from);

CREATE TABLE IF NOT EXISTS places (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('pier','viewpoint','causeway','station','facility','island')),
  lat             DOUBLE PRECISION NOT NULL,
  lng             DOUBLE PRECISION NOT NULL,
  tz              TEXT NOT NULL,                 -- IANA 时区，如 Asia/Shanghai
  tide_station_id TEXT,                          -- 适用的潮位站；缺则该地点不允许推断潮汐可通行
  description     TEXT,
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  updated_in_version_id BIGINT,                    -- 该地点记录被哪个版本改写
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 潮位站：坐标/时区/适用范围必须与地点匹配（应用层强校验）
CREATE TABLE IF NOT EXISTS tide_stations (
  id              TEXT NOT NULL,
  name            TEXT NOT NULL,
  lat             DOUBLE PRECISION NOT NULL,
  lng             DOUBLE PRECISION NOT NULL,
  tz              TEXT NOT NULL,
  applies_to      TEXT[] NOT NULL DEFAULT '{}',  -- 适用地点 id 列表（适用范围）
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (id, source_version_id)
);

-- 潮位点：固定时刻的预测/实测水位。不得跨点外推。版本随潮位站。
CREATE TABLE IF NOT EXISTS tide_points (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  station_id      TEXT NOT NULL,
  station_version_id BIGINT NOT NULL,
  ts_utc          TIMESTAMPTZ NOT NULL,          -- 绝对时刻
  height_m        DOUBLE PRECISION NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'prediction' CHECK (kind IN ('prediction','observed')),
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  UNIQUE (station_id, station_version_id, ts_utc, source_version_id),
  FOREIGN KEY (station_id, station_version_id)
    REFERENCES tide_stations(id, source_version_id)
);

-- 地点（多为潮间堤道）的可通行水位阈值
CREATE TABLE IF NOT EXISTS tide_thresholds (
  place_id        TEXT PRIMARY KEY REFERENCES places(id),
  max_height_m    DOUBLE PRECISION NOT NULL,     -- 水位 <= 该值方可能步行
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id)
);

-- 船班/巴士班次
CREATE TABLE IF NOT EXISTS schedules (
  id              TEXT NOT NULL,                  -- 班次业务 id（同一逻辑班次跨版本复用）
  mode            TEXT NOT NULL CHECK (mode IN ('ferry','bus')),
  route_name      TEXT NOT NULL,
  origin_id       TEXT NOT NULL REFERENCES places(id),
  destination_id  TEXT NOT NULL REFERENCES places(id),
  dep_date        DATE NOT NULL,
  dep_local       TIME NOT NULL,
  arr_local       TIME NOT NULL,
  tz              TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'scheduled'
                  CHECK (status IN ('scheduled','cancelled','delayed')),
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (id, source_version_id)
);

-- 临时封闭记录（仅影响对应访问段）
CREATE TABLE IF NOT EXISTS closures (
  id              TEXT PRIMARY KEY,
  place_id        TEXT NOT NULL REFERENCES places(id),
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ NOT NULL,
  reason          TEXT,
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (end_utc > start_utc)
);

-- 开放资料（故事页）
CREATE TABLE IF NOT EXISTS open_materials (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('doc','map','dataset','media')),
  url             TEXT,
  license         TEXT,
  publisher       TEXT,
  summary         TEXT,
  source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- 数据变更后的影响关系：受影响版本 -> 被影响的行程/段
CREATE TABLE IF NOT EXISTS impact_relations (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  caused_by_version_id BIGINT NOT NULL REFERENCES source_versions(id),
  subject_type    TEXT NOT NULL CHECK (subject_type IN ('schedule','closure','tide','place','tide_station')),
  subject_ref     TEXT NOT NULL,                 -- 业务 id
  impact_kind     TEXT NOT NULL CHECK (impact_kind IN
                    ('cancelled','time_changed','closed','tide_window_lost','tide_unknown',
                     'coords_changed','station_scope_mismatch','conflict','new_candidate')),
  itinerary_id    BIGINT,
  segment_id      BIGINT,
  detail          JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ir_version ON impact_relations(caused_by_version_id);
CREATE INDEX IF NOT EXISTS idx_ir_segment ON impact_relations(segment_id);

-- 行程
CREATE TABLE IF NOT EXISTS itineraries (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_date       DATE NOT NULL,
  tz              TEXT NOT NULL,
  created_from_version_id BIGINT REFERENCES source_versions(id), -- 一次生成时冻结的数据版本水位
  static_snapshot JSONB NOT NULL,                -- 一次生成的静态行程（永不改变）
  frozen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  rollback_to_id  BIGINT REFERENCES itineraries(id),
  note            TEXT
);

-- 访问段（静态段 + 当前重算状态）
CREATE TABLE IF NOT EXISTS itinerary_segments (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  itinerary_id    BIGINT NOT NULL REFERENCES itineraries(id) ON DELETE CASCADE,
  slot_key        TEXT NOT NULL,                 -- 模板槽位 key
  idx             INT NOT NULL,
  type            TEXT NOT NULL CHECK (type IN ('visit','access','buffer')),
  title           TEXT NOT NULL,
  place_id        TEXT REFERENCES places(id),
  start_utc       TIMESTAMPTZ,
  end_utc         TIMESTAMPTZ,
  schedule_id     TEXT,
  -- 静态（一次生成时冻结）
  static_start_utc TIMESTAMPTZ,
  static_end_utc   TIMESTAMPTZ,
  static_status    TEXT NOT NULL DEFAULT 'feasible'
                   CHECK (static_status IN ('feasible','infeasible','pending_adjustment')),
  -- 当前（局部重算后）
  current_status   TEXT NOT NULL DEFAULT 'feasible'
                   CHECK (current_status IN ('feasible','infeasible','pending_adjustment','locked')),
  locked           BOOLEAN NOT NULL DEFAULT FALSE,
  locked_reason    TEXT,
  infeasible_reasons JSONB NOT NULL DEFAULT '[]',  -- 结构化不可行原因
  alternatives     JSONB NOT NULL DEFAULT '[]',     -- 替代候选
  adjust_advice    TEXT,                           -- 对接驳时间的连带影响说明
  UNIQUE (itinerary_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_seg_itinerary ON itinerary_segments(itinerary_id);

-- 段的采用条件（证据链：每条建议依赖哪个来源版本/哪条记录/什么判定）
CREATE TABLE IF NOT EXISTS segment_evidence (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  segment_id      BIGINT NOT NULL REFERENCES itinerary_segments(id) ON DELETE CASCADE,
  scope           TEXT NOT NULL CHECK (scope IN ('static','current','alternative')),
  condition_type  TEXT NOT NULL CHECK (condition_type IN
                    ('schedule','closure','tide_window','tide_missing','connection',
                     'coords_match','station_scope','source_conflict','lock')),
  ref_type        TEXT,                          -- schedule/closure/tide_point/...
  source_version_id BIGINT REFERENCES source_versions(id),
  detail          JSONB NOT NULL DEFAULT '{}',
  verdict         TEXT NOT NULL CHECK (verdict IN ('pass','fail','warn','unknown'))
);
CREATE INDEX IF NOT EXISTS idx_ev_seg ON segment_evidence(segment_id);

-- 离线包（冻结日期与更新缺口）
CREATE TABLE IF NOT EXISTS offline_bundles (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  frozen_date     DATE NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload         JSONB NOT NULL,
  source_version_ids BIGINT[] NOT NULL DEFAULT '{}'
);
