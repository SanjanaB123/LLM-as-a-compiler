"""
THE SEAM (plan decision #5, milestone M0).

`Driver` is the wall between "how we perceive and act on a surface" and "the
recorded flow". Above the wall — the artifact, discovery, replay — nothing knows
whether it is driving a browser, a Windows app, or a terminal. Below it, one
implementation: `WebDriver`.

Two rules make the wall hold:

1. **Semantic targets only.** Every verb takes a `TargetLadder`, never a CSS or
   XPath selector. Translating a ladder into surface mechanics is this file's
   private business — which is exactly why a `DesktopDriver` could satisfy the
   same interface against an OS accessibility API without a line changing above.

2. **Conditions are evaluated behind the wall too.** `check()` looks like an
   extra verb beyond the plan's five, but it has to live here: the `Condition`
   vocabulary is surface-independent while *answering* it is surface-specific.
   Leaving it above the wall would mean replay reaching around the seam.

Targeting ladder (decision #7), resolved top-down, falling through only on
failure:

    rung 1  a11y role+name   `get_by_role` / name-only lookup
    rung 2  relational        geometry against a text anchor
    rung 3  structural        Nth control of a role in reading order
    rung 4  coordinates       a point, scaled from the recorded viewport

Rungs 2-4 are implemented with **geometry and reading order**, not raw
selectors. That is not fussiness: it is the same information an OS accessibility
API exposes, so the implementation strategy ports to desktop rather than being a
web trick.

A rung only counts as resolved when it matches **exactly one** visible element.
Ambiguity falls through to the next rung instead of silently taking the first
match — a flow that acts on "whichever one Playwright returned first" is not
deterministic, it is lucky.

Browser lifecycle: the Chromium process is launched separately with an open CDP
debug port (`BrowserSession`), and the driver attaches to it as one client among
possible others (`WebDriver`). That split is the handoff design (decision #11)
arriving early, so M6 adds ownership semantics rather than re-architecting: the
browser is the session, and controllers come and go.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import tempfile
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import expect as pw_expect

from core.safety import ALL_ACTIONS, ActionNotAllowed, Allowlist
from core.schema import (
    A11yRoleNameLocator,
    AllOf,
    AnyOf,
    AriaSnapshotMatches,
    Condition,
    CoordinatesLocator,
    Not,
    RelationalLocator,
    RoleNamePresent,
    StructuralLocator,
    TargetLadder,
    TextAbsent,
    TextPresent,
    ValueMatchesRef,
)

# ActionNotAllowed is defined alongside the policy in safety.py but raised
# here, so it is re-exported: callers catch it from the object that refused.
__all__ = [
    "ActionNotAllowed", "Allowlist", "Box", "BrowserSession", "Driver",
    "DriverError", "Element", "Observation", "Resolution", "TargetNotFound",
    "WebDriver", "parse_aria_snapshot", "shutdown_playwright",
]

POLL_INTERVAL_MS = 100

_PLAYWRIGHT = None


def _playwright():
    """One Playwright per process, created on first use.

    Not an optimisation. The sync API refuses to start a second instance in the
    same thread, and M6's handoff has two controllers attached to one browser at
    once — automation and the stand-in human. Sharing the instance is what makes
    that possible at all; each controller still gets its own CDP connection.
    """
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        _PLAYWRIGHT = sync_playwright().start()
    return _PLAYWRIGHT


def shutdown_playwright() -> None:
    """Release the shared instance. For process teardown, not per-driver."""
    global _PLAYWRIGHT
    if _PLAYWRIGHT is not None:
        _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Box:
    """A rectangle in viewport coordinates. Rung 4's payload, and the geometry
    rungs 2 and 3 reason over."""

    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    def shares_row_with(self, anchor: Box) -> bool:
        """True when this box sits on the same visual line as the anchor — how
        "same row" is decided without knowing the surface has <tr> elements.

        The tolerance is the ANCHOR's height, not the larger of the two. Using
        the larger let a page-sized wrapper count as sharing every row, which
        silently shifted `occurrence` indexes by one.
        """
        return abs(self.center_y - anchor.center_y) <= anchor.height / 2

    def overlaps_column_with(self, other: Box) -> bool:
        return self.right > other.x and self.x < other.right

    def contains_point(self, x: float, y: float) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def contains_box(self, other: Box) -> bool:
        return (
            self.x <= other.x
            and self.y <= other.y
            and self.right >= other.right
            and self.bottom >= other.bottom
        )

    def strictly_contains(self, other: Box, tolerance: float = 1.0) -> bool:
        """Contains `other` and is meaningfully bigger than it.

        The distinction matters: the anchor's own element contains itself, and
        excluding it would renumber `occurrence` depending on whether the
        caption text happens to sit directly in the cell or inside a <b> within
        it. A human counting "the second cell in this row" counts the caption as
        the first one either way.
        """
        return self.contains_box(other) and (self.area - other.area) > tolerance


@dataclass(frozen=True)
class Element:
    """One addressable node of the observed surface.

    `ref` is the handle the model uses to point at this element ("e7"). Refs
    exist because names don't suffice: the target app's Branch Code field has no
    accessible name, so a model restricted to naming things could not refer to
    it at all. A ref sidesteps that without letting the model author locators —
    it says *which* element, and the recorder works out how to find it again.

    Refs are per-observation and not stable across steps; nothing is persisted
    by ref. The artifact only ever holds the ladder derived from one.
    """

    ref: str
    role: str
    name: str
    text: str
    box: Box | None
    depth: int

    @property
    def label(self) -> str:
        return self.name or self.text or f"<{self.role}>"


@dataclass(frozen=True)
class Observation:
    """What the surface looks like right now — the perception payload.

    Three views of the same snapshot, each for a different consumer:

    - `tree` — the plain a11y tree.
    - `tree_boxed` — with `[box=x,y,w,h]` on every node. The spike's finding was
      that perception and coordinate capture are a single call, so recording a
      rung-4 fallback costs nothing extra.
    - `tree_for_model` — refs instead of boxes. Coordinates are noise to a model
      choosing *what* to act on, and showing them would invite it to think in
      pixels.

    Deliberately text, not pixels. It is the only perception model that honestly
    extends to desktop, and it makes rung 1 free.
    """

    url: str
    title: str
    tree: str
    tree_boxed: str
    tree_for_model: str
    elements: tuple[Element, ...]
    captured_at: float

    @property
    def digest(self) -> str:
        """Stable hash of the tree. Discovery compares digests to notice it is
        making no progress — the dead-end stopping condition (M2)."""
        return hashlib.sha256(self.tree.encode()).hexdigest()[:16]

    def element(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def same_row_as(self, element: Element) -> list[Element]:
        """Elements sharing this one's visual line, left to right. The raw
        material for a relational rung."""
        if element.box is None:
            return []
        hits = [
            e
            for e in self.elements
            if e.box is not None
            and e.ref != element.ref
            and e.box.shares_row_with(element.box)
            and not e.box.strictly_contains(element.box)
        ]
        return sorted(hits, key=lambda e: e.box.x)


@dataclass(frozen=True)
class Attempt:
    """One rung tried, and what came of it. The audit trail behind a
    `Resolution` — and the drift signal: "rung 1 missed, rung 2 caught it"."""

    rung: int
    kind: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Resolution:
    """The result of acting on a ladder: which rung won, and what was there.

    `text` is populated by `read()`. One return type for every verb keeps the
    evidence log uniform.
    """

    rung: int
    kind: str
    box: Box | None
    attempts: tuple[Attempt, ...]
    text: str | None = None

    @property
    def fell_back(self) -> bool:
        """Did a sturdier rung actually fail here?

        Not simply "resolved above rung 1". A ladder for an anonymous control
        has no rung 1 to fall back *from*, so reporting a fallback there would
        describe drift that never happened — and drift reports are the reason
        anyone reads this evidence.
        """
        return any(not attempt.ok for attempt in self.attempts)


@dataclass(frozen=True)
class CheckResult:
    """Did the condition hold, and if not, what was on screen instead.

    `observed` is the honest other half of a `HardFailure`: an error that says
    only "expected X" is not debuggable.
    """

    ok: bool
    waited_ms: int
    observed: str = ""


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DriverError(Exception):
    pass


class TargetNotFound(DriverError):
    """Every rung failed. Replay turns this into a `HardFailure`."""

    def __init__(self, ladder: TargetLadder, attempts: Sequence[Attempt]) -> None:
        self.ladder = ladder
        self.attempts = tuple(attempts)
        trail = "; ".join(f"rung {a.rung} ({a.kind}): {a.detail}" for a in attempts)
        super().__init__(f"no rung of the ladder resolved -> {trail}")


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #


class Driver(ABC):
    """The abstract surface. This class *is* the heterogeneity answer."""

    @abstractmethod
    def navigate(self, url: str) -> None:
        ...

    @abstractmethod
    def observe(self) -> Observation:
        ...

    @abstractmethod
    def click(self, target: TargetLadder) -> Resolution:
        ...

    @abstractmethod
    def type(self, target: TargetLadder, text: str) -> Resolution:  # noqa: A003
        ...

    @abstractmethod
    def read(self, target: TargetLadder) -> Resolution:
        """Returns a `Resolution` whose `.text` holds the value, rather than a
        bare str: replay needs to record which rung produced the value."""

    @abstractmethod
    def check(
        self,
        condition: Condition,
        timeout_ms: int = 5000,
        values: dict[str, str] | None = None,
    ) -> CheckResult:
        """Wait until the condition holds, or until the timeout expires.

        This is how determinism survives transient slowness: replay waits on a
        stated condition and never sleeps a guessed interval.
        """

    @abstractmethod
    def probe(self, locator) -> Box | None:
        """Resolve a single locator without acting: the box if it matches
        exactly one visible element, else None.

        Belongs behind the seam for the same reason `check` does — only the
        surface can say whether a locator finds anything on it.
        """

    @abstractmethod
    def screenshot(self, path: str | Path) -> Path:
        ...


# --------------------------------------------------------------------------- #
# The browser session: a process, not a client
# --------------------------------------------------------------------------- #


class BrowserSession:
    """A long-lived Chromium with an open CDP debug port.

    Launched as a subprocess rather than via `playwright.launch()` on purpose:
    when a Playwright-launched browser's client disconnects, the browser dies.
    Here the process outlives any controller, so automation can detach and leave
    the session alive for a human to take over — the whole point of the CDP
    choice. (`launch_server()` would do this too, but it is JS-only; Python's
    path is `connect_over_cdp`, which the spike confirmed.)
    """

    def __init__(
        self,
        port: int = 0,
        headed: bool = False,
        user_data_dir: str | Path | None = None,
        window: tuple[int, int] = (1280, 720),
    ) -> None:
        self.port = port or _free_port()
        self.headed = headed
        self.window = window
        self._user_data_dir = Path(
            user_data_dir or tempfile.mkdtemp(prefix="corebankpro-profile-")
        )
        self._proc: subprocess.Popen | None = None

    @property
    def cdp_url(self) -> str:
        """The endpoint a second controller — including a real human's browser —
        attaches to."""
        return f"http://127.0.0.1:{self.port}"

    def start(self, chromium_path: str) -> str:
        args = [
            chromium_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self._user_data_dir}",
            f"--window-size={self.window[0]},{self.window[1]}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not self.headed:
            args.append("--headless=new")
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._await_endpoint()
        return self.cdp_url

    def _await_endpoint(self, timeout_s: float = 20.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=1) as r:
                    json.load(r)
                    return
            except Exception:
                time.sleep(0.1)
        raise DriverError(f"CDP endpoint never came up on {self.cdp_url}")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# --------------------------------------------------------------------------- #
# The web implementation
# --------------------------------------------------------------------------- #


class WebDriver(Driver):
    """Playwright behind the seam, attached to a `BrowserSession` over CDP.

    `allowed_origins` comes straight off the artifact's tenant slot. Checking it
    here means a flow cannot navigate off-allowlist even if its steps say to.
    """

    def __init__(
        self,
        allowed_origins: Sequence[str] | None = None,
        session: BrowserSession | None = None,
        headed: bool = False,
        viewport: tuple[int, int] = (1280, 720),
        allowlist: Allowlist | None = None,
    ) -> None:
        # A bare list of origins is the common case; a full Allowlist is used
        # when routes or action types need constraining too.
        self.allowlist = allowlist or Allowlist(
            origins=tuple(allowed_origins or ()), actions=ALL_ACTIONS
        )
        self.allowed_origins = self.allowlist.origins
        self.viewport = viewport
        self._headed = headed
        self._session = session
        self._owns_session = session is None
        self._pw = None
        self._browser = None
        self._page: Page | None = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> WebDriver:
        self._pw = _playwright()
        if self._session is None:
            self._session = BrowserSession(headed=self._headed, window=self.viewport)
            self._session.start(self._pw.chromium.executable_path)
        self._browser = self._pw.chromium.connect_over_cdp(self._session.cdp_url)
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        self._page = context.pages[0] if context.pages else context.new_page()
        # A known viewport is what makes recorded coordinates meaningful.
        self._page.set_viewport_size({"width": self.viewport[0], "height": self.viewport[1]})
        return self

    def detach(self) -> None:
        """Release this CDP connection, leaving the browser running.

        The asymmetry the whole handoff rests on: the session is the *process*,
        and controllers come and go. After this the page is still open, still
        logged in, still exactly where it was — and a second controller, or a
        person's own browser pointed at the debug port, can pick it up.
        """
        if self._browser is not None:
            self._browser.close()
            self._browser = None
            self._page = None

    def reattach(self) -> WebDriver:
        """Take the session back and find out what state it is in now.

        Deliberately does not restore any remembered page or position: whoever
        held control in between may have navigated anywhere, so the caller
        re-observes rather than assuming.
        """
        if self._session is None:
            raise DriverError("no session to reattach to")
        self._pw = _playwright()
        self._browser = self._pw.chromium.connect_over_cdp(self._session.cdp_url)
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        self._page = context.pages[0] if context.pages else context.new_page()
        return self

    @property
    def attached(self) -> bool:
        return self._browser is not None

    def stop(self) -> None:
        """Detach, and shut the browser down if this driver started it."""
        self.detach()
        if self._owns_session and self._session is not None:
            self._session.stop()
        # The shared Playwright instance deliberately stays up: another
        # controller may still be attached, or about to attach.
        self._pw = None

    def __enter__(self) -> WebDriver:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def cdp_url(self) -> str:
        if self._session is None:
            raise DriverError("driver not started")
        return self._session.cdp_url

    @property
    def page(self) -> Page:
        if self._page is None:
            raise DriverError("driver not started; call start() or use it as a context manager")
        return self._page

    # -- verbs ------------------------------------------------------------- #

    def navigate(self, url: str) -> None:
        self.allowlist.check_action("navigate")
        self.allowlist.check_navigation(url)
        self.page.goto(url, wait_until="domcontentloaded")

    def observe(self) -> Observation:
        body = self.page.locator("body")
        boxed = body.aria_snapshot(boxes=True)
        elements, for_model = parse_aria_snapshot(boxed)
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            tree=body.aria_snapshot(),
            tree_boxed=boxed,
            tree_for_model=for_model,
            elements=elements,
            captured_at=time.time(),
        )

    def probe(self, locator) -> Box | None:
        """Does this single locator resolve here, uniquely, and to what?

        The recorder uses it to verify each candidate rung against the live
        surface at record time, so an artifact only ever contains rungs that
        were demonstrated to work — rather than rungs that looked plausible in
        a tree and fail the first time replay needs them.
        """
        try:
            return self._resolve_one(locator).box
        except (_NotFound, _Ambiguous):
            return None

    def click(self, target: TargetLadder) -> Resolution:
        self.allowlist.check_action("click")
        handle, resolution = self._resolve(target)
        if handle.locator is not None:
            handle.locator.click()
        else:
            x, y = handle.point  # type: ignore[misc]
            self.page.mouse.click(x, y)
        return resolution

    def type(self, target: TargetLadder, text: str) -> Resolution:  # noqa: A003
        self.allowlist.check_action("type")
        handle, resolution = self._resolve(target)
        if handle.locator is not None:
            handle.locator.fill(text)
        else:
            x, y = handle.point  # type: ignore[misc]
            self.page.mouse.click(x, y)
            self.page.keyboard.press("ControlOrMeta+a")
            self.page.keyboard.type(text)
        return resolution

    def read(self, target: TargetLadder) -> Resolution:
        self.allowlist.check_action("read")
        handle, resolution = self._resolve(target)
        if handle.locator is not None:
            text = handle.locator.inner_text()
        else:
            x, y = handle.point  # type: ignore[misc]
            text = self.page.evaluate(
                "([x, y]) => { const el = document.elementFromPoint(x, y);"
                " return el ? el.innerText : ''; }",
                [x, y],
            )
        return Resolution(
            rung=resolution.rung,
            kind=resolution.kind,
            box=resolution.box,
            attempts=resolution.attempts,
            text=(text or "").strip(),
        )

    def screenshot(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(out), full_page=True)
        return out

    # -- conditions -------------------------------------------------------- #

    def check(
        self,
        condition: Condition,
        timeout_ms: int = 5000,
        values: dict[str, str] | None = None,
    ) -> CheckResult:
        started = time.time()
        deadline = started + timeout_ms / 1000
        while True:
            if self._evaluate(condition, values or {}):
                return CheckResult(ok=True, waited_ms=int((time.time() - started) * 1000))
            if time.time() >= deadline:
                return CheckResult(
                    ok=False,
                    waited_ms=int((time.time() - started) * 1000),
                    observed=self.describe_state(),
                )
            time.sleep(POLL_INTERVAL_MS / 1000)

    def describe_state(self) -> str:
        """A short account of what is on screen — the `observed` half of a
        failure report.

        Messages first, then the controls that are present. The controls matter
        when a step failed because something is *missing*: "expected the Search
        button, and here is what exists instead" is a report someone can act
        on, where a bare URL leaves them opening a browser to find out.
        """
        parts = [f"url={self.page.url}"]
        for role in ("alert", "status", "heading"):
            texts = [
                t.strip()
                for t in (
                    el.inner_text()
                    for el in self.page.get_by_role(role).all()
                    if el.is_visible()
                )
                if t.strip()
            ]
            if texts:
                parts.append(f"{role}={texts[:3]}")

        controls = []
        for role in ("button", "textbox", "combobox", "link"):
            found = self._visible(self.page.get_by_role(role))
            for el in found[:4]:
                name = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
                controls.append(f"{role}{f' {name!r}' if name else ''}")
        if controls:
            parts.append(f"controls present: {', '.join(controls)}")
        return " | ".join(parts)

    def _evaluate(self, condition, values: dict[str, str]) -> bool:
        if isinstance(condition, AllOf):
            return all(self._evaluate(c, values) for c in condition.conditions)
        if isinstance(condition, AnyOf):
            return any(self._evaluate(c, values) for c in condition.conditions)
        if isinstance(condition, Not):
            return not self._evaluate(condition.condition, values)

        if isinstance(condition, RoleNamePresent):
            return bool(
                self._visible(
                    self.page.get_by_role(
                        condition.role, name=condition.name, exact=condition.exact
                    )
                )
            )

        if isinstance(condition, TextPresent):
            return bool(self._text_matches(condition))

        if isinstance(condition, TextAbsent):
            return not self._text_matches(condition)

        if isinstance(condition, ValueMatchesRef):
            expected = values.get(condition.value_ref)
            if expected is None:
                raise DriverError(
                    f"checkpoint needs the value behind {condition.value_ref!r}, "
                    "but it was not supplied"
                )
            try:
                handle, _ = self._resolve(condition.target)
            except TargetNotFound:
                return False
            if handle.locator is None:
                return False
            return handle.locator.input_value() == expected

        if isinstance(condition, AriaSnapshotMatches):
            try:
                pw_expect(self.page.locator("body")).to_match_aria_snapshot(
                    condition.snapshot, timeout=POLL_INTERVAL_MS
                )
                return True
            except AssertionError:
                return False

        raise DriverError(f"unsupported condition {type(condition).__name__}")

    def _text_matches(self, condition: TextPresent | TextAbsent) -> list[PWLocator]:
        """Shared by text_present / text_absent so the two can never disagree.

        An empty `text` with a role set means "any element with that role" — the
        shape a checkpoint needs to say "the search responded somehow", which
        must hold whether the response is a detail panel or an error.
        """
        if condition.role:
            candidates = self._visible(self.page.get_by_role(condition.role))
            if not condition.text:
                return candidates
            return [
                c
                for c in candidates
                if _text_hit(c.inner_text(), condition.text, condition.exact)
            ]
        return self._visible(self.page.get_by_text(condition.text, exact=condition.exact))

    # -- ladder resolution ------------------------------------------------- #

    def _resolve(self, ladder: TargetLadder) -> tuple[_Handle, Resolution]:
        """Walk the ladder sturdiest-first; the first rung that resolves
        unambiguously wins. Every attempt is recorded, so a fallback is visible
        in the evidence rather than silent."""
        attempts: list[Attempt] = []
        for locator in ladder.rungs:
            rung = _RUNG_OF[type(locator)]
            try:
                handle = self._resolve_one(locator)
            except _Ambiguous as exc:
                attempts.append(Attempt(rung, locator.kind, False, str(exc)))
                continue
            except _NotFound as exc:
                attempts.append(Attempt(rung, locator.kind, False, str(exc)))
                continue
            attempts.append(Attempt(rung, locator.kind, True, "resolved"))
            return handle, Resolution(
                rung=rung, kind=locator.kind, box=handle.box, attempts=tuple(attempts)
            )
        raise TargetNotFound(ladder, attempts)

    def _resolve_one(self, locator) -> _Handle:
        if isinstance(locator, A11yRoleNameLocator):
            return self._rung1(locator)
        if isinstance(locator, RelationalLocator):
            return self._rung2(locator)
        if isinstance(locator, StructuralLocator):
            return self._rung3(locator)
        if isinstance(locator, CoordinatesLocator):
            return self._rung4(locator)
        raise DriverError(f"unsupported locator {type(locator).__name__}")

    def _rung1(self, loc: A11yRoleNameLocator) -> _Handle:
        if loc.role:
            found = self._visible(
                self.page.get_by_role(loc.role, name=loc.name, exact=loc.exact)
            )
            what = f"role={loc.role} name={loc.name!r}"
        else:
            # Name-only: "the element whose accessible name is this".
            found = self._visible(self.page.get_by_label(loc.name, exact=loc.exact))
            what = f"name={loc.name!r}"
        return _one(found, what)

    def _rung2(self, loc: RelationalLocator) -> _Handle:
        """Geometry against a text anchor. No DOM knowledge, so the strategy is
        the same one a desktop driver would use."""
        anchor = self._anchor_box(loc.anchor_text)
        candidates = self._with_boxes(self.page.get_by_role(loc.target_role))
        matches = _by_relation(candidates, anchor, loc.relation)
        if not matches:
            raise _NotFound(
                f"no {loc.target_role} {loc.relation} {loc.anchor_text!r} "
                f"({len(candidates)} candidate(s) of that role on screen)"
            )
        if len(matches) < loc.occurrence:
            raise _NotFound(
                f"wanted occurrence {loc.occurrence} of {loc.target_role} "
                f"{loc.relation} {loc.anchor_text!r}, found {len(matches)}"
            )
        el, box = matches[loc.occurrence - 1]
        return _Handle(locator=el, point=None, box=box)

    def _rung3(self, loc: StructuralLocator) -> _Handle:
        """Nth control of a role in reading order, optionally after a caption.

        Its stated fragility is literal: insert one control earlier in the page
        and every index below it shifts.
        """
        candidates = self._with_boxes(self.page.get_by_role(loc.role))
        if loc.container_text:
            anchor = self._anchor_box(loc.container_text)
            candidates = _by_relation(candidates, anchor, "after")
        else:
            candidates = sorted(candidates, key=lambda p: (p[1].y, p[1].x))
        if loc.index >= len(candidates):
            raise _NotFound(
                f"wanted {loc.role} #{loc.index} but only {len(candidates)} are on screen"
            )
        el, box = candidates[loc.index]
        return _Handle(locator=el, point=None, box=box)

    def _rung4(self, loc: CoordinatesLocator) -> _Handle:
        """A raw point, scaled from the viewport it was recorded in.

        Last resort, and the reason the ladder reaches surfaces with no DOM at
        all. Scaling is the small honesty that keeps it from being nonsense at a
        different window size.
        """
        current = self.page.viewport_size or {
            "width": loc.viewport_width,
            "height": loc.viewport_height,
        }
        sx = current["width"] / loc.viewport_width
        sy = current["height"] / loc.viewport_height
        x, y = loc.x * sx, loc.y * sy
        # Report the box of whatever is actually at the point, not a degenerate
        # rectangle around it: the recorder verifies a rung by comparing the
        # element it lands on, and a zero-size box could never match anything.
        rect = self.page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " if (!el) return null; const r = el.getBoundingClientRect();"
            " return [r.x, r.y, r.width, r.height]; }",
            [x, y],
        )
        if rect is None:
            raise _NotFound(f"no element at ({x:.0f}, {y:.0f})")
        return _Handle(locator=None, point=(x, y), box=Box(*rect))

    # -- helpers ----------------------------------------------------------- #

    def _visible(self, locator: PWLocator) -> list[PWLocator]:
        return [el for el in locator.all() if el.is_visible()]

    def _with_boxes(self, locator: PWLocator) -> list[tuple[PWLocator, Box]]:
        out = []
        for el in self._visible(locator):
            bb = el.bounding_box()
            if bb:
                out.append((el, Box(bb["x"], bb["y"], bb["width"], bb["height"])))
        return out

    def _anchor_box(self, text: str) -> Box:
        """The tightest visible element carrying this text.

        Smallest-area wins because text matches nest: the cell, its row, and the
        wrapper table all "contain" the caption, and only the innermost one is a
        useful anchor.
        """
        candidates = self._with_boxes(self.page.get_by_text(text, exact=False))
        if not candidates:
            raise _NotFound(f"no visible text anchor {text!r}")
        return min(candidates, key=lambda p: p[1].area)[1]


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


@dataclass
class _Handle:
    """Something actionable: an element, or a bare point for rung 4."""

    locator: PWLocator | None
    point: tuple[float, float] | None
    box: Box | None


class _NotFound(Exception):
    pass


class _Ambiguous(Exception):
    pass


_RUNG_OF = {
    A11yRoleNameLocator: 1,
    RelationalLocator: 2,
    StructuralLocator: 3,
    CoordinatesLocator: 4,
}


def _one(found: list[PWLocator], what: str) -> _Handle:
    """A rung resolves only when it is unambiguous.

    Taking `.first` out of several matches is how a flow starts working by
    accident, then breaks when the order changes. Ambiguity is a fallthrough,
    not a coin flip.
    """
    if not found:
        raise _NotFound(f"no visible element with {what}")
    if len(found) > 1:
        raise _Ambiguous(f"{len(found)} visible elements with {what}; ambiguous")
    el = found[0]
    bb = el.bounding_box()
    box = Box(bb["x"], bb["y"], bb["width"], bb["height"]) if bb else None
    return _Handle(locator=el, point=None, box=box)


def _by_relation(
    candidates: list[tuple[PWLocator, Box]], anchor: Box, relation: str
) -> list[tuple[PWLocator, Box]]:
    """Filter and order candidates by their spatial relation to the anchor.

    "after" means reading order — to the right on the same line, or anywhere
    below — which is what "the field after the label" means to a person.

    Candidates that *contain* the anchor are dropped first. On a table-based
    page every caption sits inside a chain of nested cells, and an ancestor is
    never what "the field next to this label" means.
    """
    candidates = [c for c in candidates if not c[1].strictly_contains(anchor)]

    def same_row(box: Box) -> bool:
        return box.shares_row_with(anchor)

    if relation == "same_row":
        hits = [c for c in candidates if same_row(c[1])]
        return sorted(hits, key=lambda p: p[1].x)

    if relation == "after":
        hits = [
            c
            for c in candidates
            if (same_row(c[1]) and c[1].x >= anchor.x) or c[1].y >= anchor.bottom
        ]
        return sorted(hits, key=lambda p: (p[1].y, p[1].x))

    if relation == "before":
        hits = [
            c
            for c in candidates
            if (same_row(c[1]) and c[1].right <= anchor.right) or c[1].bottom <= anchor.y
        ]
        return sorted(hits, key=lambda p: (-p[1].y, -p[1].x))

    if relation == "below":
        hits = [
            c for c in candidates if c[1].overlaps_column_with(anchor) and c[1].y >= anchor.bottom
        ]
        return sorted(hits, key=lambda p: p[1].y)

    if relation == "above":
        hits = [
            c for c in candidates if c[1].overlaps_column_with(anchor) and c[1].bottom <= anchor.y
        ]
        return sorted(hits, key=lambda p: -p[1].y)

    if relation == "within":
        hits = [c for c in candidates if anchor.contains_point(c[1].center_x, c[1].center_y)]
        return sorted(hits, key=lambda p: (p[1].y, p[1].x))

    raise DriverError(f"unsupported relation {relation!r}")


def _text_hit(haystack: str, needle: str, exact: bool) -> bool:
    return haystack.strip() == needle if exact else needle.lower() in haystack.lower()


# --------------------------------------------------------------------------- #
# Snapshot parsing: perception -> addressable structure
# --------------------------------------------------------------------------- #

# `  - textbox "Member ID" [level=2] [box=114,65,111,21]: "12345"`
_NODE_RE = re.compile(
    r"^(?P<indent> *)- (?P<role>[a-zA-Z]+)"
    r'(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?: \[[^\]]*\])*)"
    r"(?::(?P<text>.*))?$"
)
_BOX_RE = re.compile(r"\[box=(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)\]")

# Structural scaffolding. Refs on these would triple the tree's size and give
# the model handles it has no use for — a <row> is not something you click.
_UNREFFED_ROLES = frozenset(
    {
        "table",
        "rowgroup",
        "row",
        "paragraph",
        "list",
        "listitem",
        "generic",
        "group",
        "region",
        "document",
        "none",
        "banner",
        "main",
        "navigation",
        "contentinfo",
    }
)

MAX_NAME_CHARS = 80


def parse_aria_snapshot(boxed: str) -> tuple[tuple[Element, ...], str]:
    """Turn a boxed aria snapshot into addressable elements plus the model's view.

    One parse yields both halves of the M2 design: `Element`s carrying the
    geometry the recorder needs to build a ladder, and a tree annotated with
    refs for the model to choose from.

    A container role's accessible name is the concatenation of everything
    inside it, which on this page runs to hundreds of characters. Those are
    truncated in the model's view only — they are noise to a model picking a
    control, and they would dominate the token budget.
    """
    elements: list[Element] = []
    lines_out: list[str] = []
    counter = 0

    for line in boxed.splitlines():
        match = _NODE_RE.match(line)
        if match is None:
            # Continuation of a multi-line text node: pass it through untouched.
            lines_out.append(line)
            continue

        indent = match.group("indent")
        role = match.group("role")
        name = match.group("name") or ""
        attrs = match.group("attrs") or ""
        text = (match.group("text") or "").strip().strip('"')

        box = None
        if (box_match := _BOX_RE.search(attrs)) is not None:
            box = Box(*(float(v) for v in box_match.groups()))

        ref = ""
        if role not in _UNREFFED_ROLES:
            counter += 1
            ref = f"e{counter}"
            elements.append(
                Element(
                    ref=ref,
                    role=role,
                    name=name,
                    text=text,
                    box=box,
                    depth=len(indent) // 2,
                )
            )

        # Rebuild the line for the model: refs in, boxes out.
        shown_name = name if len(name) <= MAX_NAME_CHARS else name[: MAX_NAME_CHARS - 1] + "…"
        rendered = f"{indent}- {role}"
        if name:
            rendered += f' "{shown_name}"'
        for attr in _OTHER_ATTRS_RE.findall(attrs):
            rendered += f" [{attr}]"
        if ref:
            rendered += f" [{ref}]"
        if text:
            rendered += f": {text}"
        lines_out.append(rendered)

    return tuple(elements), "\n".join(lines_out)


_OTHER_ATTRS_RE = re.compile(r"\[((?!box=)[^\]]*)\]")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
