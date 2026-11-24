# Extraction sample: findings

A hand check of single-filing extractions (`obr extract --filing`) before any batch run. Each filing is compared against its cached text (`cache/text/<accession>.txt`) and the EDGAR document. The four checks only confirm that quotes are verbatim; this log records what they can't catch. Findings feed the eval set (Plan 5) and the review CLI (Plan 3).

Prompt version 1 (version 2 from 2026-11-23, marked in the table; version 3 and schema version 2 from 2026-11-24, which add `share_capital_authority`), schema version as of BR-15 Plan 2, model `claude-opus-5-5`, effort `high`. The v1 rows were deleted from the local database before the v2 re-runs; this log is their record.

## Runs

| Filing | Accession | Stage | Input tokens | Output tokens | Time | Items | Result |
|---|---|---|---|---|---|---|---|
| AAPL 2026 | `0001308179-26-000008` | checked | 104,301 | 4,652 | 34 s | 5 | Facts correct; F1 |
| XOM 2026 | `0001193125-26-147614` | checked | 288,282 | 8,683 | 75 s | 6 | Facts correct; F2–F7 |
| GOOGL 2026 | `0001308179-26-000342` | in_review | 142,235 | 17,348 | 120 s | 14 | Facts correct; sent to review only by F8; F9–F11 |
| AAPL 2026 (prompt v2) | `0001308179-26-000008` | checked | 98,106 | 6,946 | 52 s | 5 | Facts correct; F2 and F9 rules applied. See below |
| XOM 2026 (prompt v2) | `0001193125-26-147614` | checked | 282,722 | 8,764 | 68 s | 6 | Facts correct; F2, F3 fixed; F12–F14 |
| GOOGL 2026 (prompt v2) | `0001308179-26-000342` | in_review | 133,322 | 21,003 | 175 s | 14 | Facts correct; F8 fixed; sent to review by F15; F16 |
| JPM 2026 (prompt v2) | `0000019617-26-000096` | checked | 125,379 | 8,688 | 68 s | 7 | Facts correct; F17. See below |
| ACN 2026 (prompt v2) | `0001308179-25-000651` | checked | 126,776 | 5,166 | 37 s | 7 | Facts correct; F18–F20. See below |
| ACN 2026 (prompt v3) | `0001308179-25-000651` | checked | 126,941 | 5,167 | 41 s | 7 | F20 fixed; otherwise as v2. See below |
| MSFT 2025 (prompt v2) | `0001193125-25-245150` | out_of_scope | 143,630 | 10,529 | 80 s | 10 (not stored) | Correct: meeting 2025-12-05, before any index membership; F21 |

Not run: NOW `0001373715-25-000312` (possibly a special meeting **(verify)**).

## Findings

