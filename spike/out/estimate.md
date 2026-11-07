# Daily input-token and cost estimate (PLACEHOLDER prices)

All prices and the marked assumptions are placeholders; real models and prices are decided in phase 2.
Tokens = chars/4 estimates.

## Measured (sidrops, last 30 days, IMAP)
- messages/day: 4.87; preprocessed message tokens/day (stage 6): 1314
- active threads/day (threads with >=1 kept message): 2.20 (days with activity: 21/30; peak day: 7 threads, 9367 tokens)
- sidrops monthly mbox size, Jan-Oct 2026: min 449190, max 2720481 bytes; busy-month factor (max/mean): 1.94

## Scaling by rsync monthly file size (Jan-Oct 2026 full months; listing only, nothing downloaded)
- sidrops: total 14056405 bytes, ratio to sidrops 1.00
- idr: total 116414066 bytes, ratio to sidrops 8.28
- grow: total 9769271 bytes, ratio to sidrops 0.70
- ietf: total 18425521 bytes, ratio to sidrops 1.31

Assumptions (placeholders): state 1500 tok/thread; system prompt 1000 tok/call; output 700 tok/thread; cheap stage 150 tok/message; ietf keyword pass-through 15%. Threads/day scales with file size like tokens. No interest-profile filtering applied (upper bound).
Placeholder prices USD/1M tokens: main in 3.0 / out 15.0; cheap in 0.25 / out 1.25.

| scenario | new msgs/day | new msg tokens/day | active threads/day | main input tok/day | main output tok/day | cheap input tok/day | USD/day | USD/30 days |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A: sidrops only | 4.9 | 1314 | 2.2 | 6814 | 1540 | 730 | 0.044 | 1.32 |
| B: sidrops + idr + grow | 48.6 | 13110 | 21.9 | 67983 | 15364 | 7283 | 0.437 | 13.12 |
| C: B + ietf (keyword-filtered) | 49.5 | 13369 | 22.4 | 69323 | 15667 | 7427 | 0.446 | 13.38 |
| C in a busy month | 95.8 | 25874 | 43.3 | 134168 | 30322 | 14374 | 0.863 | 25.90 |

Main input = new message tokens + threads x (state + system prompt). Without differential summarization (re-reading whole threads) the input would be larger; this estimate follows DESIGN 3.3.
