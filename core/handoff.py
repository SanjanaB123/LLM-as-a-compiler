"""
ESCALATION & HANDOFF (milestone M6) — brief 3.6.

When replay hits a `HardFailure` it has two honest options: stop, or ask a
person. This module is the second one, and the transfer is real rather than
simulated — the human drives **the same live browser session**, not a fresh
copy of it.

That is possible because of a choice made back in M0: the Chromium process is
launched separately with an open CDP debug port, and a driver is merely one
client attached to it. So handing over is not a re-architecture, it is
`detach()`; and because CDP is the browser's own remote protocol, the second
controller can equally be a person's own browser pointed at that port.

    automation ──detach──▶  [ live browser, state intact ]  ◀──attach── human

Three things make it a handoff rather than a restart:

1. **An intervention request**, not an error message. What was being attempted,
   which step, what was expected, what was actually on screen, a screenshot,
   and the URL to take over at. Someone woken at 2am should not have to read
   the code to know what to do.

2. **An ownership ledger.** Exactly one party holds the session at any moment,
   every transfer is recorded with a reason, and the record goes to evidence.
   "Who was driving when this happened" is the first question asked after an
   incident.

3. **Re-observation on resume.** The automation never assumes it is where it
   left off — the human may have navigated anywhere. It looks first, and if
   the human already completed the blocked step, it skips it instead of doing
   it twice. Acting on a remembered position after someone else has been
   driving is how automation double-submits a payment.

What the human did is recorded by diffing the screen before and after, because
"the operator fixed it" is not an audit trail.

**A refusal never escalates.** That path is closed deliberately in `replay.py`:
asking a person to finish something the allowlist forbade would turn the safety
control into a routing step toward defeating it.

The operator console is a stated cut — `CliOperator` prints the request and
waits for a keypress. The mechanism underneath it is real; only the UI is
mocked.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Protocol

from core.driver import Observation, WebDriver
from core.logging import EvidenceLog


class Ownership(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass(frozen=True)
class Transfer:
    at: float
    frm: Ownership
    to: Ownership
    reason: str


@dataclass
class ControlLedger:
    """Who holds the session, and the history of how that changed.

    One owner at a time, enforced: a transfer to the current owner is a bug in
    the caller, not a no-op to be tolerated quietly.
    """

    owner: Ownership = Ownership.AUTOMATION
    transfers: list[Transfer] = field(default_factory=list)

    def transfer_to(self, new_owner: Ownership, reason: str) -> Transfer:
        if new_owner is self.owner:
            raise ValueError(f"{new_owner.value} already holds the session")
        transfer = Transfer(at=time.time(), frm=self.owner, to=new_owner, reason=reason)
        self.owner = new_owner
        self.transfers.append(transfer)
        return transfer

    @property
    def history(self) -> list[str]:
        return [f"{t.frm.value} -> {t.to.value} ({t.reason})" for t in self.transfers]


@dataclass(frozen=True)
class InterventionRequest:
    """Everything a person needs to take over, in one object.

    Written to evidence as JSON so it can be delivered by any channel — a
    queue, an email, a console — without this module knowing about any of them.
    """

    capability_id: str
    step_id: str
    what_it_was_doing: str
    expected: str
    observed: str
    cdp_url: str
    page_url: str
    screenshot: str | None = None
    raised_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def as_brief(self) -> str:
        """The human-readable version, for a console or a message."""
        return (
            f"INTERVENTION NEEDED — {self.capability_id}\n"
            f"  step:      {self.step_id}\n"
            f"  doing:     {self.what_it_was_doing}\n"
            f"  expected:  {self.expected}\n"
            f"  observed:  {self.observed}\n"
            f"  page:      {self.page_url}\n"
            f"  screenshot:{self.screenshot or '—'}\n"
            f"  take over: attach a browser to {self.cdp_url}"
        )


@dataclass(frozen=True)
class OperatorResponse:
    """What came back from the person (or the thing standing in for one)."""

    resumed: bool
    notes: str = ""

    @classmethod
    def abandoned(cls, why: str) -> OperatorResponse:
        return cls(resumed=False, notes=why)


class Operator(Protocol):
    """The seam to whoever takes over. One method, so the console can be
    anything: a CLI, a web app, a queue consumer, or a test."""

    name: str

    def take_control(self, request: InterventionRequest) -> OperatorResponse:
        ...


class CliOperator:
    """The stated cut: a real mechanism behind a mocked console.

    It prints the request and waits. Whoever is watching attaches their own
    browser to the CDP URL, fixes whatever is wrong in the live session, and
    presses Enter. Nothing about the transfer is simulated — only the UI is.
    """

    name = "cli"

    def take_control(self, request: InterventionRequest) -> OperatorResponse:
        print("\n" + "=" * 70)
        print(request.as_brief())
        print("=" * 70)
        print("The session is yours. Act in the browser, then press Enter to hand")
        print("it back — or type 'abandon' to stop the run.")
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            return OperatorResponse.abandoned("no operator attached to this terminal")
        if answer == "abandon":
            return OperatorResponse.abandoned("operator declined to resume")
        return OperatorResponse(resumed=True, notes=answer or "operator resolved it")


@dataclass
class ScriptedOperator:
    """A stand-in that really does attach over CDP and act.

    Not a mock of the transfer — the transfer is genuine, with a second
    controller connecting to the same debug port while the automation is
    detached. Only the person is simulated, which is what makes the handoff
    testable without someone sitting at a keyboard.
    """

    session_cdp_url: str
    clicks: list[str] = field(default_factory=list)
    name: str = "scripted-operator"
    seen: list[str] = field(default_factory=list)

    def take_control(self, request: InterventionRequest) -> OperatorResponse:
        from core.driver import BrowserSession

        self.seen.append(request.step_id)
        session = BrowserSession(port=int(self.session_cdp_url.rsplit(":", 1)[1]))
        human = WebDriver(session=session)
        human._owns_session = False  # a controller, not the owner of the process
        human.reattach()
        try:
            for label in self.clicks:
                human.page.get_by_role("button", name=label).click()
            return OperatorResponse(resumed=True, notes=f"clicked {self.clicks}")
        finally:
            human.detach()


def describe_change(before: Observation, after: Observation) -> list[str]:
    """What changed on screen while someone else was driving.

    "The operator fixed it" is not an audit trail. Diffing the accessibility
    tree gives a concrete, surface-independent account of what they actually
    did, without needing them to write it down.
    """
    old = {(e.role, e.name, e.text) for e in before.elements}
    new = {(e.role, e.name, e.text) for e in after.elements}
    changes = []
    for role, name, text in sorted(new - old):
        changes.append(f"appeared: {role} {name or text or '(unnamed)'}")
    for role, name, text in sorted(old - new):
        changes.append(f"gone: {role} {name or text or '(unnamed)'}")
    if before.url != after.url:
        changes.append(f"navigated: {before.url} -> {after.url}")
    return changes or ["no visible change"]


def record_intervention(
    log: EvidenceLog,
    request: InterventionRequest,
    ledger: ControlLedger,
    response: OperatorResponse,
    changes: list[str],
) -> None:
    """Put the whole episode in the evidence: the ask, the transfers, the fix."""
    (log.dir / f"intervention-{request.step_id}.json").write_text(
        log.redact(request.to_json())
    )
    log.heading(f"Human intervention — {request.step_id}", level=2)
    log.table(
        [
            ("Why", request.observed),
            ("Expected", request.expected),
            ("Session", request.cdp_url),
            ("Control", " ; ".join(ledger.history)),
            ("Operator said", response.notes),
            ("What changed", "; ".join(changes)),
            ("Resumed", response.resumed),
        ]
    )
    log.event(
        "intervention",
        step=request.step_id,
        resumed=response.resumed,
        notes=response.notes,
        changes=changes,
        transfers=ledger.history,
    )
