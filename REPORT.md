# Report

A record-once / replay-many system for driving legacy bank UIs that have no
API. An LLM discovers how to do a task once; the run is recorded as a typed,
versioned artifact; a no-model engine replays it deterministically in
production, wrapped in safety, evidence and human handoff.

> **LLM = compiler** (run once, expensive). **Artifact = compiled program.**
> **Replay = runtime** (run often, cheap, no model in the loop).

Everything below is implemented and exercised by 136 tests. The evidence in
[`/evidence`](evidence/README.md) is from real runs, not illustrations.

---

## 1. Architecture

Single process, file-backed, synchronous. No queues, no services, no workers.
The problem does not need them, and the parts that would be hard to change
later — the schema and the driver seam — are the parts that got the attention.

```
            goal in plain language
                     │
    ┌────────────────▼──────────────────┐
    │  DISCOVERY  (core/agent.py)       │   LLM, once
    │  observe ─▶ decide ─▶ act ─▶ loop │
    └────────────────┬──────────────────┘
                     │ thin intent: "type into e8"
    ┌────────────────▼──────────────────┐
    │  RECORDER   (core/recorder.py)    │   no LLM
    │  build a verified ladder,         │
    │  propose a checkpoint             │
    └────────────────┬──────────────────┘
                     │
              ARTIFACT (.json)  ◀── human review ──▶ approved
                     │
    ┌────────────────▼──────────────────┐
    │  REPLAY     (core/replay.py)      │   no LLM, forever
    │  resolve ─▶ act ─▶ verify ─▶ class│
    └────────────────┬──────────────────┘
                     │
        Success │ BusinessOutcome │ Refused │ HardFailure ─▶ handoff
```

Everything that touches a surface goes through one wall:

```python
class Driver(ABC):
    def navigate(self, url) -> None
    def observe(self) -> Observation
    def click(self, target: TargetLadder) -> Resolution
    def type(self, target: TargetLadder, text: str) -> Resolution
    def read(self, target: TargetLadder) -> Resolution
    def check(self, condition: Condition, timeout_ms, values) -> CheckResult
    def probe(self, locator) -> Box | None
    def screenshot(self, path) -> Path
```

**Targets are always semantic**, never CSS or XPath. Translating a ladder into
surface mechanics is the driver's private business, which is what keeps the
artifact independent of how it is executed.

Two verbs beyond the obvious five, both deliberate. `check` is here because the
`Condition` vocabulary is surface-independent while *answering* one is
surface-specific; putting it above the wall would mean replay reaching around
the seam. `probe` answers "does this locator resolve, and to what" without
acting, which is how the recorder verifies rungs at record time.

**Perception is the accessibility tree as text**, not screenshots. It is the
only perception model that honestly extends to desktop, it makes rung-1
targeting free, and it is cheap enough to send to a model on every step. One
`aria_snapshot(boxes=True)` call yields three views of the same moment: the
plain tree, the tree with bounding boxes (rung-4 coordinates, captured for
free), and a ref-annotated tree for the model.

**The model's entire vocabulary** is an intent, an element ref, sometimes text,
and a reason. It cannot write a locator, name a rung, or author a checkpoint.
Two reasons: a model asked to invent locators invents brittle ones, and the
model is not there at replay time to fix them.

### Why refs

The target app has a field with no label, no id and no aria-label — anonymous
in the accessibility tree. A model restricted to naming things could not refer
to it at all. So every element in the observation carries a handle (`e8`) and
the model says "type into e8". Refs live for one observation and are never
persisted; only the ladder derived from one ever reaches the artifact.

---

## 2. Artifact schema

Pydantic models in [`core/schema.py`](core/schema.py); the generated JSON
Schema is committed at [`artifact.schema.json`](artifact.schema.json) so a
calling agent can review the contract without reading our code.

```
Artifact
├── schema_version, capability_id, name, description
├── product_identity          "CoreBankPro@4" — tags "same app"
├── target                    THE TENANT SLOT: entry_url, allowed_origins,
│                             allowed_routes, allowed_actions, overrides
├── parameters[]              typed, each with sensitive: bool
├── outputs[]                 typed
├── steps[]
│   ├── action                exactly the Driver verbs
│   ├── target: TargetLadder  ordered Locators + robustness_note
│   ├── value_ref             "params.member_id" — NEVER a literal
│   ├── extract_to
│   ├── checkpoint            Condition + description + timeout
│   └── risk                  safe | irreversible
├── known_outcomes[]          name + detect: Condition + classification
├── recoveries[]              detect + dismiss/wait_retry
├── status                    draft | approved
└── provenance                discovered_by, evidence_ref, approved_by
```

