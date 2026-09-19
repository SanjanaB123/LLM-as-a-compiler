# Implementation Plan — Computer-Use Automation System

A record-once / replay-many system that lets an AI agent operate legacy bank UIs
that have no API. An LLM **discovers** how to do a task once; we record it as a
typed, versioned **artifact**; a no-LLM engine **replays** it deterministically
in production. Wrapped in safety, evidence, and human handoff.

> Mental model: **LLM = compiler (run once, expensive). Artifact = compiled
> program. Replay = runtime (run often, cheap, no model in the loop).**

---

## Guiding principles (from the brief)

- **Thin-but-real everything, not deep on a subset.** A complete end-to-end
  thread that touches every Section 3 requirement beats a polished fraction.
- **Deterministic must not mean blind.** The UI is stable; the hard part is
  runtime errors. Replay verifies every step and classifies failures.
- **Business outcome ≠ failure.** "No such member" is a legitimate result to
  return, not a crash. Conflating them is the brief's named #1 mistake.
- **Deliberately boring architecture.** Single process, file-backed, sync. No
  queues/services/clusters — the brief gives *negative* credit for that.
- **Design for heterogeneity; build one surface.** Abstractions must not paint
  us into a corner, but multi-tenant/desktop are design-only.

---

## Locked decisions (with the "why" — we must defend each)

| # | Decision | Why |
|---|----------|-----|
| 1 | **Local, legacy-hostile web app** as the target (table layout, no test IDs, non-semantic wrappers, real labels/roles) | Only way to author runtime errors on demand (not-found, timeout, denial); ToS-safe; forces real targeting instead of cheating with test IDs |
| 2 | **Two capabilities**: `lookup_member_balance` (read, the star) + `open_sub_account` (write, thin) | Read flow covers inputs/outputs/checkpoints/outcomes; write flow gives the safety layer a real irreversible action to guard |
| 3 | **Perceive via the accessibility tree as text** (not screenshots) | Only perception model that honestly extends to desktop; makes rung-1 targeting free; cheaper + more deterministic than vision |
| 4 | **Act via semantic targets** (role+name / relational / coords), never raw selectors | Keeps the flow surface-independent; the transcript reads sensibly |
| 5 | **The Driver seam**: abstract `Driver` verbs (`observe/click/type/read/navigate`); one real `WebDriver` (Playwright) | This wall is the literal answer to "seam between perceive/act and the recorded flow"; a `DesktopDriver` slots in later unchanged |
| 6 | **Model decides, recorder enriches** | LLM emits thin intent; our code computes the full targeting ladder + coords + proposed checkpoint. This is where the durable artifact is manufactured |
| 7 | **Targeting ladder** (sturdiest→fragile): a11y role+name → relational ("field after label X") → structural/text → coordinates | Simultaneously the drift-robustness answer (rung 1 survives restyle) and the no-DOM answer (rung 4 needs no DOM) |
| 8 | **Replay is a checker**: every step has a checkpoint; failures sort into Success / BusinessOutcome / Recoverable / HardFailure | The load-bearing error taxonomy; the most-graded behavior after the schema |
| 9 | **Python + Pydantic + Playwright** | Pydantic *is* the typed/versioned/validated schema for free; Playwright Python has everything we need (proven in spike) |
| 10 | **Redaction is structural**: artifact holds `value_ref`, never literal values; sensitive params masked in logs | "Never persist raw sensitive data into artifacts or logs" — enforced by design, not bolted on |
| 11 | **Handoff over CDP**: one browser with a debug port; controllers attach/detach; ownership flag | Native browser protocol — a real human's browser can attach to the same session. Stronger than a Playwright-private channel |
| 12 | **Draft → approved lifecycle** | Discovery emits a draft; human confirms checkpoints + authors the failure branches it never saw. Honest, and near-free credit on a stretch goal |

---

## Spike results (all confirmed on real hardware)

Run before planning, to de-risk the two empirical unknowns. **All green.**

