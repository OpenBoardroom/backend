-- migrate:up

-- Adds filer_cik (spec §8.1, update 2026-11-21): the CIK the proxy filing was
-- filed under, next to the company's current cik. An addition under ADR-0003
-- §6. The view is recreated rather than replaced so filer_cik can sit beside
-- cik; it stays a security_barrier and keeps its grant (see 20261121000003).

drop view published_ballot_items;

-- Facts publish when ballot_items.published_at is set. Pending changes never
-- alter the values shown; they only raise has_pending_change.
create view published_ballot_items with (security_barrier) as
select
    bi.id,
    bi.meeting_id,
    c.cik,
    -- The CIK this proxy was filed under; differs from cik after a change of filer.
    f.cik as filer_cik,
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

grant select on published_ballot_items to web_ro;

-- migrate:down

drop view published_ballot_items;

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

grant select on published_ballot_items to web_ro;
