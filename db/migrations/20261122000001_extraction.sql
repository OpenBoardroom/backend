-- migrate:up

-- Vocabularies and columns for the extraction stage. Like every vocabulary
-- here (ADR-0003), they are text + check, not enum types.

-- A proxy filing describes one meeting; re-running extraction updates it.
alter table meetings
    add constraint meetings_proxy_filing_id_key unique (proxy_filing_id);

-- The unique constraint's index makes the plain one redundant.
drop index meetings_proxy_filing_id_idx;

alter table extraction_runs
    add constraint extraction_runs_kind_check check (kind in ('regex', 'llm', 'reconcile')),
    add constraint extraction_runs_status_check check (
        status in ('submitted', 'succeeded', 'refused', 'failed')
    ),
    add column batch_id text,
    add column detail text;

-- Filings that are not a regular annual-meeting proxy leave the pipeline here.
alter table filings drop constraint filings_stage_check;
alter table filings
    add constraint filings_stage_check check (
        stage in (
            'discovered', 'fetched', 'extracted', 'checked', 'in_review', 'published',
            'reconciled', 'out_of_scope'
        )
    );

-- migrate:down

alter table filings drop constraint filings_stage_check;
alter table filings
    add constraint filings_stage_check check (
        stage in (
            'discovered', 'fetched', 'extracted', 'checked', 'in_review', 'published', 'reconciled'
        )
    );

alter table extraction_runs
    drop column detail,
    drop column batch_id,
    drop constraint extraction_runs_status_check,
    drop constraint extraction_runs_kind_check;

create index meetings_proxy_filing_id_idx on meetings (proxy_filing_id);
alter table meetings drop constraint meetings_proxy_filing_id_key;
