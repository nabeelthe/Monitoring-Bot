"""Runs every monitor on its own cadence, with baseline suppression and health alerts."""

import asyncio
import logging
import random
import time

from .alerts import Alert, AlertEngine, ESCALATE
from .monitors.base import Context, FetchError

log = logging.getLogger("nuva.scheduler")

FAILURE_ALERT_AFTER = 5  # consecutive failures before we tell the user


class MonitorStatus:
    def __init__(self, monitor):
        self.monitor = monitor
        self.last_run: float = 0.0
        self.last_ok: float = 0.0
        self.last_error: str = ""
        self.consecutive_failures = 0
        self.alerts_sent = 0
        self.polls = 0


class Scheduler:
    def __init__(self, monitors, engine: AlertEngine, notifier, state, session, config):
        self.monitors = monitors
        self.engine = engine
        self.notifier = notifier
        self.state = state
        self.ctx = Context(session, state, config)
        self.statuses = {m.name: MonitorStatus(m) for m in monitors}
        self.started_at = time.time()
        self._tasks: list[asyncio.Task] = []

    def start(self):
        for i, monitor in enumerate(self.monitors):
            self._tasks.append(asyncio.create_task(self._run(monitor, stagger=i * 2.0), name=f"mon:{monitor.name}"))
        self._tasks.append(asyncio.create_task(self._saver(), name="state-saver"))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.state.save(force=True)

    async def run_once(self, monitor) -> tuple[int, int, str]:
        """Poll a single monitor now. Returns (found, sent, error)."""
        status = self.statuses[monitor.name]
        try:
            alerts = await monitor.poll(self.ctx)
        except Exception as exc:
            return 0, 0, str(exc)
        sent = await self._dispatch(monitor, status, alerts)
        return len(alerts), sent, ""

    async def _dispatch(self, monitor, status: MonitorStatus, alerts: list[Alert]) -> int:
        baselined = self.state.is_baselined(monitor.name)
        sent = 0
        for alert in alerts[:15]:  # hard cap per cycle to avoid alert storms
            verdict = self.engine.evaluate(alert)
            if not verdict.send:
                continue
            if not baselined:
                continue  # first successful poll only records history
            await self.notifier.broadcast(self.engine.format(alert, verdict), loud=verdict.loud)
            sent += 1
            status.alerts_sent += 1
        if not baselined:
            self.state.set_baselined(monitor.name)
            if alerts:
                log.info("%s: baseline established, %d existing items recorded silently", monitor.name, len(alerts))
        return sent

    async def _run(self, monitor, stagger: float):
        status = self.statuses[monitor.name]
        await asyncio.sleep(stagger + random.uniform(0, 2))
        while True:
            status.last_run = time.time()
            status.polls += 1
            try:
                alerts = await monitor.poll(self.ctx)
                await self._dispatch(monitor, status, alerts)
                if status.consecutive_failures >= FAILURE_ALERT_AFTER:
                    await self._system_alert(f"✅ {monitor.name} recovered after {status.consecutive_failures} failed polls.")
                status.consecutive_failures = 0
                status.last_ok = time.time()
                status.last_error = ""
            except asyncio.CancelledError:
                raise
            except (FetchError, RuntimeError, KeyError, TypeError, ValueError) as exc:
                status.consecutive_failures += 1
                status.last_error = str(exc)[:300]
                log.warning("%s poll failed (%d in a row): %s", monitor.name, status.consecutive_failures, exc)
                if status.consecutive_failures == FAILURE_ALERT_AFTER:
                    await self._system_alert(
                        f"⚠️ {monitor.name} has failed {FAILURE_ALERT_AFTER} polls in a row.\nLast error: {status.last_error}"
                    )
            except Exception:
                status.consecutive_failures += 1
                log.exception("%s poll crashed", monitor.name)
                status.last_error = "internal error (see logs)"
            # modest backoff when a source is down, capped at 3× cadence
            delay = monitor.cadence * min(1 + 0.5 * status.consecutive_failures, 3)
            await asyncio.sleep(delay + random.uniform(0, monitor.cadence * 0.05))

    async def _system_alert(self, text: str):
        alert = Alert(monitor="Bot health", layer="system", title=text, priority=ESCALATE, filterable=False)
        verdict = self.engine.evaluate(alert)
        await self.notifier.broadcast(self.engine.format(alert, verdict), loud=False)

    async def _saver(self):
        while True:
            await asyncio.sleep(30)
            try:
                self.state.save()
            except OSError:
                log.exception("state save failed")
