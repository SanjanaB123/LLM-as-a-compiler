# Evidence

Every run in this directory is real: a live model against the live app for the
discovery runs, and the deterministic engine against the live app for the rest.
Nothing here is illustrative or hand-written.

Each run directory holds:

| File | What it is |
|---|---|
| `transcript.md` | the human-readable account — what was seen, decided, and why |
| `events.jsonl` | the same run as structured records, for assertions and tooling |
| `failure-*.png` | screenshot taken at the moment of a hard failure |
| `intervention-*.json` | the handoff request, when one was raised |

**Sensitive values are masked before anything is written.** `member_id` is
declared sensitive by both capabilities, so it appears as `***` throughout —
including inside captured accessibility trees, where it would otherwise show up
as a field value, and inside generated text like the account number
`SA-***-00500`. `branch_code` is not declared sensitive and stays readable, so
the masking is visibly selective rather than a blanket redaction that would
make the evidence useless.

---

## Discovery — the model in the loop

| Run | What it shows |
|---|---|
| [discovery-20260918-165721](discovery-20260918-165721/transcript.md) | `lookup_member_balance` discovered in 5 steps. Step 2 is the point: the model reaches the **unnamed** Branch Code field, and the recorder works out that the only durable way back to it is "the textbox sharing a row with the caption 'Branch Code'". |
| [discovery-20260918-202638](discovery-20260918-202638/transcript.md) | `open_sub_account` discovered in 8 steps — the write flow, found by the same loop with no changes to the prompt. |

## Replay — deterministic, no model

| Run | Result | What it shows |
|---|---|---|
| [replay-20260918-171545](replay-20260918-171545/transcript.md) | `Success` | The happy path. Note `Model in the loop: none`. |
| [replay-20260918-201056](replay-20260918-201056/transcript.md) | `BusinessOutcome(member_not_found)` | **The most important file here.** "No such member" is returned cleanly, the run stops where the answer arrived, and the process exits 0. |
| [replay-20260918-171604](replay-20260918-171604/transcript.md) | `BusinessOutcome(permission_denied)` | A second business outcome through the same generic mechanism. |
| [replay-20260918-201017](replay-20260918-201017/transcript.md) | `HardFailure` | The counterpart: "Please enter a Member ID" means *our* typing never landed. Same alert mechanism as above, opposite classification. |
| [replay-20260918-201046](replay-20260918-201046/transcript.md) | `HardFailure` + screenshot | A control was removed (`?drift=2`). Reports the step, what was expected, and `controls present:` — what was actually on screen instead. |

## The targeting ladder under drift

| Run | What it shows |
|---|---|
| [replay-20260918-183207](replay-20260918-183207/transcript.md) | `?drift=1` renames the app's accessible label. Rung 1 fails twice, **rung 2 catches it**, and the run succeeds unchanged. The fallback trail is recorded verbatim. |

## Recoverables — handled in the loop, never surfaced

| Run | What it shows |
|---|---|
| [replay-20260918-200934](replay-20260918-200934/transcript.md) | An interstitial is dismissed and the run continues. Attributed to `click_3`, the step that raised it. |
| [replay-20260918-183211](replay-20260918-183211/transcript.md) | A 2.5s delay: the checkpoint waits **2558 ms** while every other step returns in single digits. A wait on a condition, not a sleep. |

## Safety

| Run | Result | What it shows |
|---|---|---|
| [replay-20260918-202745](replay-20260918-202745/transcript.md) | `HardFailure` | The irreversible Confirm step blocked for want of `--confirm`. It stops *at* the commitment, after the harmless steps. |
| [replay-20260918-202748](replay-20260918-202748/transcript.md) | `Success` | The same flow authorised. Output `SA-***-00500` — the member id masked inside a value the app generated. |
| [replay-20260918-202807](replay-20260918-202807/transcript.md) | `BusinessOutcome(approval_limit_exceeded)` | The bank declining a deposit over the operator's limit: a real answer, not a crash. |
| [replay-20260918-203021](replay-20260918-203021/transcript.md) | `Refused` | Navigation to `/admin.html` — an allowed origin, a forbidden route. Its own classification, never a hard failure, so it can never escalate to a human. |

## Escalation and handoff

| Run | What it shows |
|---|---|
| [handoff-20260918-204523](handoff-20260918-204523/transcript.md) | The full thread. Replay gets stuck on a "Supervisor Override Required" screen no recording ever saw, hands the **live browser session** to a second controller over CDP, that controller clears it, control returns, and the run finishes with the balance. The ownership ledger and a diff of what the human changed are both in the transcript; the request itself is in `intervention-click_3.json`. |

---

## What was pruned

Seven runs were removed as superseded duplicates: an aborted run with no
transcript, two discovery runs whose transcripts predate fixes to the wording
(they claimed fallbacks that had not happened), and four re-runs of scenarios
already represented here. Where a scenario was run twice, the later run is kept
because it reflects the corrected reporting.