### Four decisions worth defending

**`Condition` is one vocabulary serving two jobs.** The same predicate language
expresses step checkpoints *and* outcome detection. Built once, tested once,
and — more importantly — it means "did this step work" and "what did the
application say" are asked in exactly the same terms.

**`value_ref`, never a literal.** Every value a step consumes is a reference
(`params.*`, `target.*`, `outputs.*`) validated by regex; storing `"12345"`
fails validation. Redaction stops being a policy someone must remember and
becomes a shape the file cannot take. A `sensitive` parameter additionally may
not carry an `example`, which is the likeliest way a real value reaches a
committed file.

**`target` is a separable slot.** Everything environment-specific lives there
and nowhere else, so a second tenant is a thin overlay rather than a
re-recording.

**Cross-field validation turns runtime mysteries into load-time errors.**
References must resolve to declared parameters; an output cannot be consumed
before the step that reads it; the entry URL must sit inside the allowlist; an
`approved` artifact must declare at least one `known_outcome`.

### How each control is identified

A `TargetLadder` is an ordered list of locators plus a written argument for the
ordering:

| Rung | Kind | Survives | Breaks when |
|---|---|---|---|
| 1 | a11y role + name | restyling, DOM refactors, class churn | the accessible name changes |
| 2 | relational | renames, restyling, column reordering | the visible caption changes |
| 3 | structural | renames and restyling | a control is inserted before it |
| 4 | coordinates | nothing much | anything moves |

**Rungs 2–4 are implemented with geometry and reading order, not selectors.**
"Same row" means "shares a visual line", computed from bounding boxes. That is
not fastidiousness: it is the same information an OS accessibility API exposes,
so the strategy ports to desktop instead of being a web trick.

**Only verified rungs are recorded.** For each candidate the recorder asks the
live surface two questions through `probe`: does this resolve to exactly one
element, and is it *the same* element the model chose? Anything else is
discarded. An artifact therefore contains no rung that was merely plausible —
which matters because a fallback is only ever used when something has already
gone wrong, the worst possible moment to discover it never worked.

The `robustness_note` is generated from what survived, and records what did
*not*:

> Identifying the textbox **(unnamed)**. 3 rung(s) verified against the live
> surface at record time. **NO RUNG 1: this control has no accessible name at
> all**, so role+name cannot address it. The relational rung is therefore
> load-bearing rather than a fallback. **Rung 2** — relational: the textbox
> sharing a row with the caption 'Branch Code'. **Rung 3** — structural:
> position 1 in reading order, which breaks if a control is inserted before it.
> **Rung 4** — coordinates: last resort, viewport-bound.

### draft → approved

Discovery walks exactly one path, so it emits `draft` with no
`known_outcomes` — it has never seen "no such member" and cannot invent it.
`python cli.py review <capability>` reports what only a human can decide:

```
NEEDS A HUMAN:
  - Happy-path checkpoints on click_3, click_4, click_6. These assert the one
    outcome discovery happened to see. Widen them to any_of(that marker, an
    alert being present) ...
  - No known_outcomes. Discovery only walked the happy path, so every business
    outcome would be reported as a hard failure.
  - Possibly irreversible, recorded as safe: click_4 ('Open Sub-Account'),
    click_6 ('Confirm'). Discovery cannot tell a commitment from a search by
    watching it succeed.
```

On the write flow a human resolved that last one the way only a human could:
opening a *form* commits nothing, so `click_4` stayed safe; `click_6`
('Confirm') creates an account and was marked irreversible. The schema refuses
to mark an artifact `approved` until outcomes exist, so approval means
something.

---

## 3. Determinism & error handling

Replay loads an approved artifact, binds typed parameters, and walks the steps
with no model anywhere. Per step: resolve the ladder top-down, act, wait on the
checkpoint, then classify.

**Deterministic does not mean blind.** Every step has a checkpoint and every
wait is a wait *on a stated condition*. Nothing sleeps a guessed interval: a
blind sleep is either too short and flaky or too long and slow, and it is never
evidence that anything happened. Measured, on a 2.5s artificial delay:

