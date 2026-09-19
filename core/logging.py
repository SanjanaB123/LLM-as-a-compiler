"""
STRUCTURED, REDACTING EVIDENCE LOG (M2 writes it, M5 hardens the redaction).

One logger for discovery, replay and handoff. Every run gets a directory under
`/evidence/` holding a machine-readable `events.jsonl` and a human-readable
`transcript.md`, because the two audiences are different: a test asserts against
the first, a reviewer reads the second.

**Redaction happens on the way out, at the single choke point.** Every value
bound to a parameter marked `sensitive` is registered here, and no line reaches
either file without passing through `redact`. Masking at call sites is the
version of this that works until someone adds a log line and forgets.

Redaction covers the recorded value and its obvious spellings — a member id
appears in the tree as a field value, inside a concatenated row name, and in
prose the model wrote. Masking the literal substring everywhere catches all
three. It cannot catch a value the surface reformats (12345 shown as 12,345),
which is a real limit worth stating rather than papering over.

Note: this module shadows the stdlib `logging` name inside the repo, but Python
3 absolute imports mean `import logging` elsewhere still finds the stdlib.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EVIDENCE_ROOT = Path("evidence")
MASK = "***"
MIN_MASKABLE_LEN = 3


@dataclass
class EvidenceLog:
    """One run's evidence directory.

    `sensitive_values` are literals to mask. Discovery registers whatever it
    types into a field the goal called sensitive; replay (M5) registers every
    argument bound to a `sensitive` parameter, straight off the artifact.
    """

    run_id: str
    kind: str
    root: Path = EVIDENCE_ROOT
    sensitive_values: set[str] = field(default_factory=set)
    _lines: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.root / f"{self.kind}-{self.run_id}"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript.md"

    # -- redaction --------------------------------------------------------- #

    def mark_sensitive(self, *values: str) -> None:
        for value in values:
            if value and len(str(value)) >= MIN_MASKABLE_LEN:
                self.sensitive_values.add(str(value))

    def redact(self, value: Any) -> Any:
        """Mask every registered sensitive literal, at any depth."""
        if isinstance(value, str):
            out = value
            # Longest first, so a value containing another is masked whole.
            for secret in sorted(self.sensitive_values, key=len, reverse=True):
                out = out.replace(secret, MASK)
            return out
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value

    # -- writing ----------------------------------------------------------- #

    def event(self, kind: str, **fields: Any) -> dict:
        record = self.redact({"ts": round(time.time(), 3), "event": kind, **fields})
        with self.events_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    def narrate(self, line: str = "") -> None:
        """Append to the human-readable transcript."""
        self._lines.append(self.redact(line))
        self.transcript_path.write_text("\n".join(self._lines) + "\n")

    def heading(self, text: str, level: int = 2) -> None:
        self.narrate("")
        self.narrate(f"{'#' * level} {self.redact(text)}")
        self.narrate("")

    def table(self, rows: Iterable[tuple[str, Any]]) -> None:
        for key, value in rows:
            self.narrate(f"- **{key}**: {self.redact(str(value))}")


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