- ✅ Perception: a11y tree as YAML, usable on a hostile no-test-ID page
- ✅ Rung 1: find + act by role+name alone
- ✅ Rung 4: coordinates captured **inline** via `aria_snapshot(boxes=True)` — same call as perception
- ✅ Generic outcome detection: read "No such member" off-screen, not from input branching
- ✅ Same-session handoff over CDP: three control transfers, state preserved across each

Design upgrades the spike handed us (fold into the build):
- Coordinates come free-and-inline with perception (rungs 1 & 4 in one call).
- `to_match_aria_snapshot()` is a native **checkpoint** primitive.
- CDP handoff means a real human browser can attach to the same debug port.

Note: `launch_server()` is JS-only — Python uses `connect_over_cdp()`. The old
`page.accessibility` API was removed in 1.56; use `locator.aria_snapshot()`.

---

## Repo layout

```
computer-use/
  app/                  # the local hostile target ("bank")
    members.html  server.py
  core/
    driver.py           # SEAM: Driver interface + WebDriver (Playwright)
    schema.py           # Pydantic artifact models — THE contract
    recorder.py         # thin model-intent -> enriched artifact step
    agent.py            # discovery loop (observe -> decide -> act)
    replay.py           # deterministic executor + result contract
    safety.py           # allowlist + risk + redaction
    handoff.py          # CDP session ownership + intervention request
    logging.py          # structured, redacting logger
  artifacts/            # saved .json capabilities
  evidence/             # discovery + replay logs, screenshots
  tests/
  cli.py                # `discover` and `replay` entrypoints
  README.md  REPORT.md  PLAN.md
```

---

# Milestones

Vertical-slice order: the thinnest thread goal→discovery→artifact→replay first,
then thicken. After every milestone the system runs end to end — just richer.
Each milestone lists what it **delivers toward the graded deliverables**.

---

## M0 — Foundation: the target + the seam
**Delivers:** a runnable hostile app + the Driver wall everything hangs off.
**Why first:** discovery needs a target; nothing touches the surface without the Driver.

