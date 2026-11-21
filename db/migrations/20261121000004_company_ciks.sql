-- migrate:up

-- A company is the unit, not an EDGAR filer ID (spec §4, update 2026-11-21).
-- A company can file under several CIKs over time, each in use for a date
-- range; companies.cik stays the current one. Each filing keeps the CIK it
-- was filed under, so EDGAR fidelity lives on filings and continuity on companies.

create table company_ciks (
    id bigint generated always as identity primary key,
    company_id bigint not null references companies on delete restrict,
    -- Unique across companies: a CIK identifies one filer.
    cik text not null unique check (cik ~ '^[0-9]{10}$'),
    valid_from date not null,
    valid_to date,
    note text,
    check (valid_to is null or valid_to >= valid_from)
);

create index company_ciks_company_id_idx on company_ciks (company_id);

comment on column company_ciks.valid_from is
    'First day the company filed under this CIK. 1900-01-01 means the start is not recorded: '
    'in use since before coverage began.';
comment on column company_ciks.valid_to is
    'Last day the company filed under this CIK; null while it is the current one.';

-- Not null with no default: filings has no rows yet in any environment
-- (2026-11-21), so there is nothing to backfill.
alter table filings add column cik text not null check (cik ~ '^[0-9]{10}$');

comment on column filings.cik is
    'The CIK the filing was filed under, which may differ from companies.cik '
    '(the company''s current CIK).';

-- 2026 memberships are all dated 2026-01-01 from a holdings snapshot, so some
-- are misdated; public pages mark approximate memberships (spec §4).
alter table index_memberships
    add column dates_approximate boolean not null default false;

grant select, insert, update, delete on company_ciks to pipeline_rw;
grant usage on all sequences in schema public to pipeline_rw;

-- migrate:down

alter table index_memberships drop column dates_approximate;
alter table filings drop column cik;
drop table company_ciks;
