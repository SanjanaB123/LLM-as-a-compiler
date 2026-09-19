# Spike — de-risking the two empirical assumptions

Before committing the implementation plan, we probe the two things that can't
be decided on paper: (1) does our a11y-first perception actually work on a
legacy-hostile page, and (2) does same-session human handoff actually work.

## Files
- `members.html` — the legacy-hostile target: table layout, no test IDs,
  non-semantic wrappers, but real labels/roles. Authored runtime errors
  (member `99999` = not found, `00000` = permission denied, blank = validation).
- `predict_a11y.py` — static check (no browser): predicts role+name per control
  and scores each ladder rung. **Already run — passed.**
- `probe_browser.py` — PROBE 1: real Chromium a11y snapshot + rung-1 targeting.
- `probe_handoff.py` — PROBE 2: same-session control transfer via CDP attach.

## Run locally (one-time browser install)
```bash
pip install playwright beautifulsoup4
python -m playwright install chromium   # downloads the browser binary (~1 min)

python3 predict_a11y.py     # (optional) static prediction — already green
python3 probe_browser.py    # PROBE 1 — expect "PROBE 1 OK"
python3 probe_handoff.py    # PROBE 2 — expect "PROBE 2 OK — one session, three control transfers"
```

## What each result tells us
- Probe 1 green  -> perception + rung-1 targeting are sound; build the real Driver.
- Probe 1 shows an anonymous control -> good; that's where rung 2 (relational) earns its place.
- Probe 2 green  -> the handoff mechanism is real; the ownership-flag design is buildable.
- Probe 2 red    -> fall back to CDP `connect_over_cdp` with a launched-with-remote-debugging
  browser; documented alternative before we commit.
