"""The plan: a deterministic, hashable, storable description of what would be done.

Same input -> same plan, byte for byte. The plan id is the sha256 of its canonical JSON
(created_at is stored next to it, not inside it). Approval is meant to refer to that id,
so what runs is exactly what was reviewed."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field

from .classify import Effect, effect_of

PHASES = ("precheck", "change", "verify")


@dataclass
class Step:
    phase: str
    command: str
    purpose: str
    expect: str | None = None        # precheck: text that must still be present; verify: must be present after
    rollback: str | None = None      # command that restores the previous value
    effect: str = ""

    def __post_init__(self):
        if self.phase not in PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        self.effect = effect_of(self.command).value
        if self.effect == Effect.UNKNOWN.value:
            raise ValueError(f"command refused (not classified): {self.command!r}")
        if self.rollback is not None and effect_of(self.rollback) != Effect.UPDATE:
            raise ValueError(f"rollback must be a classified update: {self.rollback!r}")
        if self.phase == "change" and self.effect != Effect.UPDATE.value:
            raise ValueError("a change step must be an update")
        if self.phase != "change" and self.effect != Effect.READ.value:
            raise ValueError(f"{self.phase} steps must be reads")


@dataclass
class DevicePlan:
    device: str
    host: str
    driver: str
    state_fingerprint: str                 # sha256 of what the driver read (secrets excluded)
    steps: list[Step] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)    # already as desired
    skipped: list[str] = field(default_factory=list)      # deliberately left alone (excluded, disabled ...)
    unsupported: list[str] = field(default_factory=list)  # driver cannot do it
    notes: list[str] = field(default_factory=list)        # things the reviewer should know (e.g. no automatic rollback)
    error: str | None = None

    @property
    def changes(self) -> int:
        return sum(1 for s in self.steps if s.phase == "change")


@dataclass
class Plan:
    intent: dict
    devices: list[DevicePlan]
    writes_allowed: bool = False           # the prototype never executes

    def canonical(self) -> dict:
        return {"intent": self.intent,
                "devices": [dataclasses.asdict(d) for d in sorted(self.devices, key=lambda d: d.device)]}

    @property
    def plan_id(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def to_json(self, created_at: str | None = None) -> str:
        doc = {"plan_id": self.plan_id, "created_at": created_at, "writes_allowed": self.writes_allowed,
               **self.canonical()}
        return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def summary(self) -> str:
        lines = [f"plan {self.plan_id[:12]}  {self.intent.get('intent')}  {self.intent.get('servers') or self.intent.get('host') or self.intent.get('allowed')}"]
        for d in sorted(self.devices, key=lambda d: d.device):
            if d.error:
                lines.append(f"  {d.device:<22} ERROR  {d.error}")
                continue
            lines.append(f"  {d.device:<22} {d.driver:<10} change={d.changes} ok={len(d.unchanged)} "
                         f"skipped={len(d.skipped)} unsupported={len(d.unsupported)}")
            for s in d.steps:
                if s.phase == "change":
                    lines.append(f"      {s.command}")
            for u in d.unsupported:
                lines.append(f"      ! {u}")
            for n in d.notes:
                lines.append(f"      * {n}")
        return "\n".join(lines)
