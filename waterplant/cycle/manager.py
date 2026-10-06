"""Drives one durable, resumable treatment cycle.

The manager owns the cycle state machine. It executes steps in fixed order,
checkpoints each result before continuing, stops at a safe point on any
failure, and only continues after an explicit operator resume. All transitions
and recovery actions are written to the cycle journal.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from waterplant.cycle.journal import CycleJournal
from waterplant.cycle.model import (
    REASON_OPERATOR_ABORT,
    REASON_PH_NOT_STABLE,
    REASON_PROCESS_CRASH,
    REASON_STEP_FAILED,
    CycleStatus,
    StepDef,
    StepRecord,
    StepStatus,
    step_def,
)
from waterplant.cycle.persist import CycleDoc, CycleStore
from waterplant.intake import Sensor, mix


class CycleConflict(Exception):
    """Raised when a cycle operation is not allowed in the current state."""


@dataclass(frozen=True)
class StepOutcome:
    """Result of executing one step and whether it applied a physical effect."""

    result: dict[str, object]
    applied: bool = True


class CycleManager:
    """Executes and recovers treatment cycles against the live runtime."""

    def __init__(self, rt) -> None:  # noqa: ANN001 - runtime wiring avoids a cycle
        self._rt = rt
        self._store = rt.store
        self._cycles = CycleStore(rt.store)
        self._journal = CycleJournal(rt.store)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ start

    def start(self, inputs: CycleInputs) -> CycleDoc:
        """Validate inputs, create a cycle and run it until done or stopped."""

        inputs.validate(self._rt)
        with self._lock:
            active = self._reconcile(self._cycles.load_active())
            if active is not None:
                raise CycleConflict(
                    f"cycle {active.cycle_id} is {active.status}; resume or abort it first"
                )
            doc = self._cycles.new(uuid.uuid4().hex, inputs.as_payload())
            self._event_and_persist(doc, "cycle.started", "cycle created")
            return self._advance(doc)

    # ----------------------------------------------------------------- resume

    def resume(self, operator: str = "") -> CycleDoc:
        """Continue an interrupted cycle from its breakpoint (operator confirmed)."""

        with self._lock:
            doc = self._reconcile(self._cycles.load_active())
            if doc is None:
                raise CycleConflict("no active cycle to resume")
            if doc.status != CycleStatus.INTERRUPTED.value:
                raise CycleConflict(f"cycle is {doc.status}, not interrupted")
            doc.resumed_count += 1
            breakpoint = doc.breakpoint or (doc.first_unfinished().name if doc.first_unfinished() else "")
            self._event_and_persist(
                doc,
                "cycle.resumed",
                f"operator confirmed resume at {breakpoint}",
                step=breakpoint,
                operator=operator,
            )
            return self._advance(doc)

    # ------------------------------------------------------------------ abort

    def abort(self, operator: str = "") -> CycleDoc:
        """Discard the remainder but retain the completed portion on record."""

        with self._lock:
            doc = self._reconcile(self._cycles.load_active())
            if doc is None:
                raise CycleConflict("no active cycle to abort")
            completed = [
                record.name
                for record in doc.steps
                if record.status == StepStatus.SUCCEEDED.value
            ]
            detail = "operator aborted; retained steps: " + (",".join(completed) or "none")
            _, extra = self._journal.build(
                doc.cycle_id, "cycle.aborted", detail, operator=operator
            )
            doc.interrupt_reason = REASON_OPERATOR_ABORT
            doc.interrupt_detail = "operator aborted the cycle"
            self._cycles.finish(doc, CycleStatus.ABORTED.value, extra=extra)
            return doc

    # ------------------------------------------------------------------ reads

    def active(self) -> CycleDoc | None:
        with self._lock:
            return self._reconcile(self._cycles.load_active())

    def history(self) -> list[CycleDoc]:
        with self._lock:
            return self._cycles.history()

    def events(self, cycle_id: str | None = None):
        with self._lock:
            return self._journal.events(cycle_id)

    # -------------------------------------------------------------- internals

    def _advance(self, doc: CycleDoc) -> CycleDoc:
        """Run pending steps from the breakpoint until complete or interrupted."""

        while True:
            target = doc.first_unfinished()
            if target is None:
                return self._complete(doc)
            definition = step_def(target.name)

            gate = self._gate_failure(definition, doc)
            if gate is not None:
                self._stop(doc, target, gate, blocked=True)
                return doc

            self._mark_running(doc, target)
            try:
                outcome = self._execute(definition, doc)
            except Exception as exc:  # noqa: BLE001 - any device failure parks safely
                self._stop(doc, target, str(exc), blocked=False)
                return doc
            self._commit_success(doc, target, outcome)
            if target.name == "audit":
                return self._complete(doc)

    def _gate_failure(self, definition: StepDef, doc: CycleDoc) -> str | None:
        if definition.requires_ph_stable and not self._rt.stabilizer.is_stable():
            verdict = self._rt.stabilizer.stabilize()
            return (
                "ph not stable before chlorine: "
                f"value={verdict.value:.4f} adjustment={verdict.adjustment:.4f}"
            )
        return None

    def _mark_running(self, doc: CycleDoc, record: StepRecord) -> None:
        record.status = StepStatus.RUNNING.value
        record.attempts += 1
        self._persist(doc)

    def _stop(self, doc: CycleDoc, record: StepRecord, detail: str, blocked: bool) -> None:
        """Park the cycle at the current safe point and record the breakpoint."""

        record.status = StepStatus.BLOCKED.value if blocked else StepStatus.FAILED.value
        record.error = detail
        reason = REASON_PH_NOT_STABLE if blocked else REASON_STEP_FAILED
        doc.status = CycleStatus.INTERRUPTED.value
        doc.breakpoint = record.name
        doc.interrupt_reason = reason
        doc.interrupt_detail = detail
        kind = "cycle.blocked" if blocked else "cycle.failed"
        self._event_and_persist(doc, kind, detail, step=record.name)

    def _commit_success(self, doc: CycleDoc, record: StepRecord, outcome: StepOutcome) -> None:
        record.status = StepStatus.SUCCEEDED.value
        record.error = ""
        record.result = dict(outcome.result)
        applied = "applied" if outcome.applied else "already_applied"
        self._event_and_persist(
            doc,
            "step.succeeded",
            f"{record.name} {applied}",
            step=record.name,
        )

    def _complete(self, doc: CycleDoc) -> CycleDoc:
        _, extra = self._journal.build(doc.cycle_id, "cycle.completed", "cycle finished")
        self._cycles.finish(doc, CycleStatus.COMPLETED.value, extra=extra)
        return doc

    def _reconcile(self, doc: CycleDoc | None) -> CycleDoc | None:
        """Convert a cycle left ``running`` by a crash into an interrupted one.

        Never auto-runs anything: a crashed step is parked and waits for an
        explicit operator resume.
        """

        if doc is None or doc.status != CycleStatus.RUNNING.value:
            return doc
        for record in doc.steps:
            if record.status == StepStatus.RUNNING.value:
                record.status = StepStatus.FAILED.value
                record.error = "process interrupted before the step result was committed"
                doc.status = CycleStatus.INTERRUPTED.value
                doc.breakpoint = record.name
                doc.interrupt_reason = REASON_PROCESS_CRASH
                doc.interrupt_detail = record.error
                self._event_and_persist(
                    doc,
                    "cycle.interrupted",
                    doc.interrupt_detail,
                    step=record.name,
                )
                break
        return doc

    def _persist(self, doc: CycleDoc) -> None:
        self._cycles.save(doc)

    def _event_and_persist(
        self, doc: CycleDoc, kind: str, detail: str, step: str = "", operator: str = ""
    ) -> None:
        """Append a journal event and checkpoint the cycle in one atomic write."""

        _, extra = self._journal.build(
            doc.cycle_id, kind, detail, step=step, operator=operator
        )
        self._cycles.save(doc, extra=extra)

    # ----------------------------------------------------------------- steps

    def _ref(self, doc: CycleDoc, name: str) -> str:
        return f"{doc.cycle_id}:{name}"

    def _execute(self, definition: StepDef, doc: CycleDoc) -> StepOutcome:
        handler = getattr(self, f"_step_{definition.name}")
        return handler(doc)

    def _step_intake(self, doc: CycleDoc) -> StepOutcome:
        flow = float(doc.inputs["flow"])
        samples = [float(item) for item in doc.inputs.get("samples", [])]
        turbidity = mix(samples)
        self._rt.flow_repository.record(Sensor(flow=flow, turbidity=turbidity))
        self._rt.trend.record(flow)
        return StepOutcome({"flow": flow, "turbidity": turbidity})

    def _step_coag(self, doc: CycleDoc) -> StepOutcome:
        flow = float(doc.inputs["flow"])
        dose, applied = self._rt.coag_doser.dose_once(flow, self._ref(doc, "coag"))
        return StepOutcome({"dose": dose, "applied": applied}, applied=applied)

    def _step_turbidity(self, doc: CycleDoc) -> StepOutcome:
        samples = [float(item) for item in doc.inputs.get("samples", [])]
        dose = self._rt.sampler.judge(samples)
        return StepOutcome({"dose": dose, "verdict": self._rt.sampler.state().last_verdict})

    def _step_ph(self, doc: CycleDoc) -> StepOutcome:
        verdict = self._rt.stabilizer.stabilize()
        return StepOutcome(
            {
                "value": verdict.value,
                "stable": verdict.stable,
                "adjustment": verdict.adjustment,
                "direction": verdict.direction,
            }
        )

    def _step_residual(self, doc: CycleDoc) -> StepOutcome:
        demand = float(doc.inputs["demand"])
        target = self._rt.well.update_residual_demand(demand)
        return StepOutcome({"target": target, "demand": demand})

    def _step_chlor(self, doc: CycleDoc) -> StepOutcome:
        dose, applied = self._rt.chlor_doser.apply_once(self._ref(doc, "chlor"))
        return StepOutcome({"dose": dose, "applied": applied}, applied=applied)

    def _step_clearwell(self, doc: CycleDoc) -> StepOutcome:
        level = float(doc.inputs["level"])
        minimum = self._rt.well.adjust_level(level, self._rt.inlet, self._rt.outlet)
        return StepOutcome({"min_level": minimum, "level": self._rt.well.level()})

    def _step_rotation(self, doc: CycleDoc) -> StepOutcome:
        order = self._rt.backwash.order_rotation()
        duty = self._rt.bank.on_duty() or ""
        return StepOutcome({"rotation": order, "on_duty": duty})

    def _step_backwash(self, doc: CycleDoc) -> StepOutcome:
        bed_id = str(doc.inputs.get("bed_id", "") or "")
        if not bed_id:
            return StepOutcome({"backwash": "", "started": False})
        self._rt.backwash.start(bed_id)
        return StepOutcome(
            {"backwash": bed_id, "started": True, "drains": self._rt.backwash.drain_count()}
        )

    def _step_quota(self, doc: CycleDoc) -> StepOutcome:
        amount = float(doc.inputs["amount"])
        value, applied = self._rt.accumulator.add_once(amount, self._ref(doc, "quota"))
        return StepOutcome({"quota": value, "applied": applied}, applied=applied)

    def _step_audit(self, doc: CycleDoc) -> StepOutcome:
        return StepOutcome({"closed": True})
