-- migrate:up

-- Core pipeline tables (spec §8.1): companies, filings, meetings, runs, and
-- ballot-item facts. Vocabularies are text + check, not enum types (ADR-0003).
-- Facts are nullable on purpose: a value the filing doesn't state is stored as
-- null, never guessed, and the §6.4 checks fail on required nulls (spec §8.2).

create table companies (
    id bigint generated always as identity primary key,
    cik text not null unique check (cik ~ '^[0-9]{10}$'),
    ticker text not null,
    name text not null
);

create table index_memberships (
    id bigint generated always as identity primary key,
    company_id bigint not null references companies on delete restrict,
    index_name text not null check (index_name in ('sp100', 'sp500')),
    start_date date not null,
    end_date date,
    unique (company_id, index_name, start_date),
    check (end_date is null or end_date >= start_date)
);

comment on column index_memberships.index_name is
    'Spec §8.1 column `index`, renamed because INDEX is an SQL keyword.';

create table filings (
    id bigint generated always as identity primary key,
    accession text not null unique check (accession ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'),
    company_id bigint not null references companies on delete restrict,
    form text not null check (form in ('DEF 14A', 'DEFA14A', '8-K')),
    filed_at timestamptz not null,
    source_url text not null,
    primary_document text,
    raw_sha256 text check (raw_sha256 ~ '^[0-9a-f]{64}$'),
    text_sha256 text check (text_sha256 ~ '^[0-9a-f]{64}$'),
    stage text not null default 'discovered' check (
        stage in (
            'discovered', 'fetched', 'extracted', 'checked', 'in_review', 'published', 'reconciled'
        )
    ),
    stage_updated_at timestamptz not null default now()
);

create index filings_company_id_idx on filings (company_id);
create index filings_stage_idx on filings (stage);

create table meetings (
    id bigint generated always as identity primary key,
    company_id bigint not null references companies on delete restrict,
    proxy_filing_id bigint not null references filings on delete restrict,
    kind text check (kind in ('annual', 'special')),
    meeting_date date,
    -- The filing's own wording (e.g. '9:00 a.m. Pacific Time'): a bare time
    -- type would drop the time zone the filing states.
    meeting_time text,
    format text check (format in ('virtual', 'in_person', 'hybrid')),
    record_date date,
    shares_outstanding bigint check (shares_outstanding >= 0),
    date_quote text
);

create index meetings_company_id_idx on meetings (company_id);
create index meetings_proxy_filing_id_idx on meetings (proxy_filing_id);

create table extraction_runs (
    id bigint generated always as identity primary key,
    filing_id bigint not null references filings on delete restrict,
    kind text not null,
    model text,
    effort text,
    prompt_version text,
    schema_version text,
    input_tokens bigint check (input_tokens >= 0),
    output_tokens bigint check (output_tokens >= 0),
    output jsonb,
    status text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz
);

create index extraction_runs_filing_id_idx on extraction_runs (filing_id);

create table check_results (
    id bigint generated always as identity primary key,
    extraction_run_id bigint not null references extraction_runs on delete restrict,
    check_name text not null,
    passed boolean not null,
    detail text
);

comment on column check_results.check_name is
    'Spec §8.1 column `check`, renamed because CHECK is a reserved SQL word.';

create index check_results_extraction_run_id_idx on check_results (extraction_run_id);

create table ballot_items (
    id bigint generated always as identity primary key,
    meeting_id bigint not null references meetings on delete restrict,
    number text not null,
    title text,
    type text check (type in ('management', 'shareholder')),
    category text check (
        category in (
            'director_election', 'say_on_pay', 'say_on_pay_frequency', 'auditor_ratification',
            'equity_plan', 'charter_bylaw_amendment', 'shareholder_proposal', 'other'
        )
    ),
    effect text check (effect in ('binding', 'advisory', 'precatory')),
    lead_filer text,
    representative text,
    co_filers text[] not null default '{}',
    board_recommendation text check (
        board_recommendation in (
            'for', 'against', 'abstain', 'none', 'for_each_nominee',
            'one_year', 'two_years', 'three_years'
        )
    ),
    recommendation_quote text,
    vote_base text check (
        vote_base in (
            'votes_cast', 'shares_present_and_voting', 'shares_present', 'shares_outstanding',
            'plurality', 'other'
        )
    ),
    vote_threshold_pct numeric check (vote_threshold_pct > 0 and vote_threshold_pct <= 100),
    abstentions_count_against boolean,
    vote_standard_text text,
    vote_standard_quote text,
    request text,
    status text not null default 'active' check (status in ('active', 'withdrawn')),
    source_quote text,
    extraction_run_id bigint not null references extraction_runs on delete restrict,
    published_at timestamptz,
    unique (meeting_id, number)
);

create index ballot_items_extraction_run_id_idx on ballot_items (extraction_run_id);

create table nominees (
    id bigint generated always as identity primary key,
    ballot_item_id bigint not null references ballot_items on delete restrict,
    name text not null,
    -- 1-based order on the ballot, used to match per-nominee 8-K results.
    position integer not null check (position > 0),
    unique (ballot_item_id, position)
);

grant select, insert, update, delete on
    companies, index_memberships, filings, meetings, extraction_runs, check_results,
    ballot_items, nominees
to pipeline_rw;
grant usage on all sequences in schema public to pipeline_rw;

-- migrate:down

drop table nominees;
drop table ballot_items;
drop table check_results;
drop table extraction_runs;
drop table meetings;
drop table filings;
drop table index_memberships;
drop table companies;
