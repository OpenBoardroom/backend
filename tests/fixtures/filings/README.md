# Filing text fixtures

Plain text of four DEF 14A primary documents, produced by `openboardroom.text.to_text`.
Raw HTML is not committed. Retrieved 2026-11-22 with `EdgarClient`
(User-Agent `Boardroom LLC quinn@brdrmr.org`).

**Update (2026-11-23):** all four texts were rebuilt with block-level conversion and page-furniture
removal (backend plan 2026-11-23). The three older documents were downloaded again through
`EdgarClient`; none matched the raw sha256 recorded on 2026-11-22 (Apple 2023 now arrives with an
SGML `<DOCUMENT>` header), so the table records today's raw and text hashes. Ballot counts are
unchanged.

| Fixture | Source | Accession | Raw sha256 | Text sha256 |
|---|---|---|---|---|
| `aapl-2023-def14a.txt` | https://www.sec.gov/Archives/edgar/data/320193/000130817923000019/laap2023_def14a.htm | 0001308179-23-000019 | `b849951eb75962fc6129a9f800262fc04c6b47f9e84cb8d813a3949dbbafd6c2` | `7ec8db95e47fe1b5f9e1b2e9a49b2ee7960241c034b7d35d07eeb73874e4ce34` |
| `aapl-2024-def14a.txt` | https://www.sec.gov/Archives/edgar/data/320193/000130817924000010/laapl2024_def14a.htm | 0001308179-24-000010 | `da4a310060bedef5020d7d8cdcd52b053b29b77c6b13cf8e1b4c453f5a00f8cf` | `8ec3f0b067e9a8879b1f066d78cdfbc15692af614773ac9d5af8289abf9e0c5f` |
| `msft-2023-def14a.txt` | https://www.sec.gov/Archives/edgar/data/789019/000119312523259247/d356108ddef14a.htm | 0001193125-23-259247 | `2fbf508cd9a9c3502472d9152230acb84c19cb838f890cd83bbbca521727098d` | `89b9ab1e5894023a07d6b43c320d2990ea4ea1b00fcf23fb3967c857a537e9bf` |
| `googl-2026-def14a.txt` | https://www.sec.gov/Archives/edgar/data/1652044/000130817926000342/goog-20260424.htm | 0001308179-26-000342 | `a74eec5771ad796d7d48acb9f0acf33f002d68d7893e2291223c4d2f4e7ce030` | `17de3c6bd19e465904b669882d7d6aa715491d8517c724cd43d34a61caf82bfc` |

The Alphabet 2026 fixture was added 2026-11-23 from the local cache (fetched the same way). It
uses "Proposal Number N" headings, which the regex pass missed before (extraction sample finding F8).

Ballot sizes, checked against each filing: Apple 2023 has 9 proposals (proxy card items 1-9),
Apple 2024 has 8 (table of contents and each proposal's section), Microsoft 2023 has 13
(4 management and 9 shareholder proposals), Alphabet 2026 has 14 (4 management and 10
shareholder proposals). `tests/fixtures/regex/*.expected.json` holds the
regex pass's expected candidates for each.

`boeing-2026-defa14a.htm` (added 2026-11-27) is the raw primary document of Boeing's 2026 DEFA14A,
accession 0001193125-26-096842 (https://www.sec.gov/Archives/edgar/data/12927/000119312526096842/),
copied from the local cache; sha256 `6118ec944ec03a88462159f48855adfe02912cf20a900cfcef5e05e6317938f3`.
It holds three versions of one voting notice on 7 pages. Page-furniture removal used to treat its
repeated lines as running headers and drop all 21 of them (backend review, 2026-11-27).
