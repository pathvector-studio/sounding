# IMAP vs rsync (sidrops) - numbers only

- IMAP window start (SINCE): 2026-10-08; IMAP messages saved: 146
- rsync messages per file: {'2026-10.mail': 100, '2026-11.mail': 66}; total 166; without parsable Date: 0
- rsync messages in the same window (Date >= since - 1 day): 148
- distinct Message-IDs: IMAP 146, rsync-in-window 148
- IMAP messages without Message-ID: 0; rsync: 0
- Message-ID overlap: 146; only IMAP: 0; only rsync (in window): 2
- duplicate Message-IDs inside IMAP: 0
- header differences among overlapping messages: {'From': 0, 'Subject': 0, 'References': 0, 'In-Reply-To': 0, 'Date': 0}
- decoded body identical (whitespace-insensitive): 145; differs only by '>From ' escaping: 1; other difference: 0
- messages with mbox '>From ' escaped lines: 1
- From header looks obfuscated ('x at y', no '@'): rsync 0, IMAP 0
