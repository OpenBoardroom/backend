"""Tests for rendering a filing for review."""

from openboardroom.review import render
from openboardroom.review.store import FilingView, ItemView, TextView

URL = "https://www.sec.gov/Archives/edgar/data/1/x.htm"


def _proposal(proponent: str, board: str) -> ItemView:
    return ItemView(
        id=7,
        number="2a",
        fields={
            "title": "Report on Water Use", "type": "shareholder",
            "category": "shareholder_proposal", "effect": "precatory", "lead_filer": "A Fund",
            "representative": "B Advisors", "co_filers": ["B Trust"],
            "board_recommendation": "against",
            "recommendation_quote": "The Board recommends a vote AGAINST this proposal",
            "vote_base": "votes_cast", "vote_threshold_pct": 50,
            "abstentions_count_against": False, "vote_standard_text": "majority of votes cast",
            "vote_standard_quote": None, "request": "a report on water use", "status": "active",
            "source_quote": "Shareholders request that the Board publish a report on water use",
        },
        nominees=[],
        texts=[
            TextView(21, "summary", 1, "A request for a report.", "model", "pending"),
            TextView(22, "proponent_argument", 1, proponent, "model", "pending"),
            TextView(23, "board_argument", 2, board, "reviewer", "approved"),
        ],
        published=False,
    )  # fmt: skip


def _view(item: ItemView) -> FilingView:
    return FilingView(
        filing_id=1,
        accession="0000320193-26-000001",
        ticker="AAPL",
        stage="in_review",
        source_url=URL,
        meeting={
            "kind": "annual", "meeting_date": "2026-03-01", "meeting_time": "9:00 a.m.",
            "format": "virtual", "record_date": "2026-01-02", "shares_outstanding": 1000,
            "date_quote": "held on March 1, 2026",
        },
        items=[item],
        failed_checks=[("count", "6 proposal numbers for 5 candidates")],
    )  # fmt: skip


def test_render_shows_failed_checks_quotes_and_links() -> None:
    text = render.filing(_view(_proposal("The proponent argues x.", "The board argues y.")))

    for expected in (
        "AAPL 0000320193-26-000001 [in_review]",
        URL,
        "FAILED count: 6 proposal numbers for 5 candidates",
        "annual meeting 2026-03-01 9:00 a.m., virtual; record date 2026-01-02; 1000 shares",
        "Item 2a (id 7): Report on Water Use",
        "shareholder · shareholder_proposal · precatory · board: against",
        "lead filer: A Fund · representative: B Advisors · co-filers: B Trust",
        "request: a report on water use",
        "vote: votes_cast, 50%, abstentions count against: False",
        'source: "Shareholders request that the Board publish a report on water use"',
        f"{URL}#:~:text=Shareholders%20request%20that%20the,report%20on%20water%20use",
        "vote standard: (none)",
        "[21] summary v1 model pending: A request for a report.",
        "[23] board_argument v2 reviewer approved: The board argues y.",
    ):
        assert expected in text, expected


def test_quote_link_encodes_text_fragment() -> None:
    link = render.quote_link(URL, "the Board’s view, in short")
    assert link == f"{URL}#:~:text=the%20Board%E2%80%99s%20view%2C%20in%20short"
    assert render.quote_link(URL, " ") == URL


def test_render_flags_uneven_arguments() -> None:
    def words(n: int) -> str:
        return " ".join(["word"] * n)

    uneven = render.filing(_view(_proposal(words(40), words(60))))
    even = render.filing(_view(_proposal(words(40), words(48))))

    assert "arguments: 40 and 60 words UNEVEN" in uneven
    assert "arguments: 40 and 48 words" in even and "UNEVEN" not in even


def test_render_filing_without_meeting_or_items() -> None:
    view = FilingView(1, "0000320193-26-000001", "AAPL", "in_review", URL, None, [], [])
    text = render.filing(view)
    assert "no meeting" in text and "no items" in text


def test_render_unstated_values_read_plainly() -> None:
    # Dry run on Berkshire 2026: a plurality vote has no threshold, and the
    # share count can be unstated (finding F18); neither should print "None".
    item = _proposal("a", "b")
    item.fields.update(vote_base="plurality", vote_threshold_pct=None,
                       abstentions_count_against=None)  # fmt: skip
    view = _view(item)
    assert view.meeting is not None
    view.meeting["shares_outstanding"] = None

    text = render.filing(view)

    assert "vote: plurality, abstentions count against: not stated" in text
    assert "shares not stated" in text
    assert "None" not in text
