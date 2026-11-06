"""Core value objects shared by sensors, control loops, storage and the API."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Reading:
    metric: str
    value: float
    unit: str
    ts: float
    quality: float = 1.0          # 1.0 = trusted, <1 = degraded (e.g. stale probe)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "value": round(self.value, 4),
            "unit": self.unit,
            "ts": round(self.ts, 3),
            "quality": self.quality,
            "source": self.source,
        }


@dataclass(slots=True)
class ActuatorStatus:
    id: str
    kind: str
    on: bool
    since: float
    reason: str = ""
    mode: str = "auto"            # auto | manual
    override_until: float = 0.0

    def as_dict(self, now: float) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "on": self.on,
            "since": round(self.since, 3),
            "on_for_s": round(max(0.0, now - self.since), 1) if self.on else 0.0,
            "reason": self.reason,
            "mode": self.mode,
            "override_until": round(self.override_until, 3),
            "override_remaining_s": round(max(0.0, self.override_until - now), 1),
        }


@dataclass(slots=True)
class Alert:
    rule: str
    severity: str
    message: str
    metric: str
    value: float
    since: float
    active: bool = True

    def as_dict(self, now: float) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "metric": self.metric,
            "value": round(self.value, 3),
            "since": round(self.since, 3),
            "age_s": round(max(0.0, now - self.since), 1),
            "active": self.active,
        }


@dataclass(slots=True)
class Command:
    """A desired actuator state produced by a control loop."""

    actuator: str
    on: bool
    reason: str
    source: str = "auto"
    duration_s: float | None = None   # for dosing pumps: auto-off after N seconds


@dataclass(slots=True)
class Fault:
    key: str
    message: str
    severity: str = "warning"
    actuator: str | None = None
    metric: str | None = None


@dataclass(slots=True)
class Snapshot:
    tank: dict
    now: float
    started: float
    mode: str
    readings: dict[str, dict] = field(default_factory=dict)
    actuators: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    dosing: dict[str, dict] = field(default_factory=dict)
    loops: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tank": self.tank,
            "now": round(self.now, 3),
            "uptime_s": round(max(0.0, self.now - self.started), 1),
            "mode": self.mode,
            "readings": self.readings,
            "actuators": self.actuators,
            "alerts": self.alerts,
            "dosing": self.dosing,
            "loops": self.loops,
        }