| # | Filing | Area | Severity | Finding |
|---|---|---|---|---|
| F1 | AAPL | Vote standard | Medium | Compound standard stored as one part. See below |
| F2 | XOM | Titles (neutrality) | High | Company-written headings for shareholder proposals are edited silently, with no rule. Rule set in prompt v2. See below |
| F3 | XOM | Text conversion | Medium | Page headers and footers split sentences, so quotes can't span them. Fixed (`b2c4612`). See below |
| F4 | XOM | Meeting | Low | `shares_outstanding` is null; the filing gives a count as of a date other than the record date |
| F5 | XOM | Quotes | Low | Item 1's `source_quote` doesn't name the nominees, so the 12 nominee names have no quote behind them |
| F6 | XOM | Category | Low | Item 4 (Texas redomiciliation, by merger) is `other`; no category fits a reincorporation |
| F7 | XOM | Cost | Info | 2.8× Apple's input tokens. The proxy includes the full merger agreement and Texas charter as annexes |
| F8 | GOOGL | Regex pass | High | "Proposal Number 1" isn't matched, so the regex pass found 0 candidates. The count check failed on a correct extraction, and the model got no candidate hint. Fixed, and a scan found 10 more filings with other causes; after the conversion fix, 6 remain, all with unlabeled items. See below |
| F9 | GOOGL | Texts (neutrality) | High | Two summaries state a proponent's framing in our own voice. Rule set in prompt v2. See below |
| F10 | GOOGL | Vote standard | Low | The standard is a majority of *voting power* (Class B carries 10 votes per share); `vote_base = shares_present` loses that |
| F11 | GOOGL | Co-filers | Low | Items 11 and 12 say other co-filers exist but aren't named. `co_filers` is empty, which follows the spec, but the fact that there are co-filers is lost |
| F12 | XOM v2 | Arguments (neutrality) | Medium | The two sides' claims are reported at different levels of detail. Declined: no prompt rule. See below |
| F13 | XOM v2 | Quotes | Low | `recommendation_quote` picked the board's framed sentence ("For the 17th time, the Board recommends a vote AGAINST this proposal.") when a plain one exists |
| F14 | XOM v2 | Quotes | Low | Items 2–6 share one vote-standard sentence, and the 40-word cap makes the quote start mid-sentence ("and the shareholder proposals require…"), so it doesn't name the item it covers |
| F15 | GOOGL v2 | Consistency check | Medium | Fixed: the check now needs a lead filer **or** a representative. It required a lead filer on every shareholder proposal, but item 6's filing names only the representative ("Inspire Investing, LLC, on behalf of its client"). The model correctly left `lead_filer` empty, so the check sent a correct extraction to review. v1 had listed the representative as the lead filer, which was wrong |
| F16 | GOOGL v2 | Arguments (accuracy) | Low | Item 7's proponent argument says "98% of unaffiliated Class A votes"; the filing says "Outside shareholders overwhelmingly support this proposal, with 98% backing in 2025". "Class A" is not in the filing. Arguments aren't quote-checked; review catches this |
| F17 | JPM v2 | Arguments (accuracy) | Low | Item 5's proponent argument softens two of the proponent's claims. "Instances of apparent fraud in secured lending" became "collateral irregularities in secured lending". "A former executive who was convicted of defrauding JPM" in the Frank acquisition became "an acquisition-related fraud conviction", which drops that JPM was the one defrauded. Same kind of drift as the board's softened wording in GOOGL v1. Left to review, like F16 |
| F18 | ACN v2 | Meeting | Low | `shares_outstanding` is null, but the filing states the record-date count: "660,352,923 Class A ordinary shares outstanding (which includes 44,997,383 shares held by Accenture) and 302,358 Class X ordinary shares outstanding". Treasury shares can't vote, so the voting count is 615,355,540 Class A plus 302,358 Class X, 615,657,898 in all. One integer field can't hold two classes and a treasury carve-out; null is the safe answer, but the fact is lost. Same field as F4 |
| F19 | ACN v2 | Effect | Low | Item 4 bundles a non-binding auditor ratification with a binding authorization to set the auditor's pay. `effect = advisory` describes only the first part. The summary states both. Same shape as F1: one structured field for a two-part item |
| F20 | ACN v2 | Category | Medium | Fixed in prompt v3 and schema v2: new category `share_capital_authority` (`binding`). Items 5–7 (authority to issue shares, opt-out of pre-emption rights, treasury re-allotment price range) are `other`. These are routine yearly items for every Irish-incorporated company, and many S&P 500 companies are Irish plcs **(verify the count)**, so `other` would fill with them. Needs a schema decision: a `share_capital_authority` category, or accept `other`. Related to F6 |
| F21 | MSFT 2025 v2 | Cost | Info | Scope is decided after a full extraction, so an out-of-scope filing costs as much as an in-scope one ($0.79 here). Four more queued proxies are for 2025 meetings, before the S&P 100 memberships start (2026-01-01). Their cached texts give the dates: SNDK `0001628280-25-044481` (2025-11-18), CSCO `0000858877-25-000150` (2025-12-16), PANW `0001308179-25-000624` (2025-12-09), and NOW `0001373715-25-000312` (2025-12-05). Together about $1.50 batched |

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

**Decided 2026-11-23 (prompt v2):** every title, management or shareholder, follows one standard: a short neutral name for what the vote is about, taken from the resolution or a plain heading. The model writes it itself whenever the company's heading or the proponent's title carries an argument, a characterization, or a vote history. The original headings aren't stored as separate fields; the filing link and `source_quote` keep them reachable. Storing them would change the schema contract (ADR-0003).

### F3. Page furniture inside sentences (XOM item 6)

The resolution reads "…provides multiple independent options to shareholders so`\n`2026 Proxy Statement`\n`119`\n`Table of Contents`\n`that the retail voting program…". The stored `source_quote` starts mid-sentence at "that the retail voting program…", after the break, because a verbatim quote can't cross the page header. The quote is accurate but leaves out the start of the request.

Any quote that crosses a page break has this problem. Fix it either in fetch's text conversion (strip running headers and footers) or in the quote check (ignore known page furniture when matching).

**Fixed** in `b2c4612`: fetch drops a line at a page edge when it repeats there on at least 40% of pages (measured: furniture 47–100%, repeated content at most 23%). XOM's resolution now reads "…shareholders so" / "that the retail voting program…" on adjacent lines, which the quote check joins.

