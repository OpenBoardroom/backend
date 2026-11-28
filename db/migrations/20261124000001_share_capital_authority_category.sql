-- migrate:up

-- Irish and UK companies vote every year on the board's authority over share
-- capital (spec §8.2, update 2026-11-23; finding F20).

alter table ballot_items drop constraint ballot_items_category_check;
alter table ballot_items add constraint ballot_items_category_check check (
    category in (
        'director_election', 'say_on_pay', 'say_on_pay_frequency', 'auditor_ratification',
        'equity_plan', 'charter_bylaw_amendment', 'share_capital_authority',
        'shareholder_proposal', 'other'
    )
);

-- migrate:down

-- Fails while any item uses the new category; recategorize those rows first.
alter table ballot_items drop constraint ballot_items_category_check;
alter table ballot_items add constraint ballot_items_category_check check (
    category in (
        'director_election', 'say_on_pay', 'say_on_pay_frequency', 'auditor_ratification',
        'equity_plan', 'charter_bylaw_amendment', 'shareholder_proposal', 'other'
    )
);
