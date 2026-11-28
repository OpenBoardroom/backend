-- migrate:up

-- Supplements (DEFA14A) and pending changes (spec §7.1 and its update of
-- 2026-11-25; Plan 3b). An added item has no item yet, so a change names
-- its meeting always and its item for every other kind of change.

alter table pending_changes add column meeting_id bigint references meetings on delete restrict;
update pending_changes pc set meeting_id = bi.meeting_id
    from ballot_items bi where bi.id = pc.ballot_item_id;
alter table pending_changes alter column meeting_id set not null;
alter table pending_changes alter column ballot_item_id drop not null;
alter table pending_changes add constraint pending_changes_target_check
    check ((ballot_item_id is not null) <> (field = 'item'));
create index pending_changes_meeting_id_idx on pending_changes (meeting_id);

-- Null until decided. An applied change with no review was applied automatically.
alter table pending_changes add column decided_at timestamptz;
alter table pending_changes add column review_id bigint references reviews on delete set null;

alter table extraction_runs drop constraint extraction_runs_kind_check;
alter table extraction_runs add constraint extraction_runs_kind_check
    check (kind in ('regex', 'llm', 'reconcile', 'texts', 'supplement'));

alter table reviews drop constraint reviews_decision_check;
alter table reviews add constraint reviews_decision_check check (
    decision in (
        'approve', 'edit', 'reject', 'fix', 'clear', 'requeue', 'apply_change', 'reject_change'
    )
);

-- migrate:down

-- Fails while a 'supplement' run, a change decision, or an added-item change
-- exists; delete or resolve those first.
alter table reviews drop constraint reviews_decision_check;
alter table reviews add constraint reviews_decision_check check (
    decision in ('approve', 'edit', 'reject', 'fix', 'clear', 'requeue')
);

alter table extraction_runs drop constraint extraction_runs_kind_check;
alter table extraction_runs add constraint extraction_runs_kind_check
    check (kind in ('regex', 'llm', 'reconcile', 'texts'));

alter table pending_changes drop column review_id;
alter table pending_changes drop column decided_at;
drop index pending_changes_meeting_id_idx;
alter table pending_changes drop constraint pending_changes_target_check;
alter table pending_changes alter column ballot_item_id set not null;
alter table pending_changes drop column meeting_id;
