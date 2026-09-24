# Report

A record-once / replay-many system for driving legacy bank UIs that have no
API. An LLM discovers a task once; the run becomes a typed, versioned artifact;
a no-model engine replays it deterministically, wrapped in safety, evidence and
human handoff.

> **LLM = compiler** (run once, expensive). **Artifact = compiled program.**
> **Replay = runtime** (run often, cheap, no model in the loop).

Implemented and exercised by 136 tests. [`/evidence`](evidence/README.md) is
from real runs, not illustrations.

---

## 1. Architecture

Single process, file-backed, synchronous. No queues or services: the problem
does not need them, and the parts genuinely hard to change later — the schema
and the driver seam — got the attention instead.

```
goal ─▶ DISCOVERY (LLM, once) ─▶ RECORDER (no LLM) ─▶ ARTIFACT ─▶ review ─▶ approved
        observe/decide/act        verified ladder,        │
                                  proposed checkpoint     ▼
                                              REPLAY (no LLM, forever)
                                                          │
                          Success │ BusinessOutcome │ Refused │ HardFailure ─▶ handoff
```

Everything touching a surface goes through one wall:

```python
class Driver(ABC):
    def navigate(url); def observe() -> Observation
    def click(target: TargetLadder); def type(target, text); def read(target)
    def check(condition: Condition, timeout_ms, values) -> CheckResult
    def probe(locator) -> Box | None;  def screenshot(path)
```

**Targets are always semantic**, never CSS or XPath. Translating a ladder into
surface mechanics is the driver's private business, which is what keeps the
artifact independent of how it is executed.

Two verbs beyond the obvious ones, both deliberate. `check` lives here because
the `Condition` vocabulary is surface-independent while *answering* one is
surface-specific; putting it above the wall would mean replay reaching around
the seam. `probe` answers "does this locator resolve, and to what" without
acting — how the recorder verifies rungs at record time.

**Perception is the accessibility tree as text**, not screenshots: the only
perception model that honestly extends to desktop, and cheap enough to send to
a model every step. One `aria_snapshot(boxes=True)` yields the plain tree, the
tree with bounding boxes (rung-4 coordinates, free), and a ref-annotated tree
for the model.

**The model's whole vocabulary** is an intent, an element ref, sometimes text,
and a reason. It cannot write a locator, name a rung, or author a checkpoint —
a model asked to invent locators invents brittle ones, and it is not there at
replay time to fix them.

**Why refs.** The target app has a field with no label, id or aria-label:
anonymous in the tree. A model restricted to naming things could not refer to
it at all. So elements carry handles (`e8`) that live for one observation and
are never persisted; only the ladder derived from one reaches the artifact.

---

## 2. Artifact schema

Pydantic in [`core/schema.py`](core/schema.py); the generated JSON Schema is
committed at [`artifact.schema.json`](artifact.schema.json), so a calling agent
can review the contract without reading our code.

```
Artifact
├── schema_version, capability_id, name, description
├── product_identity      "CoreBankPro@4" — tags "same app"
├── target                THE TENANT SLOT: entry_url, allowed_origins,
│                         allowed_routes, allowed_actions, overrides
├── parameters[]          typed, each with sensitive: bool
├── outputs[]             typed
├── steps[]               action · target: TargetLadder · value_ref ·
│                         extract_to · checkpoint · risk: safe|irreversible
├── known_outcomes[]      name + detect: Condition + classification
├── recoveries[]          detect + dismiss/wait_retry
├── status                draft | approved
└── provenance            discovered_by, evidence_ref, approved_by
```

**`Condition` is one vocabulary serving two jobs** — step checkpoints *and*
outcome detection. Built once, tested once, and it means "did this step work"
and "what did the application say" are asked in the same terms.

**`value_ref`, never a literal.** Every consumed value is a reference
(`params.*`, `target.*`, `outputs.*`) validated by regex; storing `"12345"`
fails validation. Redaction stops being a policy someone must remember and
becomes a shape the file cannot take. A `sensitive` parameter also may not
carry an `example` — the likeliest way a real value reaches a committed file.

**`target` is a separable slot**, so a second tenant is a thin overlay rather
than a re-recording.

**Cross-field validation turns runtime mysteries into load-time errors:**
references must resolve, an output cannot be consumed before the step that
reads it, the entry URL must sit inside the allowlist, and an `approved`
artifact must declare at least one `known_outcome`.

### How each control is identified

