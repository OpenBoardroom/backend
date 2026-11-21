"""Contract test: the published views keep the columns the frontend relies on (ADR-0003 §6).

``views.json`` lists, per view, the columns ``frontend`` may use. Adding a column or
view is backward compatible; removing or renaming one fails here and needs a spec or ADR.
"""

import json
from pathlib import Path

import psycopg

CONTRACT = Path(__file__).resolve().parent / "views.json"


def _contract() -> dict[str, list[str]]:
    data: dict[str, list[str]] = json.loads(CONTRACT.read_text())
    return data


def test_contract_lists_the_adr_views() -> None:
    assert sorted(_contract()) == [
        "public_corrections",
        "published_ballot_items",
        "published_item_texts",
        "published_meetings",
        "published_vote_results",
    ]


def test_views_keep_contract_columns(pg_url: str) -> None:
    with psycopg.connect(pg_url) as connection:
        for view, columns in _contract().items():
            kind = connection.execute(
                "select table_type from information_schema.tables "
                "where table_schema = 'public' and table_name = %s",
                (view,),
            ).fetchone()
            assert kind == ("VIEW",), f"{view} is not a view"

            rows = connection.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = %s",
                (view,),
            ).fetchall()
            missing = set(columns) - {row[0] for row in rows}
            assert not missing, f"{view} lost contract columns: {sorted(missing)}"
