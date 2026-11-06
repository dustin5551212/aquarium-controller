"""Threshold alerts with hold-time debouncing, and outbound notification."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .config import AlertCfg, Config, NotifyCfg
from .model import Alert, Fault, Reading
from .storage import Storage

log = logging.getLogger("aquarium.alerts")


class Notifier:
    """Fan alerts out to a webhook and/or a shell command.

    The command template is formatted with shlex-quoted values, so a message
    with spaces cannot smuggle extra arguments into the shell.
    """

    def __init__(self, cfg: NotifyCfg) -> None:
        self.cfg = cfg
        self._sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def _allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._sent.get(key, 0.0)
            if now - last < self.cfg.cooldown_s:
                return False
            self._sent[key] = now
            return True

    def send(self, alert: Alert, event: str) -> None:
        if not self._allowed(alert.rule):
            return
        text = f"[{alert.severity}] {alert.message} ({alert.metric}={alert.value:g}) — {event}"
        if self.cfg.webhook:
            threading.Thread(target=self._post, args=(alert, text), daemon=True).start()
        if self.cfg.command:
            threading.Thread(target=self._run, args=(alert, text), daemon=True).start()

    def _post(self, alert: Alert, text: str) -> None:
        body = json.dumps({
            "text": text,
            "message": alert.message,
            "rule": alert.rule,
            "severity": alert.severity,
            "metric": alert.metric,
            "value": alert.value,
            "ts": time.time(),
        }).encode()
        req = urllib.request.Request(
            self.cfg.webhook, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                log.info("webhook %s -> %s", self.cfg.webhook, resp.status)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("webhook failed: %s", exc)

    def _run(self, alert: Alert, text: str) -> None:
        command = self.cfg.command.format(
            message=shlex.quote(text),
            severity=shlex.quote(alert.severity),
            metric=shlex.quote(alert.metric),
            value=shlex.quote(f"{alert.value:g}"),
        )
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, timeout=30)
            if proc.returncode != 0:
                log.warning("notify command failed (%s): %s",
                            proc.returncode, proc.stderr.decode()[:200])
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("notify command failed: %s", exc)


class AlertEngine:
    def __init__(self, cfg: Config, storage: Storage | None = None,
                 notifier: Notifier | None = None) -> None:
        self.cfg = cfg
        self.storage = storage
        self.notifier = notifier or Notifier(cfg.notify)
        self.rules: dict[str, AlertCfg] = {rule.rule_id: rule for rule in cfg.alerts}
        self.active: dict[str, Alert] = {}
        self._pending: dict[str, float] = {}
        self._fault_pending: dict[str, float] = {}
        self.fault_holds: dict[str, float] = {}

    # -- evaluation ------------------------------------------------------
    def evaluate(self, readings: dict[str, Reading], faults: list[Fault],
                 now: float) -> list[tuple[Alert, str]]:
        transitions: list[tuple[Alert, str]] = []

        for rule_id, rule in self.rules.items():
            reading = readings.get(rule.metric)
            condition = False
            value = float("nan")
            if reading is not None:
                value = reading.value
                if rule.above is not None:
                    condition = value >= rule.above
                elif rule.below is not None:
                    condition = value <= rule.below

            if condition:
                first = self._pending.setdefault(rule_id, now)
                if rule_id not in self.active and now - first >= rule.hold_s:
                    alert = Alert(rule_id, rule.severity, rule.message, rule.metric, value, first)
                    self.active[rule_id] = alert
                    self._log(alert, "raised")
                    self.notifier.send(alert, "raised")
                    transitions.append((alert, "raised"))
            else:
                self._pending.pop(rule_id, None)
                if rule_id in self.active:
                    alert = self.active.pop(rule_id)
                    alert.active = False
                    self._log(alert, "cleared")
                    self.notifier.send(alert, "cleared")
                    transitions.append((alert, "cleared"))

        transitions.extend(self._evaluate_faults(faults, now))
        return transitions

    def _evaluate_faults(self, faults: list[Fault], now: float) -> list[tuple[Alert, str]]:
        out: list[tuple[Alert, str]] = []
        seen: set[str] = set()
        for fault in faults:
            seen.add(fault.key)
            severity = fault.severity if fault.severity in ("info", "warning", "critical") else "warning"
            hold = 0.0 if severity == "critical" else self.fault_holds.get(fault.key, 30.0)
            if fault.key in self.active:
                self.active[fault.key].value = 1.0
                continue
            first = self._fault_pending.setdefault(fault.key, now)
            if now - first < hold:
                continue
            alert = Alert(fault.key, severity, fault.message,
                          fault.metric or "", 1.0, first)
            self.active[fault.key] = alert
            self._log(alert, "raised")
            self.notifier.send(alert, "raised")
            out.append((alert, "raised"))

        for key in list(self.active):
            if key in seen or key in self.rules:
                continue
            alert = self.active.pop(key)
            alert.active = False
            self._fault_pending.pop(key, None)
            self._log(alert, "cleared")
            self.notifier.send(alert, "cleared")
            out.append((alert, "cleared"))
        return out

    def _log(self, alert: Alert, state: str) -> None:
        log.warning("alert %s: %s (%s)", state, alert.message, alert.rule)
        if self.storage is not None:
            self.storage.add_alert(alert, state)

    # -- reporting -------------------------------------------------------
    def snapshot(self, now: float) -> list[dict]:
        ordered = sorted(
            self.active.values(),
            key=lambda a: ({"critical": 0, "warning": 1, "info": 2}.get(a.severity, 3), -a.since),
        )
        return [a.as_dict(now) for a in ordered]

    def clear(self, rule: str | None = None) -> int:
        """Drop active *equipment* alerts after the operator has fixed them.

        Threshold alerts are re-raised on the next evaluation while the
        condition still holds, so clearing them is harmless.
        """
        keys = [rule] if rule else list(self.active)
        removed = 0
        for key in keys:
            alert = self.active.pop(key, None)
            if alert:
                alert.active = False
                self._log(alert, "acknowledged")
                removed += 1
            self._pending.pop(key, None)
            self._fault_pending.pop(key, None)
        return removed