### F4. Share count not at the record date (XOM)

The filing: "On February 28, 2026, there were 4,156,559,415 shares of common stock outstanding and entitled to vote." The record date is April 1, 2026. Null follows the "not stated: null, never a guess" rule. But the schema has no as-of date for the count, so the number can't be stored honestly. It matters only for standards based on shares outstanding (none at XOM).

The results 8-K does give the record-date count: "As of the close of business on April 1, 2026, which was the record date … 4,144,455,560 shares … were outstanding and entitled to vote" (`0000034088-26-000078`). Reconciliation (Plan 4) could fill `shares_outstanding` from the 8-K when the proxy doesn't state it. For multi-class companies (GOOGL), one number doesn't capture the voting power in any case.

### F8. "Proposal Number N" headings (GOOGL)

`openboardroom/extract/regex.py` matches `Proposal|Item`, then an optional `No.`, then the number. Alphabet writes "Proposal Number 1: Election of Directors" (summary table, line 797) and "Proposal Number 1:" (section heading, line 3459). Neither matches, so the regex pass found 0 candidates. The model's 14 items all match the filing, but the count check failed ("14 items for 0 candidates") and the filing went to review.

**Fixed** in `8fd422c`: the pattern accepts "Number". Alphabet 2026 is now a golden fixture with 14 candidates.

**Scan of all 105 cached DEF 14As (2026-11-23).** The regex pass was run on every cached proxy and the results compared with each filing's text. Before the fix, 3 filings had 0 candidates and 7 had gaps in their numbering. After it, 9 filings are still wrong, plus JNJ, whose missing items come after the last one found, so a gap check can't see them:

| Cause | Filings | Example |
|---|---|---|
| Text conversion splits an inline label across lines | CSCO (items 2–3), JNJ (3–4), IBM (8) | "Proposal" / "2" / ": Advisory Vote to Approve Executive Compensation" on three lines |
| Label in another position or wording | PEP, HD, BA | "Animals within Supply Chain (Proxy Item No. 6)", "(ITEM 1 ON THE PROXY CARD)", "ELECTION OF DIRECTORS (ITEM 1)" |
| The number has no word next to it (a badge or bare numeral) | IBM (1–4), JNJ (1–2), BRK-B ("1." / "ELECTION OF DIRECTORS"); TXN (1–3) and V (1–4) probably **(verify)** | "2" / "Ratification of appointment of independent registered public accounting firm" |
| One unnumbered item | NOW 2025 (special meeting, stock split) | "PROPOSAL" / "Approval of an Amended and Restated Certificate of Incorporation to Effect a 5-for-1 Split…" |

That's 10 of 105 filings (about 10%). Every one of these misses sends a filing to review; none can publish a wrong count. But each costs a reviewer's time, and the model got a wrong or empty candidate hint on each.

The first cause comes from `openboardroom/text.py`: `soup.get_text("\n")` puts a newline between every text node, including inline `<span>`s inside one heading. Breaking lines only at block-level elements would fix that cause and probably part of F3 too. But it changes every cached text, `text_sha256`, and the fixtures. That's a change to the fetch stage, not a regex tweak.

**After the fix (2026-11-23).** Fetch now writes one line per text block (`b5ab7db`), drops page furniture (`b2c4612`), and `obr fetch --reconvert` rebuilt all 416 cached texts (415 changed, 0 failed). The regex pass also matches parenthesized labels (`1512159`). `scripts/scan_candidates.py` over the 105 DEF 14As now flags 6 filings, down from 10:

| Filing | Before | After | Why it remains |
|---|---|---|---|
| CSCO, JNJ, HD, BA, GOOGL | gaps or 0 | all items found | (fixed) |
| IBM | items 5–8 | items 5–8 | Items 1–4 are numbered badges with no word |
| V | items 5–8 | items 5–8 | Same |
| PEP | 0 | items 1, 4–6 | Items 2–3 have no number in the text, only titles |
| TXN | 1 | 0 | Its old "Proposal 4." line was a sentence split by the old converter ("…recommends you vote AGAINST" / "Proposal 4."), not a heading |
| BRK-B | 2 | 0 | Same: "…Recommends a Vote For" / "Item 2." |
| NOW 2025 | 0 | 0 | One unnumbered special-meeting item |

All six keep going to review until the 8-K reconciliation can confirm their counts, as the plan's scope ruling says. Fixture headings are now whole ("Proposal No. 4 - EEO Policy Risk Report", where the hint used to read "Proposal No. 4 - EEO"), which improves the candidate hint the model gets.

