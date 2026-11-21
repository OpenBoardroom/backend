-- migrate:up

-- Review, change, and results tables (spec §7, §8.1). Vocabularies are
-- text + check, not enum types (ADR-0003).

create table pending_changes (
    id bigint generated always as identity primary key,
    ballot_item_id bigint not null references ballot_items on delete restrict,
    field text not null,
    proposed jsonb,
    source_quote text,
    extraction_run_id bigint not null references extraction_runs on delete restrict,
    reason text not null check (reason in ('rerun', 'supplement', 'reconciliation')),
    status text not null default 'pending' check (status in ('pending', 'applied', 'rejected'))
);

create index pending_changes_ballot_item_id_idx on pending_changes (ballot_item_id);

create table item_texts (
    id bigint generated always as identity primary key,
    ballot_item_id bigint not null references ballot_items on delete restrict,
    kind text not null check (kind in ('summary', 'proponent_argument', 'board_argument')),
    version integer not null check (version > 0),
    -- Null when there is no such text (e.g. no opposing argument on a routine item).
    body text,
    origin text not null check (origin in ('model', 'reviewer')),
    -- Null for reviewer-written texts.
    extraction_run_id bigint references extraction_runs on delete restrict,
    review_status text not null default 'pending' check (
        review_status in ('pending', 'approved', 'rejected', 'needs_manual')
    ),
    unique (ballot_item_id, kind, version)
);

create table reviews (
    id bigint generated always as identity primary key,
    filing_id bigint not null references filings on delete restrict,
    item_text_id bigint references item_texts on delete restrict,
    decision text not null,
    reason text,
    reviewer text not null,
    before jsonb,
    after jsonb,
    decided_at timestamptz not null default now()
);

create index reviews_filing_id_idx on reviews (filing_id);

create table corrections (
    id bigint generated always as identity primary key,
    ballot_item_id bigint not null references ballot_items on delete restrict,
    field text not null,
    before jsonb,
    after jsonb,
    reason text not null,
    -- Null for changes applied without review (verified DEFA14A withdrawals and
    -- recommendation changes, spec §7.1).
    review_id bigint references reviews on delete restrict,
    corrected_at timestamptz not null default now()
);

create index corrections_ballot_item_id_idx on corrections (ballot_item_id);

create table vote_results (
    id bigint generated always as identity primary key,
    ballot_item_id bigint not null references ballot_items on delete restrict,
    -- Set for per-nominee rows of a director election; null for the item row.
    nominee_id bigint references nominees on delete restrict,
    votes_for bigint check (votes_for >= 0),
    votes_against bigint check (votes_against >= 0),
    abstentions bigint check (abstentions >= 0),
    broker_non_votes bigint check (broker_non_votes >= 0),
    votes_one_year bigint check (votes_one_year >= 0),
    votes_two_years bigint check (votes_two_years >= 0),
    votes_three_years bigint check (votes_three_years >= 0),
    outcome text,
    source_filing_id bigint not null references filings on delete restrict,
    -- Set only after reconciliation passes (spec §6.5).
    published_at timestamptz,
    unique nulls not distinct (ballot_item_id, nominee_id)
);

grant select, insert, update, delete on
    pending_changes, item_texts, reviews, corrections, vote_results
to pipeline_rw;
grant usage on all sequences in schema public to pipeline_rw;

-- migrate:down

drop table vote_results;
drop table corrections;
drop table reviews;
drop table item_texts;
drop table pending_changes;
