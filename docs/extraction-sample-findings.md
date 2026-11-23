# Extraction sample: findings

A hand check of single-filing extractions (`obr extract --filing`) before any batch run. Each filing is compared against its cached text (`cache/text/<accession>.txt`) and the EDGAR document. The four checks only confirm that quotes are verbatim; this log records what they can't catch. Findings feed the eval set (Plan 5) and the review CLI (Plan 3).

Prompt version 1, schema version as of BR-15 Plan 2, model `claude-opus-5-5`, effort `high`.

## Runs

| Filing | Accession | Stage | Input tokens | Output tokens | Time | Items | Result |
|---|---|---|---|---|---|---|---|
| AAPL 2026 | `0001308179-26-000008` | checked | 104,301 | 4,652 | 34 s | 5 | Facts correct; F1 |
| XOM 2026 | `0001193125-26-147614` | checked | 288,282 | 8,683 | 75 s | 6 | Facts correct; F2–F7 |
| GOOGL 2026 | `0001308179-26-000342` | in_review | 142,235 | 17,348 | 120 s | 14 | Facts correct; sent to review only by F8; F9–F11 |

Planned next: JPM `0000019617-26-000096`, ACN `0001308179-25-000651`, MSFT `0001193125-25-245150` (expected `out_of_scope`), NOW `0001373715-25-000312` (possibly a special meeting **(verify)**).

## Findings

| # | Filing | Area | Severity | Finding |
|---|---|---|---|---|
| F1 | AAPL | Vote standard | Medium | Compound standard stored as one part. See below |
| F2 | XOM | Titles (neutrality) | High | Company-written headings for shareholder proposals are edited silently, with no rule. See below |
| F3 | XOM | Text conversion | Medium | Page headers and footers split sentences, so quotes can't span them. See below |
| F4 | XOM | Meeting | Low | `shares_outstanding` is null; the filing gives a count as of a date other than the record date |
| F5 | XOM | Quotes | Low | Item 1's `source_quote` doesn't name the nominees, so the 12 nominee names have no quote behind them |
| F6 | XOM | Category | Low | Item 4 (Texas redomiciliation, by merger) is `other`; no category fits a reincorporation |
| F7 | XOM | Cost | Info | 2.8× Apple's input tokens. The proxy includes the full merger agreement and Texas charter as annexes |
| F8 | GOOGL | Regex pass | High | "Proposal Number 1" isn't matched, so the regex pass found 0 candidates. The count check failed on a correct extraction, and the model got no candidate hint. See below |
| F9 | GOOGL | Texts (neutrality) | High | Two summaries state a proponent's framing in our own voice. See below |
| F10 | GOOGL | Vote standard | Low | The standard is a majority of *voting power* (Class B carries 10 votes per share); `vote_base = shares_present` loses that |
| F11 | GOOGL | Co-filers | Low | Items 11 and 12 say other co-filers exist but aren't named. `co_filers` is empty, which follows the spec, but the fact that there are co-filers is lost |

### F1. Compound vote standards (AAPL, all five items)

The filing: "the affirmative vote of (i) a majority of the shares present or represented by proxy and voting at the Annual Meeting and (ii) a majority of the shares required to constitute a quorum."

Stored: `vote_base = shares_present_and_voting`, `vote_threshold_pct = 50`, and a `vote_standard_quote` that ends after part (i). `vote_standard_text` has both parts.

Spec §8.2 says to "store the stricter base and keep the full wording." Two gaps:
- Neither part is always the stricter one; that depends on turnout. The rule can't be applied as written.
- The quote check verified only part (i). Part (ii) survives only in `vote_standard_text`, which isn't quote-checked.

This matters for reconciliation (§6.5): an item can win part (i) and still fail part (ii), so an outcome computed from the structured fields can be wrong.

### F2. Shareholder-proposal titles (XOM items 5 and 6)

The company's headings contain the company's own argument. The extraction shortened them without any stated rule:

| Item | Company heading | Proponent's own title | Stored `title` |
|---|---|---|---|
| 5 | "Independent Chair, a proposal overwhelmingly defeated on 16 separate occasions since 2000" | "Request for Board of Directors to Adopt Policy for an Independent Chair" | "Independent Chair" |
| 6 | "Proposal requesting Company to modify its Voluntary Retail Voting Program to provide multiple options not aligned with the Board's recommendations" | "Board-Aligned Automatic Voting" | "Proposal requesting Company to modify its Voluntary Retail Voting Program" |

Titles are facts, so they publish automatically when checks pass (§7.1), with no reviewer. Neither heading is neutral as written: the company's carries the board's argument, and the proponent's carries the proponent's. The handbook's equal-treatment rule means this needs one rule applied to every proposal. For example: a neutral title generated from the resolution, with both originals kept and attributed. Decide this before the batch, or titles will be inconsistent across filings.

### F3. Page furniture inside sentences (XOM item 6)

