-- Run each read-only query before upgrading. Any returned row requires review.
-- uq_calendar_feeds_active
SELECT identity_id, department_id, count(*) AS duplicates
FROM calendar_feeds WHERE revoked IS FALSE
GROUP BY identity_id, department_id HAVING count(*) > 1;

-- uq_people_active_short_code
SELECT organisation_id, short_code, count(*) AS duplicates
FROM people WHERE active IS TRUE AND short_code <> ''
GROUP BY organisation_id, short_code HAVING count(*) > 1;

-- uq_jobs_live_validation
SELECT workspace_id, input_snapshot ->> 'validation_request_key' AS request_key, count(*) AS duplicates
FROM jobs
WHERE kind = 'validation' AND status IN ('queued', 'running')
  AND input_snapshot ->> 'validation_request_key' IS NOT NULL
GROUP BY workspace_id, input_snapshot ->> 'validation_request_key' HAVING count(*) > 1;

-- uq_jobs_live_generation
SELECT workspace_id, count(*) AS duplicates
FROM jobs WHERE kind = 'generation' AND status IN ('queued', 'running')
GROUP BY workspace_id HAVING count(*) > 1;
