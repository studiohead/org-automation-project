-- PLAIN ENGLISH: Ready-made questions you can ask the database ("reports").
-- Run them with:  sqlite3 org_dev.sqlite3 < sql/reporting_queries.sql
-- They show the kind of operational reporting a Technology & Operations team would want.

-- 1) How many documents landed in each queue each day? (volume + exception rate)
SELECT substr(created_at, 1, 10) AS day, kind, COUNT(*) AS items
FROM approvals
GROUP BY day, kind
ORDER BY day DESC, kind;

-- 2) How long do humans take to decide? (turnaround, in minutes)
SELECT decided_by,
       COUNT(*) AS decisions,
       ROUND(AVG((julianday(decided_at) - julianday(created_at)) * 24 * 60), 1) AS avg_minutes
FROM approvals
WHERE status IN ('approved', 'rejected')
GROUP BY decided_by;

-- 3) Which problems come up most often? (tells us where to improve extraction or forms)
SELECT json_each.value AS issue_code, COUNT(*) AS times_seen
FROM audit_events, json_each(json_extract(audit_events.details_json, '$.issue_codes'))
WHERE event_type = 'document_processed'
GROUP BY issue_code
ORDER BY times_seen DESC;

-- 4) Anything waiting more than a day? (a simple "stuck work" alert)
SELECT approval_id, document_id, kind, created_at
FROM approvals
WHERE status = 'pending' AND created_at < datetime('now', '-1 day');

-- 5) Full story of one document, oldest first (what an auditor would ask for)
--    Replace 'demo-001' with a real document id.
SELECT timestamp, event_type, actor, outcome
FROM audit_events
WHERE document_id = 'demo-001'
ORDER BY seq;