- [ ] Promote the spike page into a real app. Add the two things the spike lacked:
  - [ ] a **second search field with no name/aria-label** → forces rung-2 relational targeting to do real work (proves the ladder isn't decorative).
  - [ ] the **write flow**: an "Open Sub-Account" form (2–3 fields → Confirm → confirmation screen).
- [ ] Serve over HTTP (`server.py`, Flask or stdlib) so there is a real `localhost` origin — the allowlist is meaningless on `file://`.
- [ ] Keep the **authored runtime errors** (member `99999` = not found, `00000` = permission denied, blank = validation) — detected later off-screen, never by input-branching in replay.
- [ ] Define the **`Driver` interface** (`driver.py`): `observe() -> Observation`, `click(target)`, `type(target, text)`, `read(target) -> str`, `navigate(url)`. `target` is a **semantic target**, never a raw selector. This abstract class *is* the heterogeneity seam.
- [ ] Implement **`WebDriver`** (Playwright), launched with the **CDP debug port** (so handoff works later with no re-architecting). `observe()` returns `aria_snapshot(boxes=True)`.
- [ ] **Checkpoint:** a script starts the app and drives the happy path through Driver verbs alone, reading the balance.

---

## M1 — The schema: the contract at the center
**Delivers:** the typed, versioned artifact — the single most-graded piece.
**Why here:** discovery and replay both speak this language; define it before they can drift.

- [ ] **Primitives first** (`schema.py`):
  - [ ] `Locator` — tagged union: `a11y_role_name | relational | structural | coordinates`, each with its data.
  - [ ] `Condition` — predicate in the same vocabulary ("role+name present" / "text present"). **Serves checkpoints AND outcomes** — one primitive, tested once.
- [ ] `TargetLadder` = ordered `list[Locator]` + a `robustness_note: str` per control. That note is where "how each control is identified, with reasoning about robustness" literally lives.
- [ ] `Step` — `action`, `target: TargetLadder`, `value_ref` (param ref, never literal), `extract_to`, `checkpoint: Condition`, `risk: safe|irreversible`.
- [ ] Envelope + contract — `schema_version`, `capability_id/name/description`, `product_identity` (e.g. `CoreBankPro@4`), `target` (separable slot for tenant overlay), `parameters` (typed, each `sensitive: bool`), `outputs` (typed), `known_outcomes: list[Outcome]` (name + `detect: Condition` + `classification`), `status: draft|approved`.
- [ ] Export `Artifact.model_json_schema()` — instant proof of "reviewable by a calling agent."
- [ ] **Checkpoint:** hand-write one `lookup_member_balance` artifact JSON and load it through Pydantic — validation passes. The contract is real before discovery must produce one.

---

## M2 — Thinnest discovery: goal → a real LLM run  ⭐ THE NON-NEGOTIABLE
**Delivers:** the one mandatory thing — a genuine LLM-driven run against the live surface, with evidence.
**Why now:** it's the heart of the project and the biggest empirical risk. Everything before was setup so this can happen cleanly.

- [ ] **The loop** (`agent.py`): observe (Driver → YAML tree) → decide (goal + tree → LLM → **one** structured action) → act (dispatch to Driver) → repeat. Model outputs **thin intent only** (decision #6).
- [ ] **Stopping conditions** (3.1 requires these): `max_steps`, wall-clock `timeout`, dead-end detection (model "stuck"/"done" signal, or N steps with no state change).
- [ ] **Provider seam:** use available API key; model returns validated JSON (Pydantic on the action). Swappable behind one interface.
- [ ] **Evidence from day one:** log each step — what it saw, what it decided, **why** (model's stated reason), what happened. This log is a deliverable.
- [ ] **Checkpoint:** `discover "look up member 12345 and read the savings balance"` completes against the live app; transcript saved to `/evidence/`. **Do it thin — no artifact emission yet.** This milestone proves the project is real.

---

## M3 — Close the loop: discovery emits an artifact
**Delivers:** the "record" step — completes the compiler half.
**Why here:** loop works + schema exists → connect them via the recorder.

- [ ] **The recorder** (`recorder.py`): for each model action, enrich into a full `Step` — compute the `TargetLadder` (role+name from tree, relational from label/cell structure, coords from box) and snapshot post-action state to **propose a checkpoint**.
- [ ] **Draft → approved:** discovery emits `status: draft`. A tiny review step (CLI prompt or hand-edit) is where you confirm proposed checkpoints and **hand-author `known_outcomes`** (the failure branches discovery never walked). Mark `approved`.
- [ ] **Redaction check:** recorder writes `value_ref: params.member_id`, never `12345`. Verify no real value lands in the artifact.
- [ ] **Checkpoint:** a discovery run produces `artifacts/lookup_member_balance.json`; you review + approve it.

---

## M4 — Deterministic replay + the error taxonomy  ⭐ DEEPEST WORK
**Delivers:** the production path + the most-graded behavior after the schema.
**Why here:** consumes the M3 artifact. Spend the most depth here.

- [ ] **Executor** (`replay.py`): load + validate artifact, take typed params, walk steps with **no LLM**. Per step: resolve the ladder top-down (rung 1→2→3→4 on failure), act, **verify checkpoint**.
- [ ] **Result contract** — typed:
  - [ ] `Success(outputs)` — checkpoint passed, outputs extracted.
  - [ ] `BusinessOutcome(name)` — a `known_outcome.detect` matched; returned cleanly, **not** an error.
  - [ ] `Recoverable` — known interstitial / slow load: dismiss or wait/retry, then continue (in-loop behavior, not terminal).
  - [ ] `HardFailure(step, expected, observed)` — missing on all rungs / unclassified checkpoint fail. Stop with a debuggable error → doorway to handoff.
- [ ] **Waiting:** explicit waits on checkpoint conditions (no blind sleeps) — how determinism survives transient slowness.
- [ ] **Screenshot-on-failure** → `/evidence/` (the "richer signal" 3.5 requires).
- [ ] **Checkpoints:**
  - [ ] `replay lookup_member_balance --member_id 12345` → `Success`, balance returned.
  - [ ] `replay ... --member_id 99999` → `BusinessOutcome(member_not_found)`. **This is the error-replay the evidence wants.**
  - [ ] Rung-fallback: rename a label in the app; rung 1 fails, rung 2 catches it (drift demo).

---

## M5 — Safety, made real
**Delivers:** allowlist + risky-action handling + redaction (3.4), exercised by the write flow.

- [ ] **Allowlist** (`safety.py`): permitted origins/routes + action types, enforced **inside the Driver** so nothing can act off-list. Demonstrate a blocked off-allowlist navigation.
- [ ] **Risky/irreversible:** the write flow's Confirm step is `risk: irreversible`. Replay **requires explicit confirmation** (`--confirm` flag / approval callback) before executing; blocks otherwise. Justify "confirm" in REPORT.
- [ ] **Redacting logger** (`logging.py`): values tied to a `sensitive` param are masked in every log line. Demonstrate `member_id=***`, never the real value.
- [ ] **Checkpoint:** run the write flow → pauses for confirmation on the irreversible step; off-allowlist action refused; logs redacted.

---

## M6 — Escalation & handoff (CDP mechanism, productized)
**Delivers:** 3.6 — a real, non-TODO control transfer, built on the passing spike.

- [ ] **Detect "stuck":** a `HardFailure` (or discovery dead-end) triggers escalation instead of exiting.
- [ ] **Intervention request** (`handoff.py`): structured object with capability/goal, current step, current state (+ screenshot), why it stopped → `/evidence/`.
- [ ] **Control transfer:** ownership flag `automation → human`; automation detaches, CDP browser stays live; expose the debug endpoint. **Mock the operator UI** (CLI: "session is yours at `<cdp-url>`, act, press Enter") — mechanism is the real CDP attach.
- [ ] **Hand back:** on resume, flag `human → automation`, re-attach, **re-observe** state, continue/finish. **Record what the human did.**
- [ ] **Checkpoint:** force a `HardFailure` → intervention raised with context → human drives the same session to clear it → hand back → run resumes. Whole thing to `/evidence/`.

---

## M7 — Deliverables polish (compliance points)
**Delivers:** the free points submissions lose by fumbling.

- [ ] **`/REPORT.md`** — the **seven exact headings**: Architecture, Artifact schema, Determinism & error handling, Heterogeneity & multi-tenant, Escalation & handoff, Safety, Cuts. (Largely drafted through our design conversation.)
- [ ] **`/README.md`** — setup, keys/config, **how to run without live services** (replay + tests need no API key), exact `discover` / `replay` demo commands.
- [ ] **`/evidence/`** — discovery log, replay-success log, replay-business-outcome log, handoff log, failure screenshot.
- [ ] **Secrets:** `.env` gitignored, key never committed.
- [ ] **`tests/`** — schema validation, ladder fallback, taxonomy classification. Typed where it counts; not exhaustive.

---

## Heterogeneity & multi-tenant (DESIGN-ONLY — REPORT §4, do not build)

- **Surface abstraction:** the `Driver` seam. A `DesktopDriver` satisfies the same
  verbs via an OS accessibility API; a11y-tree perception already generalizes; the
  flow (artifact) is unchanged. Only the Driver behind the wall changes.
- **Multi-tenant reuse:** artifact = **base capability + thin per-tenant overlay**
  (only deltas — a URL, a renamed label). `product_identity` tags "same app."
  **Checkpoints double as drift detectors:** a base replayed on a new tenant fails
  its checkpoint loudly and specifically → the signal to author an overlay (or, if
  divergence is large, record a fresh base). Keep tenant values in separable slots
  (already in the schema) so nothing is hardwired.

---

## Deliberate cuts (state in REPORT §7)

- Operator console → bare CLI (mechanism real, UI mocked).
- Desktop Driver → seam only, not built.
- Multi-tenant plumbing → design only (brief gives negative credit for building it).
- Write capability kept thin (exists mainly to exercise safety).
- Stretch goals not attempted unless core is solid (draft→approved gives partial
  credit on one for free).

---

## Stop-early story

If time runs out, stop at a milestone boundary — everything through it is a
coherent runnable thread. Minimum credible submission: **through M4** (the full
graded core: real discovery run + schema + deterministic replay + error taxonomy).
M5–M6 wrap safety and handoff; M7 is compliance. Document the rest as next steps.
```