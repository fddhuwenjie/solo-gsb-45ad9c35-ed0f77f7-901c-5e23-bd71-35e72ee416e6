-- 陶瓷合窑装载确认台：数据库结构
-- 已确认记录（confirmed_loads / confirmed_load_items）只增不改，无 UPDATE/DELETE 路径。

PRAGMA journal_mode = WAL;

-- 待烧批次：登记编号与允许的峰温、保温分钟闭区间
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL UNIQUE,
    peak_min     REAL NOT NULL,
    peak_max     REAL NOT NULL,
    hold_min     INTEGER NOT NULL,
    hold_max     INTEGER NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (peak_min <= peak_max),
    CHECK (hold_min <= hold_max),
    CHECK (hold_min >= 0 AND hold_max >= 0)
);

-- 窑炉
CREATE TABLE IF NOT EXISTS kilns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL
);

-- 固定编号窑位（每台窑的窑位编号固定）
CREATE TABLE IF NOT EXISTS kiln_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kiln_id     INTEGER NOT NULL REFERENCES kilns(id),
    pos_no      INTEGER NOT NULL,
    UNIQUE (kiln_id, pos_no)
);

-- 装窑草案（一台草案对应一台窑）
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kiln_id      INTEGER NOT NULL REFERENCES kilns(id),
    version      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 草案占位：一批占一位
CREATE TABLE IF NOT EXISTS draft_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    pos_no       INTEGER NOT NULL,
    UNIQUE (draft_id, batch_id),
    UNIQUE (draft_id, pos_no)
);

-- 已确认窑次（不可变装载清单头）
CREATE TABLE IF NOT EXISTS confirmed_loads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id       INTEGER NOT NULL UNIQUE REFERENCES drafts(id),
    kiln_id        INTEGER NOT NULL REFERENCES kilns(id),
    name           TEXT NOT NULL,
    peak_min       REAL NOT NULL,
    peak_max       REAL NOT NULL,
    hold_min       INTEGER NOT NULL,
    hold_max       INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 已确认装载清单明细（不可变快照）
-- UNIQUE(batch_id)：一个批次最多被一个已确认窑次使用；
--                   两个草案同时争用同一批次时，只有一个确认事务能提交。
CREATE TABLE IF NOT EXISTS confirmed_load_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id        INTEGER NOT NULL REFERENCES confirmed_loads(id),
    batch_id       INTEGER NOT NULL UNIQUE,
    batch_code     TEXT NOT NULL,
    pos_no         INTEGER NOT NULL,
    peak_min       REAL NOT NULL,
    peak_max       REAL NOT NULL,
    hold_min       INTEGER NOT NULL,
    hold_max       INTEGER NOT NULL,
    UNIQUE (load_id, pos_no)
);

-- 烧成时段安排：对已确认窑次安排带时区的开始/结束时间与拟用工艺参数。
-- UNIQUE(load_id)：同一窑次只能安排一次（调整只能修改原安排）；
-- start_at/end_at 统一存 UTC ISO8601 定长字符串（字典序即时间序），
-- 同一台窑的时间段不得重叠（首尾相接允许），由保存时事务内重查保证。
CREATE TABLE IF NOT EXISTS firing_schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id       INTEGER NOT NULL UNIQUE REFERENCES confirmed_loads(id),
    kiln_id       INTEGER NOT NULL REFERENCES kilns(id),
    start_at      TEXT NOT NULL,
    end_at        TEXT NOT NULL,
    peak_temp     REAL NOT NULL,
    hold_minutes  INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (start_at < end_at)
);

-- 烧成实绩：为已有烧成安排登记一次实测峰温与保温分钟。
-- UNIQUE(schedule_id)/UNIQUE(load_id)：每个安排（窑次）只能登记一次，
--   并发登记时由唯一约束兜底，只有一个事务能提交；
-- schedule_*：登记时页面读到的安排时段与拟用参数快照，服务端在
--   BEGIN IMMEDIATE 事务内与 firing_schedules 最新行比对，不一致即
--   拒绝（stale），提示刷新且不生成实绩；
-- actual_peak_temp：非负数字，actual_hold_minutes：非负整数分钟；
--   超出工艺区间的实测值仍如实保存，逐批判定在查询时按
--   confirmed_load_items 的不可变闭区间快照现算（批次范围后改不影响判定）；
-- 登记成功后 firing_schedules 对应行不得再修改（由 PUT 路由检查本表）。
CREATE TABLE IF NOT EXISTS firing_records (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id            INTEGER NOT NULL UNIQUE REFERENCES firing_schedules(id),
    load_id                INTEGER NOT NULL UNIQUE REFERENCES confirmed_loads(id),
    schedule_start_at      TEXT NOT NULL,
    schedule_end_at        TEXT NOT NULL,
    schedule_peak_temp     REAL NOT NULL,
    schedule_hold_minutes  INTEGER NOT NULL,
    actual_peak_temp       REAL NOT NULL,
    actual_hold_minutes    INTEGER NOT NULL,
    registered_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (actual_peak_temp >= 0),
    CHECK (actual_hold_minutes >= 0)
);