| Rung | Kind | Breaks when |
|---|---|---|
| 1 | a11y role + name | the accessible name changes |
| 2 | relational | the visible caption changes |
| 3 | structural | a control is inserted before it |
| 4 | coordinates | anything moves |

**Rungs 2–4 use geometry and reading order, not selectors.** "Same row" means
"shares a visual line", computed from bounding boxes — the same information an
OS accessibility API exposes, so the strategy ports to desktop instead of being
a web trick.

**Only verified rungs are recorded.** For each candidate the recorder asks the
live surface, through `probe`: does this resolve to exactly one element, and is
it *the same* element the model chose? Anything else is discarded. An artifact
therefore holds no rung that was merely plausible — which matters because a
fallback is only used once something has already gone wrong, the worst moment
to discover it never worked.

The `robustness_note` is generated from what survived and records what did
*not* — e.g. *"NO RUNG 1: this control has no accessible name at all… the
relational rung is therefore load-bearing rather than a fallback."*

### draft → approved  *(stretch goal: confidence & approval)*

Discovery walks one path, so it emits `draft` with no `known_outcomes` — it has
never seen "no such member" and cannot invent it. `cli.py review` reports what
only a human can decide: checkpoints that assume the happy path, missing
failure branches, and steps that might commit. On the write flow a human
resolved the last one the way only a human could — opening a *form* commits
nothing, so `click_4` stayed safe; `click_6` ('Confirm') creates an account and
was marked irreversible. The schema refuses `approved` until outcomes exist.

---

## 3. Determinism & error handling

Replay loads an approved artifact, binds typed parameters, and walks the steps
with no model anywhere: resolve the ladder top-down, act, wait on the
checkpoint, classify.

**Deterministic does not mean blind.** Every step has a checkpoint and every
wait is on a *stated condition*. Nothing sleeps a guessed interval — a blind
sleep is either too short and flaky or too long and slow, and never evidence
that anything happened. Measured against a 2.5s artificial delay, that step
waited 2,558 ms while every other returned in 1–21 ms.

**Ambiguity is a fallthrough, not a coin flip.** A rung resolves only on
exactly one visible match; acting on "whichever came back first" is not
deterministic, it is lucky.

| Result | Meaning | Exit | Escalates? |
|---|---|---|---|
| `Success` | checkpoints held, outputs extracted | 0 | — |
| `BusinessOutcome` | a real answer that is not the happy one | **0** | no |
| `Refused` | policy forbade the action | 3 | **never** |
| `HardFailure` | the surface is not what the artifact says | 1 | yes |
| `Recoverable` | known interstitial or slow load | — | handled in-loop; never reaches the caller |

**A business outcome is not a failure.** "No such member" is the bank answering
the question asked. It returns cleanly, the run stops where the answer arrived
rather than failing at the next step, and the process exits 0.

**What makes that possible** is separating "did the application respond" from
"what did it say". The checkpoint after a submit asserts only `any_of(member
panel, any alert)`; deciding what the response *meant* is the outcomes' job.
Narrow that checkpoint to the happy path and every business outcome becomes a
step failure — which is why the review tool flags exactly that pattern.

**Outcomes are read off the screen, never from the input.** The `if/else`
deciding what `99999` means lives in the application. A test parses
`replay.py`, strips comments and docstrings, and asserts the executable code
contains no `99999`, no `"Member Detail"`, no `member_id`. Duplicating the
app's rules would make the taxonomy a fiction — reporting our own guess back to
ourselves.