```
click_3   Checkpoint: passed after 2558 ms      ← waited for the condition
others    Checkpoint: passed after 1–21 ms      ← no fixed pause anywhere
```

**Ambiguity is a fallthrough, not a coin flip.** A rung resolves only when it
matches exactly one visible element. Acting on "whichever one came back first"
is not deterministic, it is lucky.

### The taxonomy

| Result | Meaning | Exit | Escalates? |
|---|---|---|---|
| `Success` | checkpoints held, outputs extracted | 0 | — |
| `BusinessOutcome` | the application gave a real answer that is not the happy one | **0** | no |
| `Refused` | policy forbade the action | 3 | **never** |
| `HardFailure` | the surface is not what the artifact says | 1 | yes |
| `Recoverable` | known interstitial or slow load | — | handled in-loop; never reaches the caller |

**A business outcome is not a failure.** "No such member" is the bank answering
the question it was asked. It is returned cleanly, the run stops where the
answer arrived rather than failing at the next step, and the process exits 0.

**The mechanism that makes that possible** is separating "did the application
respond" from "what did it say". The checkpoint after a submit asserts only
`any_of(member panel, any alert)`. Deciding what the response *meant* is the
outcomes' job. Narrow that checkpoint to the happy path and every business
outcome becomes a step failure — which is why the review tool flags exactly
that pattern.

**Outcomes are read off the screen, never from the input.** The `if/else` that
decides what `99999` means lives in the application. There is a test that
parses `replay.py`, strips comments and docstrings, and asserts the executable
code contains no `99999`, no `"Member Detail"`, no `member_id`. Duplicating the
app's rules would make the whole taxonomy a fiction — we would be reporting our
own guess back to ourselves.

**Same mechanism, opposite classification.** Both of these arrive as a
`role="alert"`:

- *"No such member"* → `BusinessOutcome`. The bank answered.
- *"Please enter a Member ID"* → `HardFailure`. Our typing never landed. That
  is our bug, and dressing it up as a business outcome would hide a defect.

**Hard failures are debuggable on purpose** — step, expected, observed,
screenshot:

```
expected: button 'Search' is on screen
observed: url=...?drift=2 | controls present: textbox 'Member ID', textbox
```

### The runtime conditions, one by one

The brief names the exceptional states that matter in this environment. Each is
handled, and the classification differs on purpose:

| Condition | How it surfaces here | Classified |
|---|---|---|
| validation error | empty Member ID / Branch Code, unparseable deposit | **hard failure** — the app is telling us *our* input never landed |
| "record not found" | member `99999` | business outcome |
| permission denial | member `00000`, and the deposit approval limit | business outcome |
| unexpected dialog | `?maintenance=1` (declared) / `?blocker=1` (not) | recoverable / hard failure → escalation |
| **session / timeout expiry** | `?session_expires=1` | **hard failure → escalation** |
| transient slowness | `?slow=2000` | waited out, then success |
| outright app error | `?drift=2`, a control gone | hard failure |

Two rows are worth dwelling on.

**Validation errors are ours, not the bank's.** "Please enter a Member ID"
arrives through the identical `role="alert"` mechanism as "No such member", and
it would be easy to bucket them together. But one is the application answering
a question and the other means our type step silently did nothing. Calling the
second a business outcome would hide a defect behind a legitimate-looking
result.

**Session expiry escalates rather than recovering.** It is declared in
`known_outcomes`, so it is named and detected in ~400 ms rather than by waiting
out the checkpoint. It is classified `hard_failure` — not `recoverable` — for a
specific reason: recovering means re-authenticating, re-authenticating means
credentials, and the automation must never handle those. A one-click "resume"
would have been recoverable; a sign-in form is not. So the target app's expiry
screen demands an Operator ID and a password, and the only correct response is
to hand the live session to a person who has them. The operator's credentials
never enter the automation's context, and the transcript records
`filled Operator ID; filled Password; clicked Sign In` without either value.

### What waiting costs

Measured medians over 20 runs each, and the split is not where you would guess.
It is **not** failure that is slow — it is *unanticipated* failure:

