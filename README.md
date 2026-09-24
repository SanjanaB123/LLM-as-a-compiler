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

136 tests, no key, no network beyond a local server. They skip cleanly if
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

Two recorded examples: [an unrecognised screen](evidence/handoff-20260924-104232/transcript.md),
and [an expired session](evidence/handoff-session-expiry-20260924-104122/transcript.md) where
re-authentication needs credentials the automation must never handle — so it
cannot self-heal, and a person signs in instead.

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
| `?session_expires=1` | signed out mid-flow, sign-in required | **named** hard failure → escalation |

Every one of those branches lives in the page, never in replay.

---

## Measuring it, instead of claiming it

The table above is a set of claims. This runs them:

```bash
python cli.py eval lookup_member_balance --runs 20
```

```
scenario                           expected           result         median
member_found                       success            PASS 20/20     418ms
member_not_found                   business_outcome   PASS 20/20     317ms
our_typing_did_not_land            hard_failure       PASS 20/20     367ms
drift_absorbed_by_rung_two         success            PASS 20/20     434ms
drift_too_large_to_absorb          hard_failure       PASS 20/20    8203ms
interstitial_recovered             success            PASS 20/20     450ms
slow_response_waited_for           success            PASS 20/20    2525ms
unknown_blocker_stops_the_run      hard_failure       PASS 20/20    8483ms
...
11/11 scenarios consistent at 20 runs; 6/6 outcomes reached
```

Scenarios live in `evals/<capability>.json`. Two things are measured:

**Consistency** — each scenario lands in its bucket N/N, or it doesn't. The
drift scenario additionally asserts *which rung* resolved, because a
bucket-only check would pass even if rung 1 had quietly started working again
and the fallback had silently stopped being tested.

**Outcome reachability** — every branch the artifact declares must actually
fire. The schema can tell that a `known_outcome` is well-formed; it cannot tell
whether `text_present("No such membr")` matches anything real. Those conditions
are hand-authored by a human at review, which is exactly when typos happen, and
the failure is silent: a business outcome quietly degrades into a hard failure
in production.

On its first run the harness reported `branch_code_not_submitted` as **declared
but never reached** — an outcome authored during review and never once proven
to fire. A suite that leaves any outcome unreached fails, even if every
scenario passed, because an outcome nothing can trigger is not a safety net.

Note the ~8s medians on two rows. That is the checkpoint timeout expiring, and
it is not failure in general that is slow — `our_typing_did_not_land` is a hard
failure too and returns in 367ms. The eight seconds is paid only where the
checkpoint can never be satisfied: a control was deleted, or the screen is one
nothing in the artifact describes. A slow load and a genuine failure are
indistinguishable until the clock runs out, so the wait is what tells them
apart. Anticipated failures are fast because an error alert satisfies the
"the app responded" checkpoint immediately and the outcomes classify it — so
naming a failure in `known_outcomes` makes it ~20x faster to detect, as well as
correctly classified. See REPORT §3 for the measured table.

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
  evals.py      scenario harness: consistency + outcome reachability
artifacts/  approved capabilities (+ artifact.schema.json at the root)
evals/      scenario suites, one per capability
evidence/   real transcripts from every scenario
tests/      136 tests
scripts/    the original spike that de-risked a11y perception and CDP handoff
cli.py      discover / review / replay / eval
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success, **or** a business outcome — the caller got a real answer |
| 1 | hard failure — the surface is not what the artifact says |
| 2 | bad usage (missing key, unknown parameter, draft artifact) |
| 3 | refused by policy — distinct from a failure, and never escalated |