**Same mechanism, opposite classification.** Both arrive as `role="alert"`:
*"No such member"* is a `BusinessOutcome` (the bank answered); *"Please enter a
Member ID"* is a `HardFailure`, because our typing never landed. Dressing the
second up as a business outcome would hide a defect. Validation errors,
not-found, permission denials, unexpected dialogs, session expiry, slowness and
outright app errors are each handled and each reproducible from the target app
(see the README's flag table).

**Session expiry escalates rather than recovering.** Declared in
`known_outcomes`, so it is named and caught in ~400 ms. Classified
`hard_failure`, not `recoverable`, for a specific reason: recovering means
re-authenticating, which means credentials, which the automation must never
handle. A one-click "resume" would be recoverable; a sign-in form is not.

**Hard failures are debuggable on purpose** — step, expected, observed,
screenshot:

```
expected: button 'Search' is on screen
observed: url=...?drift=2 | controls present: textbox 'Member ID', textbox
```

**What waiting costs.** Measured over 20 runs each, it is not failure that is
slow but *unanticipated* failure. Four of six hard failures return under
600 ms; only two cost ~8.3 s — the two where the checkpoint can never be
satisfied, because a slow load and a genuine failure are indistinguishable
until the clock runs out. Everything else is fast because an error alert
satisfies the "responded" checkpoint immediately and the outcomes classify it
instantly. **Naming a failure in `known_outcomes` makes it ~20× faster to
detect** — a concrete argument for the review step beyond correctness. p99 is
therefore set by the timeout, not the application; the timeout is per-step in
the artifact and tunable per capability.

### Measured, not asserted  *(stretch goal: multi-run stability)*

`cli.py eval <capability> --runs 20` replays a declared scenario suite and
scores it. Both capabilities are **11/11 and 7/7 consistent at 20 runs with
every declared outcome reached**.

It measures consistency, and something else that closes a gap nothing else can:
**outcome reachability**. The schema validates that a `known_outcome` is
well-formed but cannot know whether `text_present("No such membr")` matches
anything the app renders. Those conditions are hand-authored at review —
exactly when typos happen — and the failure is silent, because the outcome
never matches and a clean business outcome degrades into a `HardFailure`. The
harness found precisely that on its first run: `branch_code_not_submitted`,
declared but never reached. A suite leaving any outcome unproven fails even
when every scenario passes.

So `approved` now means a human authored the failure branches *and* the machine
confirmed they fire. It does not mean the branches are the right ones.

### Drift

`?drift=1` renames the accessible label while leaving the visible caption — what
a tenant or localisation does. The flow keeps working and records which rung
saved it: *"Resolved at rung 2 — after 2 sturdier rung(s) failed"*, with the
full fallback trail. `?drift=2` removes a control outright: drift a ladder must
*not* absorb, and it correctly stops. A fallback that quietly found something
else would be worse than failing.

---

## 4. Heterogeneity & multi-tenant

**Design only, deliberately not built** — the brief gives negative credit for
multi-tenant plumbing, and an unused abstraction is a liability.

**A different surface.** The `Driver` seam is the whole answer: a
`DesktopDriver` satisfies the same verbs against an OS accessibility API and
nothing above the wall changes. Three choices keep that true rather than merely
claimed — perception is *already* an accessibility tree (what those APIs
return); rungs 2–4 use geometry, not DOM queries, so resolution ports
unchanged; and rung 4 exists because a surface may have no queryable tree at
all. `TargetSurface.surface` already distinguishes `web` from `desktop`.

**A second tenant.** An artifact is a base capability plus a thin overlay —
deltas only: a URL, a renamed label, a route policy. `product_identity` tags
"same application", and everything environment-specific already lives in the
separable `target` slot. `--entry-url` demonstrates the mechanism today: the
same approved artifact runs against a different URL with no re-recording, which
is how the drift and handoff scenarios are driven.

**Checkpoints double as drift detectors.** A base replayed at a new tenant does
not fail vaguely — it fails at a named step with an expected and an observed,
which is the signal needed to write the overlay. If many checkpoints fail, that
is the signal to record a fresh base instead.

Not built: overlay merging, a tenant registry, concurrent tenants.

---

## 5. Escalation & handoff

A `HardFailure` can stop or ask a person. With an operator configured it asks,
and **the person drives the same live browser session** — not a fresh copy.

That works because of a first-milestone decision: Chromium runs as its own
process with an open CDP debug port, and a driver is one client attached to it.
Handing over is `detach()`; taking back is `reattach()`. Because CDP is the
browser's native remote protocol, the second controller can equally be a
person's own browser pointed at the port.

```
automation ──detach──▶  [ live browser, state intact ]  ◀──attach── human
```

Four things make it a handoff rather than a restart:

- **An intervention request, not an error message** — what was attempted, which
  step, expected, observed, a screenshot, and the URL to take over at, written
  to evidence as JSON so any channel can deliver it. It carries the model's own
  stated reason from discovery, resurfacing when a human needs the context.
- **An ownership ledger.** One party holds the session at a time; transferring
  to the current owner raises rather than quietly passing. "Who was driving" is
  the first question after an incident.
- **Re-observation on resume, never memory.** The operator may have navigated
  anywhere, so the automation looks first — and skips the blocked step if they
  already completed it. Acting on a remembered position after someone else has
  been driving is how automation double-submits a payment.
- **A diff of what the human did.** "The operator fixed it" is not an audit
  trail; the tree before and after is diffed into `appeared: heading Member
  Detail`, `gone: button Override`.

Escalation is bounded — an operator who cannot fix it on the second attempt
will not on the twentieth, and a loop that keeps paging someone is its own
outage.

**A refusal never escalates**, and a test asserts the operator is never called.
Escalating a policy refusal would mean asking a person to do by hand the thing
the allowlist just prevented — the safety control becoming a routing step
toward defeating itself. That is why `Refused` is a distinct result type.

---

## 6. Safety

Three mechanisms, each placed where it cannot be bypassed rather than where it
was convenient.

**The allowlist lives inside the Driver** — not in replay, not in the CLI. A
check in the caller is a check the next caller forgets. It constrains origins,
**routes**, and action types; the middle one is the one people skip. The repo
includes `app/admin.html`: a real, reachable page on the *same origin* with a
"Close Account" button. Blocking `evil.example.com` proves little, because no
flow was going to navigate there by accident; the neighbouring admin console is
the realistic hazard, and it is refused by route. An allowlist rather than a
blocklist, because a blocklist is a list of the harm you already thought of.

**Irreversible actions require explicit confirmation.** The Confirm step is
`risk: irreversible`; replay stops *at* it, having completed the harmless steps
before. Why confirmation rather than blocking: the capability exists to be
used, and a system that refuses all irreversible work is not a system for
operating a bank. The default is refusal — it should need someone to say yes,
not need someone to remember to say no — but authorisation is a normal,
explicit, recorded act, per-run and never persisted. *Which* steps are
irreversible is a human judgement made at review, because nothing observable
distinguishes a commitment from a navigation.

**Credentials are out of scope, structurally.** No step targets a credential
field, no parameter is declared for one, and `value_ref` resolution has nowhere
to read one from. When the app demands re-authentication the run escalates and
the person types their own credentials into their own browser. The safest way
to handle a class of secret is to build a system that cannot represent it.

**Redaction is structural.** The artifact holds `value_ref`, so a sensitive
value has nowhere to be written; a `sensitive` parameter may not carry an
`example`; the evidence logger masks declared-sensitive values at the single
choke point every line passes through; and the assembler refuses to emit an
artifact containing any caller value. That last gate caught two live leaks
nothing else would have — the **goal text** and the **model's own stated
reasons**, both copying `12345` into prose. Both are now rewritten as
`{member_id}`, which reads better than a mask. Locators are deliberately *not*
rewritten: an `anchor_text` is page text, so substituting would break it — if a
value reaches one, the artifact is refused, because that ladder would be welded
to one caller's data.

**Limits.** Redaction masks literals, so a value the app reformats (12345 →
12,345) would survive. Screenshots are pixels and cannot be masked at all; they
are kept because a failure screenshot is the point and the app is a local fake.

---

## 7. Cuts

### Stretch goals: two taken, four declined

The brief asks for at most one or two. Two were taken because they reinforce
each other: **multi-run stability** (§3) and **confidence & approval** (§2).
Together they answer a question neither answers alone — *is this capability
trustworthy enough to run unattended?*

Declined deliberately: an **agent-facing catalog** is mostly plumbing over a
contract that already exists as JSON Schema; **code generation** emits the
artifact in a second format, doubling what can drift; **canonicalization /
cross-tenant reuse** is designed in §4 and would have been third. **Assisted
fallback** I would refuse on principle rather than time — putting a model back
into the replay path reintroduces exactly the non-determinism the artifact
exists to eliminate, at the moment the system is least sure what is going on.

### What was cut

- **Operator console → a CLI prompt.** The mechanism beneath — detach, second
  controller on the same session, reattach, re-observe — is real and tested.
  Only the UI is mocked.
- **Desktop driver → seam only.** Writing a second implementation would have
  cost the time that went into the error taxonomy, and the seam is the part
  that had to be right.
- **Multi-tenant → design only.** The slots exist and `--entry-url` uses them.
- **The write capability is thin**, existing to give the safety layer a real
  irreversible action to guard. It does not set account type, which needs a
  `select` verb the vocabulary lacks.
- **No retry on hard failures.** Retrying an action of unknown reversibility is
  not obviously safer than stopping.
- **Tests are typed where it counts, not exhaustive.** 136 tests on the
  schema's guarantees, ladder fallback and taxonomy classification; most
  negative tests break a rule deliberately and assert rejection.

### If there were more time

A `select` verb and a richer action vocabulary; overlay resolution so the
multi-tenant design is exercised rather than described; a real operator
console; and extending the eval harness to the *discovery* half — running one
goal repeatedly to measure how often the model produces an equivalent artifact.
The harness measures the runtime today; that number would say whether the
compiler is dependable rather than merely working, and it is the one
measurement still missing.