| Scenario | Result | Median |
|---|---|---|
| `member_not_found` | business outcome | 317 ms |
| `our_typing_did_not_land` | hard failure | **367 ms** |
| `branch_code_did_not_land` | hard failure | **367 ms** |
| `blocked_without_confirmation` | hard failure | **470 ms** |
| `unparseable_deposit` | hard failure | **533 ms** |
| `?slow=2000` (a real 2s delay) | success | 2,493 ms |
| `drift_too_large_to_absorb` | hard failure | **8,216 ms** |
| `unknown_blocker_stops_the_run` | hard failure | **8,442 ms** |

Four of the six hard failures return in under 600 ms. Only two cost eight
seconds, and they are the two where **the checkpoint can never be satisfied**:
a control was deleted, or the application showed a screen nothing in the
artifact describes. There the timeout has to expire, because a slow load and a
genuine failure are indistinguishable until the clock runs out — the timeout
*is* what tells them apart, and giving up early would trade a bounded latency
for flaky false failures.

Everything else is fast because the checkpoint after a submit asserts only that
the application *responded*. An error alert satisfies it immediately, and
classification then falls to the outcomes, which evaluate instantly. That
widening was introduced so a business outcome would not be misreported as a
step failure; the speed is a second dividend of the same decision. **Naming a
failure in `known_outcomes` makes it roughly twenty times faster to detect**,
which is a concrete argument for the review step beyond correctness.

So the honest statement of the cost is narrow: a capability pays its full
checkpoint budget only when it meets a state nobody anticipated. Two
consequences worth stating rather than discovering later. Operationally, p99 is
set by the timeout rather than by the application, and capacity planning should
assume an unrecognised-state failure costs the full budget. For the eval
harness, those two scenarios dominate its runtime — at 20 runs they are around
five of its eight minutes.

The timeout is per-step and lives in the artifact, so it is tunable per
capability rather than globally. It was not tuned here: 8s is a defensible
default for a legacy UI, and picking a smaller number to make the demo look
brisk would be optimising the wrong thing.

### Measured, not asserted

`python cli.py eval <capability> --runs 20` replays a declared scenario suite N
times and scores it. Both capabilities are **10/10 and 6/6 scenarios consistent
at 20 runs, with every declared outcome reached**.

It measures two things. Consistency is the obvious one. The other is
**outcome reachability**, and it closes a gap nothing else in the system can:
the schema validates that a `known_outcome` is well-formed, but cannot know
whether `text_present("No such membr")` matches anything the application
renders. Those conditions are hand-authored by a human at review time — exactly
when typos happen — and the failure mode is silent, because the outcome simply
never matches and a clean business outcome degrades into a `HardFailure`. That
is the brief's central mistake re-entering through the back door after the
whole taxonomy was built to prevent it.

The harness found exactly that on its first run: `branch_code_not_submitted`,
authored during the M3 review, reported as **declared but never reached**. A
suite that leaves any outcome unproven fails even when every scenario passes.

This tightens the lifecycle: `approved` now means a human authored the failure
branches *and* the machine confirmed they fire. It does not mean the branches
are the right ones — a human still decides that.

### Drift

`?drift=1` renames the app's accessible label while leaving the visible caption
alone — what a tenant or a localisation does. The recorded flow keeps working
and says which rung saved it:

```
Resolved at: rung 2 — after 2 sturdier rung(s) failed
Fallback trail: ["rung 1 (a11y_role_name): no visible element with role=textbox
                 name='Member ID'",
                 "rung 1 (a11y_role_name): no visible element with name='Member ID'",
                 "rung 2 (relational): ok"]
```

`?drift=2` removes a control outright. That is drift a ladder must *not*
absorb, and it correctly stops with a hard failure. A fallback that quietly
finds something else would be worse than failing.

---

## 4. Heterogeneity & multi-tenant

**Design only. Deliberately not built** — the brief gives negative credit for
building multi-tenant plumbing, and an unused abstraction is a liability.

### A different kind of surface

The `Driver` seam is the whole answer. A `DesktopDriver` satisfies the same
verbs against an OS accessibility API (UIA on Windows, AX on macOS) and nothing
above the wall changes: the artifact, the recorder, replay, the taxonomy and
the handoff are all written against ladders and conditions.

Three choices were made specifically to keep that true rather than merely
claimed:

- **Perception is already an accessibility tree**, which is what those APIs
  return. A screenshot-based design would have to be rebuilt from scratch.
- **Rungs 2–4 use geometry and reading order**, not DOM queries, so the
  resolution strategy ports unchanged.
