# JWZ vs server THREAD=REFERENCES (sidrops, last 30 days) - numbers only

- messages: 146; server-threaded: 146
- threads: mine (JWZ + subject merge) 49; mine without subject merge 56; server 49
- messages whose whole thread grouping matches the server: 146/146 = 100.0% (without subject merge: 102 = 69.9%)
- thread size distribution (mine, top 10): [36, 8, 8, 8, 8, 6, 6, 5, 4, 4]; singletons: 29
- messages with no References/In-Reply-To: 38
- disagreeing clusters: 0 -> categories {}

## Disagreement examples (UIDs, up to 6 per side; no bodies)
- none (after the subject normalization fix described in the README; before it, one gateway-tagged '[Internet]' subject and one localized reply prefix kept 1 message out of a 36-message thread)

## Without the subject fallback: 10 disagreeing clusters -> {'server merged more: member lacks References/In-Reply-To (missing headers)': 9, 'server merged more: other': 1}
(first 10; UIDs up to 6 per side)
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 3 uids [7258, 7260, 7267]; server size 4 uids [7258, 7260, 7264, 7267]
- server merged more: other: mine size 1 uids [7264]; server size 4 uids [7258, 7260, 7264, 7267]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 2 uids [7281, 7284]; server size 4 uids [7281, 7284, 7391, 7392]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 24 uids [7303, 7304, 7305, 7314, 7315, 7316]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 1 uids [7334]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 2 uids [7335, 7336]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 1 uids [7342]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 7 uids [7350, 7351, 7366, 7367, 7368, 7379]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 1 uids [7369]; server size 36 uids [7303, 7304, 7305, 7314, 7315, 7316]
- server merged more: member lacks References/In-Reply-To (missing headers): mine size 2 uids [7391, 7392]; server size 4 uids [7281, 7284, 7391, 7392]
