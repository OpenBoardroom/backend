-- migrate:up

-- A filing's CIK must be one of its own company's CIKs (spec §4, update
-- 2026-11-21). cik alone is already unique, but a composite foreign key needs
-- a unique constraint on exactly the referenced columns.

alter table company_ciks
    add constraint company_ciks_company_id_cik_key unique (company_id, cik);

alter table filings
    add constraint filings_company_id_cik_fkey foreign key (company_id, cik)
        references company_ciks (company_id, cik) on delete restrict;

-- migrate:down

alter table filings drop constraint filings_company_id_cik_fkey;
alter table company_ciks drop constraint company_ciks_company_id_cik_key;
