# spike/ - phase 0 measurements (throwaway, see specs/phase0.md)

Standard library only (written to run on Python 3.9+; the project target is 3.12). Run everything from the repository root.
Raw data goes to `spike/data/` (gitignored). `spike/out/` is committed and holds numbers and categories only.

## Fetch (network; run rarely, be polite)
```sh
# needs FETCH_CONTACT (env var, or a FETCH_CONTACT= line in ./.env). The value is never printed or saved.
python3 spike/fetch_imap.py     # anonymous IMAP, sidrops, last 30 days -> spike/data/imap/
sh spike/fetch_rsync.sh         # this and previous month mbox, run twice -> spike/data/rsync/ (stats in rsync_runs.txt)
sh spike/list_sizes.sh          # rsync --list-only of sidrops/idr/grow/ietf (sizes only, no download)
```

## Analyse (offline, from spike/data/)
```sh
cd spike
python3 compare.py      # IMAP vs rsync          -> out/compare.md
python3 thread.py       # JWZ vs server THREAD   -> out/thread.md, data/threads.json
python3 preprocess.py   # 6-stage reduction      -> out/preprocess.{csv,md}, data/samples/ (20 pairs, for human review)
python3 estimate.py     # daily tokens and cost  -> out/estimate.md  (needs the three steps above)
```
`estimate.py` prices and assumptions are placeholders at the top of the file.
