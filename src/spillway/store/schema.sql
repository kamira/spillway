-- spillway schema v1:與 docs/spec.md 2.3 逐欄一致(差異走 CHG)。
-- 由 store/db.py 的 migration 1 套用;本檔只建表與索引,不寫入任何資料列(spec 2)。

CREATE TABLE deployments (
  id             TEXT PRIMARY KEY,
  model_name     TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('cli','api','local')),
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  params         TEXT NOT NULL DEFAULT '{}',   -- JSON
  credential_ref TEXT,
  capabilities   TEXT NOT NULL DEFAULT '{}',   -- JSON {ctx, tools, vision, efforts[]}
  enabled        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE deployment_tags (
  deployment_id TEXT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
  tag           TEXT NOT NULL,
  PRIMARY KEY (deployment_id, tag)
);

CREATE TABLE budgets (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  type            TEXT NOT NULL CHECK (type IN ('usd','credit_pool','none')),
  limit_amount    REAL,
  window          TEXT NOT NULL DEFAULT 'monthly'
                  CHECK (window IN ('monthly','weekly','daily','none')),
  reset_anchor    INTEGER,          -- monthly 的重置日，1–28
  exhausted_until TEXT              -- 上游回報用完或限流時設定
);

CREATE TABLE groups (
  name     TEXT PRIMARY KEY,
  selector TEXT NOT NULL            -- JSON {all[], any[], none[]}
);

CREATE TABLE routes (
  name   TEXT PRIMARY KEY,
  target TEXT NOT NULL,             -- JSON {group} | {tags:{all,any,none}}
  mode   TEXT NOT NULL DEFAULT 'chat' CHECK (mode IN ('chat','task')),
  tags   TEXT NOT NULL DEFAULT '[]',
  record_context  INTEGER,            -- NULL = 沿用全域；1 開；0 關
  budget_precheck INTEGER NOT NULL DEFAULT 0  -- 1 = 啟用預估額度檢查（4.2.5）
);

CREATE TABLE route_targets (
  route_name    TEXT NOT NULL REFERENCES routes(name) ON DELETE CASCADE,
  deployment_id TEXT NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
  slot          INTEGER NOT NULL,   -- 使用者拖曳決定的優先級，小者先
  position      INTEGER NOT NULL,   -- slot 內的顯示與 local 嘗試順序
  effort        TEXT CHECK (effort IN ('none','low','medium','high','max')),
  weight        TEXT NOT NULL DEFAULT 'medium'
                CHECK (weight IN ('high','medium','low')),
  PRIMARY KEY (route_name, deployment_id)
);

CREATE TABLE usage_ledger (
  id             INTEGER PRIMARY KEY,
  ts             TEXT NOT NULL,
  route_name     TEXT,
  deployment_id  TEXT,
  effort         TEXT,
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  cost_usd       REAL,
  status         TEXT NOT NULL,     -- ok | error | quota_exhausted | rate_limited | skipped_precheck
  slot           INTEGER,
  fallback_depth INTEGER NOT NULL DEFAULT 0,
  latency_ms     INTEGER,
  est_tokens_in      INTEGER,
  est_tokens_out_p50 INTEGER,
  est_tokens_out_p90 INTEGER,
  est_cost_usd_p90   REAL,
  error          TEXT
);
CREATE INDEX ix_ledger_dep_ts ON usage_ledger(deployment_id, ts);

CREATE TABLE usage_payloads (
  ledger_id     INTEGER PRIMARY KEY REFERENCES usage_ledger(id) ON DELETE CASCADE,
  ts            TEXT NOT NULL,
  request_json  TEXT NOT NULL,      -- 實際送出的 messages；CLI 為轉換後的 prompt
  response_text TEXT,
  bytes         INTEGER NOT NULL,
  truncated     INTEGER NOT NULL DEFAULT 0,
  redacted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_payloads_ts ON usage_payloads(ts);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