### F9. Summaries that adopt a proponent's framing (GOOGL items 8 and 9)

| Item | Resolution | Stored summary |
|---|---|---|
| 8 | "assessing the risks … arising from an **apparent** lack of viewpoint diversity" | "a report on risks from a lack of viewpoint diversity" |
| 9 | "risks of the use of diagnostic tools created by politicized corporate partners" | "a report on the benefits, costs, and risks of using diagnostic tools created by politicized corporate partners" |

Item 8 drops "apparent", so the summary asserts as fact something the proponent only claims. Item 9 repeats the proponent's characterization ("politicized") in our voice, without quotes or attribution. Both summaries describe the request accurately, but the summary is the platform's own layer-1 voice, where the policy allows "no adjectives that take sides." A style rule is needed: a summary keeps the resolution's hedges, and puts any characterization taken from one side in quotes or attributes it ("which the proponent describes as…"). The same rule applies to board-written framing. These texts go through review before publication, but the prompt should get them right first.

**Decided 2026-11-23 (prompt v2):** `summary`, `request`, and `title` keep the resolution's hedges. A characterization that belongs to one side is quoted and attributed ("what the proponent calls …"), with the same rule for board wording. The prompt's examples are invented, not taken from the sample filings, so the eval set stays a fair test of these rules.

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

## Prompt v2 re-runs (2026-11-23)

Text from the block-level converter with page furniture removed (`fef44ea`), prompt v2 (`bddbc96`).

**AAPL** (`checked`, $0.53 at standard rates; input down 6% from v1, because the page furniture is gone):
- **F2 applied.** Item 5's title is now "Report on Risks and Costs Related to Operations in China". In v1 it was "China Entanglement Audit", the proponent's own title.
- **F9 applied.** The summary reads "…what the proponent calls the company's \"continued entanglement\" with the People's Republic of China", quoting the resolution's wording and attributing it.
- **F1 partly better.** `vote_standard_quote` now covers both parts of the standard: "(i) … and (ii) a majority of the shares required to constitute a quorum". The structured fields still hold only part (i).
- **Arguments.** 67 and 68 words. The proponent's figures ("nearly 20% of Apple's revenues", rare earth minerals, the espionage reference) all trace to the filing.
- **Everything else is unchanged from v1:** meeting, 5 items, recommendations, and 8 nominees.

**XOM** (`checked`, $1.31; input down 2%):
- **F2 applied.** The titles are "Independent Board Chair" and "Voting Options in Retail Voting Program". Neither the company's heading ("…overwhelmingly defeated on 16 separate occasions…", "…not aligned with the Board's recommendations") nor the proponent's "Board-Aligned Automatic Voting" is used.
- **F3 fixed.** Item 6's `source_quote` now starts at the resolution ("Resolved: Shareholders request that the Board of Directors adopt and disclose policies…").
- **F9 applied.** "Robo-voting" is quoted and attributed on both sides ("creates what it calls \"robo-voting\""; "disputes the \"robo-voting\" comparison"). Item 6's `request` quotes the resolution's "does not inordinately advantage the Board's own voting recommendations".
- **Arguments.** 74/75 and 72/75 words, parallel in form and attributed. Every figure traces to the filing: 60% and 47% (Spencer Stuart), "16 times since 2000", "12 of 13", about 150 million shares, and Broadridge 2025.
- **Unchanged:** facts, vote standards, filers, the 12 nominees, F4 (`shares_outstanding` null), and F6 (`other` category).

### F12. Unequal detail between sides (XOM item 6)

The board's charge is kept specific and close to its wording: "describes the proposal as part of a political campaign by a frequent submitter" (the filing says "long-standing political campaign by a serial submitter"). The proponent's charges are flattened to "points to the Company's past shareholder relations". The filing says Exxon "has a history of being unresponsive to shareholders", lost a 2021 proxy contest to Engine No. 1, and in 2024 sued investors over a shareholder proposal. Both are attributed, but one side's claim is concrete and the other's is vague, which conflicts with the handbook's equal-treatment rule. **Fix:** add a rule to the prompt that each side's main claims are reported at the same level of detail, then confirm in review. The review CLI's word-count check can't catch this, because the lengths match.

**Declined 2026-11-23 [Quinn]:** no prompt rule. Shareholder proposals often carry more specific detail than the board's response by nature, and a rule forcing equal detail would tend to trim the proponent's specifics to match the board, which would build in a bias toward the company. Unequal detail is left to review.

