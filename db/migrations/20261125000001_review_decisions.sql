-- migrate:up

-- Review decisions (spec §7.2; Plan 3). A fact fix names its item, and
-- requeueing a filing keeps earlier review rows: their links to deleted
-- texts and items become null.

alter table reviews add constraint reviews_decision_check check (
    decision in ('approve', 'edit', 'reject', 'fix', 'clear', 'requeue')
);

alter table reviews add column ballot_item_id bigint references ballot_items on delete set null;
create index reviews_ballot_item_id_idx on reviews (ballot_item_id);

alter table reviews drop constraint reviews_item_text_id_fkey;
alter table reviews add constraint reviews_item_text_id_fkey
    foreign key (item_text_id) references item_texts on delete set null;

-- Text-only regeneration runs after a rejection (spec §7.2).
alter table extraction_runs drop constraint extraction_runs_kind_check;
alter table extraction_runs add constraint extraction_runs_kind_check
    check (kind in ('regex', 'llm', 'reconcile', 'texts'));

-- migrate:down

-- Fails while a 'texts' run exists; delete those first.
alter table extraction_runs drop constraint extraction_runs_kind_check;
alter table extraction_runs add constraint extraction_runs_kind_check
    check (kind in ('regex', 'llm', 'reconcile'));

alter table reviews drop constraint reviews_item_text_id_fkey;
alter table reviews add constraint reviews_item_text_id_fkey
    foreign key (item_text_id) references item_texts on delete restrict;

drop index reviews_ballot_item_id_idx;
alter table reviews drop column ballot_item_id;
alter table reviews drop constraint reviews_decision_check;
