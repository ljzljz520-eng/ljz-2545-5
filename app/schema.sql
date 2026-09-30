-- 海岛灯塔导览站 · PostgreSQL 16 schema
-- 设计原则：所有业务数据均带来源版本(source_version_id)；冲突显式裁决并留痕。

CREATE TABLE IF NOT EXISTS source_versions (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL,
    revision TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    superseded_by BIGINT REFERENCES source_versions(id),
    UNIQUE (source_key, revision)
);

CREATE TABLE IF NOT EXISTS places (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('dock','lighthouse','reef','viewpoint','shuttle_stop','information')),
    lat NUMERIC(9,6) NOT NULL,
    lon NUMERIC(9,6) NOT NULL,
    timezone TEXT NOT NULL,
    tide_point_code TEXT,
    source_version_id BIGINT REFERENCES source_versions(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 班次（船班/接驳车）。一条记录 = 某一天的一个班次实例
CREATE TABLE IF NOT EXISTS ferry_records (
    id BIGSERIAL PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('ferry','shuttle')),
    route_code TEXT NOT NULL,
    date DATE NOT NULL,
    depart_place TEXT NOT NULL,
    arrive_place TEXT NOT NULL,
    depart_at TIMESTAMPTZ NOT NULL,
    arrive_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled','cancelled','delayed')),
    source_version_id BIGINT REFERENCES source_versions(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mode, route_code, date, source_version_id)
);

-- 封闭记录（临时封闭）
CREATE TABLE IF NOT EXISTS closure_records (
    id BIGSERIAL PRIMARY KEY,
    closure_key TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('place','route')),
    target_code TEXT NOT NULL,
    reason TEXT,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    source_version_id BIGINT REFERENCES source_versions(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (closure_key, source_version_id)
);

-- 潮汐观测/预报点
CREATE TABLE IF NOT EXISTS tide_records (
    id BIGSERIAL PRIMARY KEY,
    tide_point_code TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    level_cm INTEGER,              -- NULL = 缺测
    kind TEXT NOT NULL CHECK (kind IN ('low','high')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_version_id BIGINT REFERENCES source_versions(id),
    UNIQUE (tide_point_code, observed_at, source_version_id)
);

-- 潮汐点适用范围（与地点绑定）
CREATE TABLE IF NOT EXISTS tide_points (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat NUMERIC(9,6) NOT NULL,
    lon NUMERIC(9,6) NOT NULL,
    timezone TEXT NOT NULL,
    applies_to TEXT[] NOT NULL,
    max_safe_cm INTEGER NOT NULL,
    source_version_id BIGINT REFERENCES source_versions(id)
);

-- 来源矛盾裁决留痕
CREATE TABLE IF NOT EXISTS data_conflicts (
    id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    winner_version_id BIGINT REFERENCES source_versions(id),
    loser_version_id BIGINT REFERENCES source_versions(id),
    winner_snapshot JSONB NOT NULL,
    loser_snapshot JSONB NOT NULL,
    reason TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 行程（方案）
CREATE TABLE IF NOT EXISTS plans (
    id BIGSERIAL PRIMARY KEY,
    travel_date DATE NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','static_generated','recomputing','active','superseded')),
    generated_mode TEXT NOT NULL DEFAULT 'static' CHECK (generated_mode IN ('static','incremental')),
    based_on_versions JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 生成时各来源最新版本 id 集合
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS segments (
    id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    slot_code TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ferry','shuttle','visit','tide_visit','open_data')),
    title TEXT NOT NULL,
    from_place TEXT,
    to_place TEXT,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed','feasible','infeasible','pending_adjustment','locked')),
    infeasible_reason TEXT,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    record_ref JSONB,        -- 采用班次/封闭记录引用
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 条件追溯链
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (plan_id, position)
);

CREATE TABLE IF NOT EXISTS connections (
    id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    from_segment_id BIGINT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    to_segment_id BIGINT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    feasible BOOLEAN NOT NULL,
    gap_minutes INTEGER,
    cross_day BOOLEAN NOT NULL DEFAULT FALSE,
    detail TEXT,
    knock_on BOOLEAN NOT NULL DEFAULT FALSE,   -- 受上游变更连带影响
    knock_on_detail TEXT
);

-- 影响关系（数据变更 -> 受影响段/接驳），供追溯
CREATE TABLE IF NOT EXISTS impact_edges (
    id BIGSERIAL PRIMARY KEY,
    source_version_id BIGINT NOT NULL REFERENCES source_versions(id),
    change_type TEXT NOT NULL,                 -- ferry_cancel / closure_added / place_coord / tide_missing ...
    target_segment_id BIGINT REFERENCES segments(id) ON DELETE CASCADE,
    target_connection_id BIGINT REFERENCES connections(id) ON DELETE CASCADE,
    effect TEXT NOT NULL,                      -- direct / knock_on
    detail TEXT
);

-- 替代候选
CREATE TABLE IF NOT EXISTS alternatives (
    id BIGSERIAL PRIMARY KEY,
    segment_id BIGINT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    record_ref JSONB,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    chosen BOOLEAN NOT NULL DEFAULT FALSE
);

-- 离线包登记：冻结日期、覆盖数据版本、更新缺口
CREATE TABLE IF NOT EXISTS offline_bundles (
    id BIGSERIAL PRIMARY KEY,
    frozen_at TIMESTAMPTZ NOT NULL,
    covers_date DATE NOT NULL,
    version_ids BIGINT[] NOT NULL,
    gap_sources TEXT[] NOT NULL DEFAULT '{}',
    bundle_path TEXT,
    checksum_sha256 TEXT,
    note TEXT
);
