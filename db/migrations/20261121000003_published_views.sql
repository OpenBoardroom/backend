-- migrate:up

-- The frontend contract (ADR-0003): web_ro reads only these views, never the
-- tables. Each view applies the publication rule of spec §7.1 in one place.
-- They are plain views with security_invoker off (the default), so they run
-- with the owner's privileges and web_ro needs select on the views alone.
-- Each is a security_barrier: web_ro can create temp functions, and without
-- the barrier a cheap one in a query's WHERE clause is planned below the
-- view's filter and sees the hidden rows.
-- Columns keep their table names; removing or renaming one is a breaking
-- change (ADR-0003 §6) and fails tests/contract/test_contract.py.

-- Facts publish when ballot_items.published_at is set. Pending changes never
-- alter the values shown; they only raise has_pending_change.
create view published_ballot_items with (security_barrier) as
select
    bi.id,
    bi.meeting_id,
    c.cik,
    c.ticker,
    c.name as company_name,
    m.meeting_date,
    m.date_quote,
    f.source_url,
    bi.number,
    bi.title,
    bi.type,
    bi.category,
    bi.effect,
    bi.lead_filer,
    bi.representative,
    bi.co_filers,
    -- Nominee names in ballot order; empty for items that aren't elections.
    array(
        select n.name from nominees n where n.ballot_item_id = bi.id order by n.position
    ) as nominees,
    bi.board_recommendation,
    bi.recommendation_quote,
    bi.vote_base,
    bi.vote_threshold_pct,
    bi.abstentions_count_against,
    bi.vote_standard_text,
    bi.vote_standard_quote,
    bi.request,
    bi.status,
    bi.source_quote,
    bi.published_at,
    exists (
        select 1 from pending_changes pc
        where pc.ballot_item_id = bi.id and pc.status = 'pending'
    ) as has_pending_change
from ballot_items bi
join meetings m on m.id = bi.meeting_id
join companies c on c.id = m.company_id
join filings f on f.id = m.proxy_filing_id
where bi.published_at is not null;

create view published_meetings with (security_barrier) as
select
    m.id,
    m.company_id,
    c.cik,
    c.ticker,
    c.name as company_name,
    m.kind,
    m.meeting_date,
    m.meeting_time,
    m.format,
    m.record_date,
    m.shares_outstanding,
    m.date_quote,
    f.source_url
from meetings m
join companies c on c.id = m.company_id
join filings f on f.id = m.proxy_filing_id
where exists (
    select 1 from ballot_items bi
    where bi.meeting_id = m.id and bi.published_at is not null
);

-- Texts publish only after review: the latest approved version per item and
-- kind. A newer pending or rejected version leaves the approved one showing.
create view published_item_texts with (security_barrier) as
select distinct on (t.ballot_item_id, t.kind)
    t.ballot_item_id,
    t.kind,
    t.version,
    t.body,
    t.origin
from item_texts t
join ballot_items bi on bi.id = t.ballot_item_id
where t.review_status = 'approved' and bi.published_at is not null
order by t.ballot_item_id, t.kind, t.version desc;

-- Results publish once reconciliation passes (spec §6.5) and set published_at.
create view published_vote_results with (security_barrier) as
select
    vr.id,
    vr.ballot_item_id,
    vr.nominee_id,
    n.name as nominee_name,
    n.position as nominee_position,
    vr.votes_for,
    vr.votes_against,
    vr.abstentions,
    vr.broker_non_votes,
    vr.votes_one_year,
    vr.votes_two_years,
    vr.votes_three_years,
    vr.outcome,
    f.source_url,
    vr.published_at
from vote_results vr
join ballot_items bi on bi.id = vr.ballot_item_id
join filings f on f.id = vr.source_filing_id
left join nominees n on n.id = vr.nominee_id
where vr.published_at is not null and bi.published_at is not null;

-- Every correction to a published item is public (spec §7.3). A DEFA14A
-- change can write one before the item publishes; that stays hidden until it
-- does (ADR-0003 §2). Reviewer identity stays private.
create view public_corrections with (security_barrier) as
select
    co.id,
    co.ballot_item_id,
    bi.number,
    bi.title,
    c.ticker,
    m.meeting_date,
    co.field,
    co.before,
    co.after,
    co.reason,
    co.corrected_at
from corrections co
join ballot_items bi on bi.id = co.ballot_item_id
join meetings m on m.id = bi.meeting_id
join companies c on c.id = m.company_id
where bi.published_at is not null;

grant select on
    published_meetings, published_ballot_items, published_item_texts,
    published_vote_results, public_corrections
to web_ro;

-- migrate:down

drop view public_corrections;
drop view published_vote_results;
drop view published_item_texts;
drop view published_meetings;
drop view published_ballot_items;
