# Filing text fixtures

Plain text of three DEF 14A primary documents, produced by `openboardroom.text.to_text`.
Raw HTML is not committed. Retrieved 2026-11-22 with `EdgarClient`
(User-Agent `Boardroom LLC quinn@brdrmr.org`).

| Fixture | Source | Accession | Raw sha256 | Text sha256 |
|---|---|---|---|---|
| `aapl-2023-def14a.txt` | https://www.sec.gov/Archives/edgar/data/320193/000130817923000019/laap2023_def14a.htm | 0001308179-23-000019 | `62753e9405d6aa7c3d573a5effb5b3fe87e5da3bcdca5fe2e36fe012a05c48c4` | `6cfdfa62fa5aa06f8b5cd8a877a3ee9c895da2b7d1d6ad2258c0a0e5cddfdb4a` |
| `aapl-2024-def14a.txt` | https://www.sec.gov/Archives/edgar/data/320193/000130817924000010/laapl2024_def14a.htm | 0001308179-24-000010 | `56a3b2430961e9cd961b82e55c2c66a3e638dbb1936c54a234c26d1c49e7e32c` | `77eed34f3a3e08b4c1f3c2640a85772f3a1d812945121dac471609e523cc6f45` |
| `msft-2023-def14a.txt` | https://www.sec.gov/Archives/edgar/data/789019/000119312523259247/d356108ddef14a.htm | 0001193125-23-259247 | `70ddcfc73f2714a2caaa05d59a2f567957407139b1998cae1abeb5f12d8b087b` | `31a151f91cb2bf382249c12e6e414699804afd72780a5019da13402c7d1cd1c0` |

Ballot sizes, checked against each filing: Apple 2023 has 9 proposals (proxy card items 1-9),
Apple 2024 has 8 (table of contents and each proposal's section), Microsoft 2023 has 13
(4 management and 9 shareholder proposals). `tests/fixtures/regex/*.expected.json` holds the
regex pass's expected candidates for each.
