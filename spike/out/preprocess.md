# Preprocessing reduction (sidrops, IMAP last 30 days) - numbers only

Messages: 146; tokens are chars/4 estimates.

| stage | chars | est. tokens | % of raw | % of body-only |
|---|---:|---:|---:|---:|
| 1 raw (with headers) | 1846139 | 461535 | 100.0 |  |
| 2 body only | 441441 | 110360 | 23.9 | 100.0 |
| 3 quotes removed | 176399 | 44100 | 9.6 | 40.0 |
| 4 signature removed | 174427 | 43607 | 9.4 | 39.5 |
| 5 footer/Note Well removed | 174221 | 43555 | 9.4 | 39.5 |
| 6 auto mails dropped | 157675 | 39419 | 8.5 | 35.7 |

- messages dropped as auto-generated: 16 {'Auto-Submitted header': 16}
- non-dropped messages with <20 chars left after stage 5: 2
- per-message token quartiles after stage 6 (kept messages): {'min': 2, 'q1': 34, 'median': 89, 'q3': 321, 'max': 4056}