The resolution reads "…provides multiple independent options to shareholders so`\n`2026 Proxy Statement`\n`119`\n`Table of Contents`\n`that the retail voting program…". The stored `source_quote` starts mid-sentence at "that the retail voting program…", after the break, because a verbatim quote can't cross the page header. The quote is accurate but leaves out the start of the request.

Any quote that crosses a page break has this problem. Fix it either in fetch's text conversion (strip running headers and footers) or in the quote check (ignore known page furniture when matching).

### F4. Share count not at the record date (XOM)

The filing: "On February 28, 2026, there were 4,156,559,415 shares of common stock outstanding and entitled to vote." The record date is April 1, 2026. Null follows the "not stated: null, never a guess" rule. But the schema has no as-of date for the count, so the number can't be stored honestly. It matters only for standards based on shares outstanding (none at XOM).

The results 8-K does give the record-date count: "As of the close of business on April 1, 2026, which was the record date … 4,144,455,560 shares … were outstanding and entitled to vote" (`0000034088-26-000078`). Reconciliation (Plan 4) could fill `shares_outstanding` from the 8-K when the proxy doesn't state it. For multi-class companies (GOOGL), one number doesn't capture the voting power in any case.

### F8. "Proposal Number N" headings (GOOGL)

`openboardroom/extract/regex.py` matches `Proposal|Item`, then an optional `No.`, then the number. Alphabet writes "Proposal Number 1: Election of Directors" (summary table, line 797) and "Proposal Number 1:" (section heading, line 3459). Neither matches, so the regex pass found 0 candidates. The model's 14 items all match the filing, but the count check failed ("14 items for 0 candidates") and the filing went to review.

The fix is a parser bug fix. Per the handbook, it adds a fixture: Alphabet's heading lines with all 14 numbers expected. Before the batch, scan every cached DEF 14A for the heading styles in use, so other variants are found now rather than in review.

### F9. Summaries that adopt a proponent's framing (GOOGL items 8 and 9)

| Item | Resolution | Stored summary |
|---|---|---|
| 8 | "assessing the risks … arising from an **apparent** lack of viewpoint diversity" | "a report on risks from a lack of viewpoint diversity" |
| 9 | "risks of the use of diagnostic tools created by politicized corporate partners" | "a report on the benefits, costs, and risks of using diagnostic tools created by politicized corporate partners" |

Item 8 drops "apparent", so the summary asserts as fact something the proponent only claims. Item 9 repeats the proponent's characterization ("politicized") in our voice, without quotes or attribution. Both summaries describe the request accurately, but the summary is the platform's own layer-1 voice, where the policy allows "no adjectives that take sides." A style rule is needed: a summary keeps the resolution's hedges, and puts any characterization taken from one side in quotes or attributes it ("which the proponent describes as…"). The same rule applies to board-written framing. These texts go through review before publication, but the prompt should get them right first.

## Verified correct

- **AAPL:**
  - meeting (2026-02-24, 8:00 A.M. Pacific, virtual, record date 2026-01-02) and the 14,697,926,000 share count
  - 5 items, their recommendations, and the 8 nominees
- **XOM:**
  - Meeting: 2026-05-27, 9:30 a.m. Central, virtual, record date 2026-04-01.
  - 6 items, their types, and the board recommendations.
  - Vote standards: majority of votes cast, abstentions not counted. This also holds for Item 4: the "Votes Required" section names the merger, and the proxy's discussion of the New Jersey two-thirds default doesn't apply.
  - Filers: item 5 has the National Legal and Policy Center as lead filer. Item 6 has the New York City Police Pension Fund as lead filer and the New York City Comptroller's Office as representative, which matches "submitted by … on behalf of".
  - Requests: both match their resolutions.
  - Argument figures traced to the filing: "voted down 16 times since 2000", "12 of our 13 directors are fully independent", "holders of 150 M shares having already signed up", and item 5's "60 percent of S&P 500 companies had separate CEOs and Board Chairs as of 2024" (the proponent citing the Spencer Stuart Board Index) and its Calibre One citation.
- **GOOGL:**
  - Meeting: 2026-06-05, 9:00 a.m. Pacific, virtual, record date 2026-04-06.
  - 14 items, in the filing's order, and their recommendations.
  - 10 nominees.
  - Vote standard: majority of voting power present, abstentions counting against.
  - Lead filers, plus representatives where the filing says "on behalf of": Bowyer Research for the Heritage Foundation (item 9), Zevin Asset Management for Denise and Gerald Bergman (item 11), and the Shareholder Association for Research & Education for the United Church of Canada pension plan (item 12).
  - Co-filer: Mercy Investment Services on item 7.
  - The facts behind item 12's proponent argument (the October 2025 charter amendments that removed oversight of civil and human rights) are in the filing.
- **Argument voice:** every argument is attributed ("The board argues…", "The proponent argues…"). The board's "long-standing political campaign by a serial submitter" became "a political campaign by a frequent proposal submitter": still attributed, but softened. Whether paraphrase may soften a side's wording is a question for the style rules.
