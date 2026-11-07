
CREATE TABLE cooldowns (
  deployment_id   TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
  exhausted_until TEXT NOT NULL,    -- 上游回報用完或限流時設定
  reason          TEXT NOT NULL CHECK (reason IN ('quota_exhausted','rate_limited'))
);

INSERT INTO cooldowns (deployment_id, exhausted_until, reason)
SELECT b.deployment_id, b.exhausted_until,
       COALESCE((SELECT l.status FROM usage_ledger l
                 WHERE l.deployment_id = b.deployment_id
                   AND l.status IN ('quota_exhausted','rate_limited')
                 ORDER BY l.ts DESC, l.id DESC LIMIT 1), 'quota_exhausted')
FROM budgets b
WHERE b.exhausted_until IS NOT NULL;

ALTER TABLE budgets DROP COLUMN exhausted_until;
