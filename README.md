# Computer-Use Automation System

Drives legacy bank UIs that have no API. An LLM works out how to do a task
**once**; the run is recorded as a typed, versioned artifact; a no-model engine
replays it deterministically forever after.

> **LLM = compiler** (run once, expensive). **Artifact = compiled program.**
> **Replay = runtime** (run often, cheap, no model in the loop).

- [REPORT.md](REPORT.md) — architecture, schema, error taxonomy, safety, cuts
- [evidence/](evidence/README.md) — real transcripts from every scenario
- [PLAN.md](PLAN.md) — the implementation plan this was built against

---

## Setup

Python 3.11+.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

**No API key is needed for any of the following.** Discovery is the only thing
that talks to a model; everything else — the tests, the whole replay path, the
error taxonomy, safety, handoff — runs offline. That asymmetry is the point of
the design, so it is worth checking:

```bash
python -m pytest tests -q
```

115 tests, no key, no network beyond a local server. They skip cleanly if
Chromium is not installed.

---

## The demo, in four commands

### 1. Replay a recorded capability — deterministic, no model

```bash
python cli.py replay lookup_member_balance --member_id 12345 --branch_code 001
```

```
Success({'savings_balance': '$2,755.55'})
  classification: success
  savings_balance: $2,755.55
```

### 2. A business outcome is an answer, not a crash

```bash
python cli.py replay lookup_member_balance --member_id 99999 --branch_code 001
```

```
BusinessOutcome(member_not_found)
  classification: business_outcome
  No member exists with that ID. A legitimate answer, not a failure.
```

Exit code **0**. The bank answered the question it was asked. Try `00000` for a
permission denial.

### 3. Survive drift

```bash
python cli.py replay lookup_member_balance --member_id 12345 --branch_code 001 \
  --entry-url "http://127.0.0.1:8000/members.html?drift=1"
```

The app renames its accessible label. Rung 1 fails, rung 2 catches it, the run
succeeds — and the transcript records the fallback trail. Use `?drift=2` to
remove a control entirely and see a debuggable `HardFailure` with a screenshot.

### 4. Safety on an irreversible action

```bash
python cli.py replay open_sub_account --member_id 12345 --branch_code 001 --initial_deposit 500
python cli.py replay open_sub_account --member_id 12345 --branch_code 001 --initial_deposit 500 --confirm
```

The first stops at the Confirm step. The second opens the account.

---

## Discovery — the only part that needs a key

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env        # gitignored
python cli.py discover "look up member 12345 in branch 001 and read the savings balance" \
  --param member_id=12345 --param branch_code=001 --sensitive member_id
```

Roughly $0.30 per run on `claude-opus-5`. It writes a transcript to
`evidence/` and a **draft** artifact to `artifacts/`.

`--param` is not optional bookkeeping: anything the model types must be one of
the declared parameters, so the artifact can store a reference instead of a
literal. Typing an undeclared value is refused at record time.

Then review it — discovery only ever walked one path:

```bash
python cli.py review lookup_member_balance
```

It reports what only a human can decide (failure branches, which steps commit,
checkpoints that assume the happy path). Edit the JSON, then:

```bash
python cli.py review lookup_member_balance --approve --by yourname
```

Replay refuses a draft by default, because a draft has no authored failure
branches.

---

## Human handoff

When replay gets stuck, it can hand you the **live browser session** over CDP:

```bash
python cli.py replay lookup_member_balance --member_id 12345 --branch_code 001 \
  --entry-url "http://127.0.0.1:8000/members.html?blocker=1" --operator
```

`?blocker=1` raises a "Supervisor Override Required" screen no recording has
ever seen. Replay prints an intervention brief and a CDP URL, you attach a
browser and click Override, press Enter — and the run re-observes, sees the
step is done, and finishes with the balance.

See [`evidence/handoff-20260918-204523`](evidence/handoff-20260918-204523/transcript.md)
for a complete recorded example.

---

## The target application

`app/members.html` is a deliberately legacy-hostile stand-in bank UI: table
layout, no test IDs, `<font>` tags — but real labels, buttons and roles, so the
accessibility tree is honest. Accessible to a screen reader, hostile to a lazy
automator.

Run it on its own with `python -m app.server`.

**One field has no label, no id and no aria-label.** It is anonymous in the
accessibility tree, so role+name targeting cannot reach it at all — which is
what forces the relational rung to do real work instead of being decoration.

Behaviour you can trigger:

| Input / flag | What happens | Classified as |
|---|---|---|
| member `99999` | "No such member." | business outcome |
| member `00000` | permission denied | business outcome |
| member blank | "Please enter a Member ID." | **hard failure** — our typing didn't land |
| deposit > 10,000 | manager approval required | business outcome |
| `?drift=1` | accessible label renamed | rung fallback |
| `?drift=2` | a control removed | hard failure |
| `?maintenance=1` | interstitial before results | recoverable |
| `?slow=2000` | delayed response | waited for, not slept through |
| `?blocker=1` | override screen nothing knows about | escalation |

Every one of those branches lives in the page, never in replay.

---

## Layout

```
app/        members.html, admin.html (off-limits on purpose), server.py
core/
  schema.py     the contract — typed, versioned, validated
  driver.py     THE SEAM: Driver interface + WebDriver over CDP
  agent.py      discovery loop, provider seam, stopping conditions
  recorder.py   verified ladders, proposed checkpoints, artifact assembly
  replay.py     deterministic executor + the error taxonomy
  safety.py     allowlist: origins, routes, action types
  handoff.py    ownership ledger, intervention request, CDP transfer
  logging.py    structured, redacting evidence log
artifacts/  approved capabilities (+ artifact.schema.json at the root)
evidence/   real transcripts from every scenario
tests/      115 tests
scripts/    the original spike that de-risked a11y perception and CDP handoff
cli.py      discover / review / replay
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success, **or** a business outcome — the caller got a real answer |
| 1 | hard failure — the surface is not what the artifact says |
| 2 | bad usage (missing key, unknown parameter, draft artifact) |
| 3 | refused by policy — distinct from a failure, and never escalated |