**GOOGL** (`in_review`, $0.95; input down 6%, output up 21%):
- **F8 fixed.** The count check passed: 14 candidates for 14 items.
- **F2 applied.** All 14 titles are neutral, for example "Report on Risks of Diagnostic Tools from Corporate Partners" and "Recapitalization to One Vote per Share".
- **F9 applied.** Item 8 keeps "an apparent lack of viewpoint diversity". Item 9 reads "what the proposal calls \"politicized corporate partners.\"" Items 6, 11, 12, and 14 quote and attribute the proponents' own wording ("activist-driven", "accountability gap", "suspect record on data ethics").
- **Arguments.** 41–51 words each, with balanced pairs. The figures trace to the filing: 51% over 2019, $100,000 H-1B fee, $425 million verdict, the Gemma removal from AI Studio, and October 2025 charter changes. Exception: F16.
- **Representatives** are correct for items 6, 9, 11, and 12. Item 6 has no lead filer, which is correct (F15).


**JPM** (`checked`, $0.68 at standard rates; first run, prompt v2):
- **Meeting:** 2026-05-19, 10:00 a.m. Eastern, virtual, record date 2026-03-20. The 2,682,219,629 share count is stated at the record date, so F4 doesn't recur here.
- **Items:** 7 items in the filing's order, with their types and recommendations (FOR 1–3, AGAINST 4–7). The 11 nominees are correct.
- **Vote standards** match the filing's table and footnote. Item 1 is a majority of votes cast, with abstentions having no effect. Items 2–7 count abstentions as votes against ("votes FOR divided by the sum of votes FOR plus votes AGAINST plus votes ABSTAINING"). Item 3 is `advisory`, the same as the auditor ratification at AAPL, XOM, and GOOGL.
- **Filers:** each lead filer matches its "has advised us that it intends to introduce" line. Co-sponsors are disclosed only on request (footnote), so `co_filers` is correctly empty.
- **F2 applied.** The titles follow the filing's plain headings. "Lobbying alignment" and "Sustainability ROI report" became "Lobbying Alignment Report" and "Report on Return on Investment of Sustainability Investments".
- **Arguments.** All are attributed and parallel. The board's own characterizations ("misrepresents", "incorrectly suggesting", "unsupported link") stay inside "The board argues…". The prompt's quote rule covers only `title`, `summary`, and `request`, so this follows it. The proponent's figures trace to the filing: $1.5 trillion SRI, IEA Net Zero basis, $2.5 trillion target, $129 billion, $1 trillion green target, and the Epstein reports. Exception: F17.

**ACN** (`checked`, $0.61 at standard rates; first run, prompt v2; an Irish plc with no shareholder proposals):
- **Meeting:** 2026-01-28, 12:00 pm local time, in person at The Dock, Dublin, record date 2025-12-01. Exception: F18.
- **Items:** 7 items in the filing's order, all management and all recommended FOR. The 10 nominees are correct.
- **Vote standards** match the filing's chart. Every item is decided by votes cast, and abstentions have no effect. Items 6 and 7 are Irish special resolutions at 75%, and the rest are ordinary resolutions at a simple majority.
- **Summaries match the filing:**
  - Item 3: an additional 7 million shares, with the term extended to December 12, 2035.
  - Items 5 and 6: about 20% of issued share capital for 18 months.
  - Item 6: the rights-issue carve-out.
  - Item 4: names both the non-binding and the binding part. Exception: F19.
- **Quotes:** items 1 and 6 start their `vote_standard_quote` mid-sentence ("the resolution in respect of…"), as in F14.

**MSFT 2025** (`out_of_scope`, $0.79 at standard rates):
- **Scope:** the run read the meeting as 2025-12-05, backed by the quote "Annual Meeting to be held on December 5, 2025". No S&P 100 membership covers that date (all start 2026-01-01), so the filing was set aside and nothing was written. That is the designed outcome.
- **Output:** the run's stored output still holds the meeting (record date 2025-09-30, 7,433,087,554 shares, virtual) and 10 items, for audit.
- **Cost:** see F21.

**ACN under prompt v3** (`checked`, $0.61; 2026-11-24). The v2 run was deleted first, as with the earlier resets; its results are recorded above:
- **F20 fixed.** Items 5–7 are `share_capital_authority`, effect `binding`. Thresholds are unchanged: item 5 at 50%, items 6–7 at 75%.
- **Unchanged from v2:** the meeting, the other four items, the 10 nominees, the vote standards, and the summaries. Item 4's title now names KPMG. F18 and F19 remain.