- **Rung 4 exists at all** because a surface may have no queryable tree. On the
  web it is the last resort; on some desktop surfaces it is the only option,
  and it is captured for free with perception.

`TargetSurface.surface` already distinguishes `web` from `desktop`, so an
artifact can say which kind of thing it was recorded against.

### The same app at a second tenant

An artifact is a **base capability plus a thin per-tenant overlay** — deltas
only: a URL, a renamed label, a different route policy. `product_identity`
(`CoreBankPro@4`) tags "this is the same application", and everything
environment-specific already lives in the separable `target` slot, so nothing
above it is hardwired. `target.overrides` is the slot those deltas go in, and
`value_ref` resolution already reads through it.

`--entry-url` demonstrates the mechanism today: the same approved artifact runs
against a different URL with no re-recording, which is how the drift and
handoff scenarios are driven.

**Checkpoints double as drift detectors.** Replaying a base capability at a new
tenant does not fail vaguely — it fails at a named step with an expected and an
observed, which is precisely the signal needed to write the overlay. If the
divergence is large enough that many checkpoints fail, that is the signal to
record a fresh base instead, and the report says which.

What is *not* built: overlay resolution and merging, a tenant registry, and any
notion of running several tenants at once.

---

## 5. Escalation & handoff

A `HardFailure` can either stop or ask a person. With an operator configured it
asks, and **the person drives the same live browser session** — not a fresh
copy.

That works because of a decision made in the first milestone: Chromium is
launched as its own process with an open CDP debug port, and a driver is merely
one client attached to it. Handing over is `detach()`; taking back is
`reattach()`. Because CDP is the browser's native remote protocol, the second
controller can equally be a person's own browser pointed at the port.

```
automation ──detach──▶  [ live browser, state intact ]  ◀──attach── human
```

Four things make it a handoff rather than a restart:

**An intervention request, not an error message** — what was being attempted,
which step, expected, observed, a screenshot, and the URL to take over at,
written to evidence as JSON so any channel can deliver it. It carries the
model's own stated reason from discovery, resurfacing months later exactly when
a human needs the context.

**An ownership ledger.** One party holds the session at a time; transferring to
the current owner raises rather than quietly passing. Every transfer is
recorded with a reason, because "who was driving" is the first question after
an incident.

**Re-observation on resume, never memory.** The operator may have navigated
anywhere, so the automation looks first — and if the human already completed
the blocked step, it skips it. Acting on a remembered position after someone
else has been driving is how automation double-submits a payment.

**A diff of what the human did.** "The operator fixed it" is not an audit
trail. The accessibility tree before and after is diffed into
`appeared: heading Member Detail`, `gone: button Override`.

Escalation is bounded: an operator who cannot fix it on the second attempt will
not on the twentieth, and a loop that keeps paging someone is its own outage.

**A refusal never escalates.** That path is closed deliberately, with a test
asserting the operator is never called. Escalating a policy refusal would mean
asking a person to do by hand the thing the allowlist just prevented — the
safety control becoming a routing step toward defeating itself. This is the
reason `Refused` is a distinct result type rather than a kind of hard failure.

---

## 6. Safety

Three mechanisms, each placed where it cannot be bypassed rather than where it
was most convenient.

### The allowlist lives inside the Driver

Not in replay, not in the CLI. A check in the caller is a check the next caller
forgets. Because every action funnels through the seam, a flow physically
cannot act off-list, whatever its steps say and whatever a model decided
mid-run.

It constrains three things, and the middle one is the one people skip:

| | |
|---|---|
| origins | which hosts may be touched at all |
| **routes** | **which paths — a bank's admin console and its member search share an origin** |
| actions | which verbs — a read-only capability can be prevented from ever typing |

The repository includes `app/admin.html`: a real, reachable page on the same
origin with a "Close Account" button. Blocking `evil.example.com` proves very
little, because no flow was going to navigate there by accident. The
neighbouring admin console is the realistic hazard:

```
Refused(step=open_application)
  reason: navigation to '/admin.html' is outside the permitted routes
          ['/members.html'] — the origin is allowed but this page is not
```

An allowlist rather than a blocklist, because a blocklist is a list of the harm
you already thought of, and everything unanticipated is permitted by default.

### Irreversible actions require explicit confirmation

