-- Example queries for Postgres.

-- 1. Recent statistics with their audience and evidence.
SELECT m.published_at, s.name AS source, f.topic, f.indicator_type,
       f.ir_applicability,
       f.value, f.audience, f.percent_base, f.study_period,
       f.summary, f.evidence, m.url
FROM findings f
JOIN materials m ON m.id = f.material_id
JOIN sources s ON s.id = m.source_id
WHERE m.published_at > now() - interval '30 days'
  AND (m.processed_prompt_version IS NULL
       OR f.prompt_version = m.processed_prompt_version)
ORDER BY m.published_at DESC;

-- 2. Findings grouped by indicator type. Market metrics are deliberately
-- separated from audience shares.
SELECT indicator_type, ir_applicability, count(*)
FROM findings
GROUP BY indicator_type, ir_applicability
ORDER BY count(*) DESC;

-- 3. Runs of every block, newest first.
SELECT id, service, status, items, note, started_at, finished_at
FROM runs ORDER BY id DESC LIMIT 20;

-- 4. Controlled prompt upgrade: after changing PROMPT_VERSION, enable
-- REPROCESS_OLD=1 for the worker. No status reset is required.
