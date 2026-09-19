"""
SAFETY (milestone M5) — brief 3.4.

Three mechanisms, each placed where it cannot be bypassed rather than where it
is most convenient.

**The allowlist is enforced inside the Driver.** Not in replay, not in the
agent, not in the CLI — inside the one object that actually touches the
surface. A check in the caller is a check that the next caller forgets. Because
every action funnels through the seam, a flow physically cannot act off-list,
whatever its steps say and whatever a model decided mid-run.

It constrains three things, because "which site" is not enough on its own:

    origins   which hosts may be touched at all
    routes    which paths on those hosts — a bank's admin console and its
              member search share an origin, and only one of them is in scope
    actions   which verbs are permitted — a capability that should only ever
              read has no business being able to type

**Irreversible actions require explicit confirmation** (see `replay.py`). The
default is refusal: opening an account should need someone to say yes, not need
someone to remember to say no.

**Redaction is structural** (see `schema.py` and `logging.py`). The artifact
holds `value_ref`, never a value, so a sensitive value has nowhere to be
written down; and the evidence logger masks the values of parameters the
capability itself declares sensitive.

Why an allowlist over a blocklist: a blocklist is a list of the harm you
already thought of. Everything you did not think of is permitted by default,
which is the wrong default for software driving a bank.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from urllib.parse import urlparse

from core.schema import Action, TargetSurface

ALL_ACTIONS = frozenset(a.value for a in Action)


class ActionNotAllowed(Exception):
    """Refused by policy. Raised inside the Driver, so nothing can route around it."""


@dataclass(frozen=True)
class Allowlist:
    """What a run is permitted to touch. Frozen: policy is not adjusted mid-run."""

    origins: tuple[str, ...]
    routes: tuple[str, ...] = ("*",)
    actions: frozenset[str] = ALL_ACTIONS

    @classmethod
    def from_target(cls, target: TargetSurface) -> Allowlist:
        """Read the policy off the capability's own tenant slot."""
        return cls(
            origins=tuple(target.allowed_origins),
            routes=tuple(target.allowed_routes),
            actions=frozenset(target.allowed_actions or ALL_ACTIONS),
        )

    @classmethod
    def read_only(cls, origins: list[str] | tuple[str, ...]) -> Allowlist:
        """A capability that may look but not touch."""
        return cls(origins=tuple(origins), actions=frozenset({"navigate", "read"}))

    # -- checks ------------------------------------------------------------ #

    def check_navigation(self, url: str) -> None:
        if not self.origins:
            return  # no policy configured; the caller has opted out explicitly
        if not any(url.startswith(origin) for origin in self.origins):
            raise ActionNotAllowed(
                f"navigation to {url!r} is outside the permitted origins "
                f"{list(self.origins)}"
            )
        path = urlparse(url).path or "/"
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.routes):
            raise ActionNotAllowed(
                f"navigation to {path!r} is outside the permitted routes "
                f"{list(self.routes)} — the origin is allowed but this page is not"
            )

    def check_action(self, action: str) -> None:
        if action not in self.actions:
            raise ActionNotAllowed(
                f"the action {action!r} is not permitted here; this run may only "
                f"{sorted(self.actions)}"
            )

    def describe(self) -> str:
        return (
            f"origins={list(self.origins)} routes={list(self.routes)} "
            f"actions={sorted(self.actions)}"
        )


UNRESTRICTED = Allowlist(origins=())