The write flow's Confirm step is `risk: irreversible`. Replay stops *at* it,
having completed the harmless steps before it:

| | Result |
|---|---|
| without `--confirm` | `HardFailure(step=click_6)` — "explicit confirmation before an irreversible action" |
| with `--confirm` | `Success({'new_account_number': 'SA-12345-00500'})` |

**Why confirmation rather than blocking outright**: the capability exists to be
used, and a system that simply refuses irreversible work is not a system for
operating a bank. The default is refusal — an irreversible action should need
someone to say yes, not need someone to remember to say no — but authorisation
is a normal, explicit, recorded act. The flag is per-run, never persisted, and
the authorised run is in the evidence like any other.

Which steps are irreversible is a **human** judgement, made at review. It has
to be: discovery watched both "Open Sub-Account" and "Confirm" succeed, and
nothing observable distinguishes a commitment from a navigation.

### Credentials are out of scope, structurally

The system has no way to enter a credential and no place to keep one. There is
no step in either artifact targeting a credential field, no parameter declared
for one, and `value_ref` resolution has nowhere to read one from. When the
target app demands re-authentication the run escalates, and the person who
takes the session over types their own credentials into their own browser — an
exchange the automation never observes.

This is the sharpest case of a general rule: the safest way to handle a class
of secret is to build a system that cannot represent it.

### Redaction is structural

- The artifact holds `value_ref`, so a sensitive value has nowhere to be
  written down. The schema rejects a literal.
- A `sensitive` parameter may not carry an `example`.
- The evidence logger masks the *values* of parameters the capability declares
  sensitive, at the single choke point every line passes through.
- The assembler refuses to emit an artifact containing any caller value, and
  inspects string leaves only — scanning raw JSON would flag a branch code of
  `001` inside a coordinate of `1001.0`, and a guard that cries wolf gets
  switched off.

That last gate caught two leaks on live runs that nothing else would have:
the **goal text** ("look up member 12345…") being copied into the description,
and the **model's own stated reasons** ("Enter the member ID 12345 into…")
being copied into step descriptions. Both are now rewritten in terms of the
parameter — `{member_id}` rather than `***`, which reads better and documents
the capability generally. Locators are deliberately *not* rewritten: an
`anchor_text` is page text, so substituting into it would break the locator. If
a value ever reaches one, the artifact is refused outright, because that ladder
would be welded to one caller's data.

A grep for `12345` across every committed transcript and event log returns
nothing. `.env` is gitignored and the key was never read into the working
context.

---

## 7. Cuts

Stated plainly, with the reasoning.

**Operator console → a CLI prompt.** `CliOperator` prints the intervention
brief and waits for a keypress while you attach a browser to the CDP URL. The
mechanism underneath — detach, second controller on the same session,
reattach, re-observe — is real and tested. Only the UI is mocked, because a
console would have been the most visible work and the least interesting.

**Desktop driver → seam only.** Built as an interface with one implementation
behind it. Writing a second one would have consumed the time that went into the
error taxonomy, and the seam is the part that had to be right.

**Multi-tenant → design only.** The slots exist (`target`, `overrides`,
`product_identity`) and are already used by `--entry-url`. Overlay merging and
a tenant registry are not built.

**The write capability is thin.** `open_sub_account` exists to give the safety
layer a genuinely irreversible action to guard. It does not set the account
type, because that needs a `select` verb the vocabulary does not have — a
natural extension, and honestly out of scope for a flow whose job is to be
guarded.

**No retry or backoff on hard failures.** Replay stops and escalates rather
than retrying, because retrying an action of unknown reversibility is not
obviously safer than stopping.

**Discovery emits one capability per run.** No attempt to generalise a flow
across several goals.

**Tests are typed where it counts, not exhaustive.** 136 tests concentrated on
the schema's guarantees, ladder fallback, and taxonomy classification. Most
negative tests deliberately break a rule and assert the system rejects it,
because those are the guarantees that would otherwise quietly rot.

### If there were more time

In order: a `select` verb and a richer action vocabulary; overlay resolution so
the multi-tenant design is exercised rather than described; a real operator
console; and extending the eval harness to the *discovery* half — running the
same goal repeatedly to measure how often the model produces an equivalent
artifact. The harness currently measures the runtime; that number would say
whether the compiler is dependable rather than merely working, and it is the
one measurement still missing.
