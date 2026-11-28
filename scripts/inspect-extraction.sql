-- Inspect the most recent LLM extraction run and everything it wrote.
-- Run from backend/:
--   docker compose exec -T postgres psql -U obr_owner -d openboardroom < scripts/inspect-extraction.sql
\pset pager off
\x auto

-- The run being inspected: the latest llm run.
select r.id as run_id into temp run from extraction_runs r where r.kind = 'llm' order by r.id desc limit 1;

\echo '== 1. Filing and run =='
select c.name as company, f.accession, f.form, f.filed_at::date, f.stage, f.source_url,
       r.status, r.model, r.effort, r.prompt_version, r.input_tokens, r.output_tokens,
       r.finished_at - r.started_at as duration, r.detail
from extraction_runs r
join filings f on f.id = r.filing_id
join companies c on c.id = f.company_id
where r.id = (select run_id from run);

\echo '== 2. The four checks =='
select check_name, passed, detail from check_results
where extraction_run_id = (select run_id from run) order by id;

\echo '== 3. Meeting =='
select m.kind, m.meeting_date, m.meeting_time, m.format, m.record_date, m.shares_outstanding, m.date_quote
from meetings m join extraction_runs r on r.filing_id = m.proxy_filing_id
where r.id = (select run_id from run);

\echo '== 4. Ballot items (facts layer) =='
select b.number, b.title, b.type, b.category, b.effect, b.board_recommendation,
       b.vote_base, b.vote_threshold_pct, b.abstentions_count_against, b.lead_filer, b.status
from ballot_items b where b.extraction_run_id = (select run_id from run)
order by b.number;

\echo '== 5. Quotes backing each item =='
select b.number, b.recommendation_quote, b.vote_standard_quote, b.source_quote
from ballot_items b where b.extraction_run_id = (select run_id from run)
order by b.number;

\echo '== 6. Director nominees =='
select b.number, n.position, n.name
from nominees n join ballot_items b on b.id = n.ballot_item_id
where b.extraction_run_id = (select run_id from run)
order by b.number, n.position;

\echo '== 7. Draft texts (need review before publication) =='
select b.number, t.kind, t.review_status, t.body
from item_texts t join ballot_items b on b.id = t.ballot_item_id
where b.extraction_run_id = (select run_id from run)
order by b.number, t.kind;
