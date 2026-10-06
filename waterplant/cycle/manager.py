"""Durable, resumable execution of one treatment cycle."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from waterplant.backwash.controller import MARKER_KEY_PREFIX
from waterplant.clearwell import LEVEL_KEY
from waterplant.filter.report import BANK_KEY
from waterplant.intake import FLOW_KEY, FLOW_TREND_KEY, TURBIDITY_KEY
from waterplant.ns import Stage, treatment_line

if TYPE_CHECKING:  # pragma: no cover - type only
    from waterplant.console.runtime import Runtime

CYCLE_KEY = "cycle:active"
CYCLE_HISTORY_KEY = "cycle:history"

STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"

STEP_PENDING = "pending"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_BLOCKED = "blocked"

RECOVERY_POLICY = "operator_confirmation_required"
FAILURE_POLICY = "retain_completed_and_stop"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CycleError(ValueError):
    """A request that conflicts with the durable cycle state."""


class CycleManager:
    """Persist every step boundary so a partial cycle can be recovered."""

    def __init__(self, rt: "Runtime") -> None:
        self._rt = rt
        self._store = rt.store
        self._auditor = rt.auditor

    def get(self) -> dict[str, Any] | None:
        raw, present = self._store.get(CYCLE_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._rt.cycle_lock:
            cycle_id = str(payload.get("cycle_id") or uuid.uuid4().hex)
            existing = self.get()
            if existing is not None:
                if existing.get("status") == STATUS_COMPLETED and existing.get("id") != cycle_id:
                    self._archive(existing)
                elif existing.get("status") == STATUS_COMPLETED and existing.get("id") == cycle_id:
                    raise CycleError(f"cycle {cycle_id} is already complete; start a new cycle")
                elif existing.get("status") != STATUS_COMPLETED:
                    raise CycleError(
                        "an interrupted cycle is waiting for operator recovery; POST /cycle/recover"
                    )
            stages = treatment_line().stages
            state = {
                "id": cycle_id,
                "status": STATUS_RUNNING,
                "recovery_policy": RECOVERY_POLICY,
                "failure_policy": FAILURE_POLICY,
                "current_index": 0,
                "created_at": _now(),
                "updated_at": _now(),
                "input": self._jsonable(payload),
                "steps": [
                    {
                        "stage": stage.value,
                        "index": index,
                        "status": STEP_PENDING,
                        "attempts": 0,
                        "result": None,
                        "error": "",
                        "started_at": "",
                        "completed_at": "",
                    }
                    for index, stage in enumerate(stages)
                ],
                "last_error": "",
                "recoveries": [],
            }
            self._save(state, [])
            return self.advance()

    def advance(self) -> dict[str, Any]:
        state = self._require()
        if state["status"] != STATUS_RUNNING:
            return state
        while state["status"] == STATUS_RUNNING:
            index = int(state["current_index"])
            if index >= len(state["steps"]):
                state["status"] = STATUS_COMPLETED
                state["updated_at"] = _now()
                self._save(state, [self._event(state, "cycle.completed", "cycle completed")])
                break
            step = state["steps"][index]
            if step["status"] == STEP_COMPLETED:
                state["current_index"] = index + 1
                continue
            step["status"] = STEP_PENDING
            step["attempts"] = int(step.get("attempts", 0)) + 1
            step["started_at"] = _now()
            stage = Stage(step["stage"])
            events: list[dict[str, str]] = []
            try:
                result = self._execute_step(state, stage)
                step["result"] = self._jsonable(result)
                step["status"] = STEP_COMPLETED
                step["error"] = ""
                step["completed_at"] = _now()
                state["current_index"] = index + 1
                events.append(self._event(state, f"cycle.step.{stage.value}.completed", json.dumps(result, sort_keys=True)))
            except _Blocked as blocked:
                step["status"] = STEP_BLOCKED
                step["result"] = self._jsonable(blocked.result)
                step["error"] = str(blocked)
                step["completed_at"] = _now()
                state["status"] = STATUS_BLOCKED
                state["last_error"] = str(blocked)
                events.append(self._event(state, f"cycle.step.{stage.value}.blocked", str(blocked)))
            except Exception as exc:  # safe stop: later steps must not run
                step["status"] = STEP_FAILED
                step["error"] = str(exc)
                step["completed_at"] = _now()
                state["status"] = STATUS_FAILED
                state["last_error"] = str(exc)
                events.append(self._event(state, f"cycle.step.{stage.value}.failed", str(exc)))
            self._save(state, events)
        return state

    def recover(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._rt.cycle_lock:
            return self._recover_locked(payload)

    def _recover_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._require()
        if state["status"] == STATUS_COMPLETED:
            raise CycleError(f"cycle {state['id']} is already complete")
        if payload.get("confirmed") is not True:
            raise CycleError("recovery requires confirmed=true after the duty operator reviews the safe point")
        operator = str(payload.get("operator") or "").strip()
        if not operator:
            raise CycleError("recovery requires a non-empty operator id")
        cycle_id = str(payload.get("cycle_id") or state["id"])
        if cycle_id != state["id"]:
            raise CycleError("cycle_id does not match the interrupted cycle")
        state["status"] = STATUS_RUNNING
        state["updated_at"] = _now()
        state["last_error"] = ""
        state.setdefault("recoveries", []).append(
            {"at": _now(), "operator": operator, "action": "resume"}
        )
        event = self._event(state, "cycle.recovery.resume", operator)
        self._save(state, [event])
        return self.advance()

    def response_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        results = {
            step["stage"]: step.get("result") or {}
            for step in state["steps"]
            if step.get("status") == STEP_COMPLETED and isinstance(step.get("result"), dict)
        }
        payload = {
            "cycle_id": state["id"],
            "status": state["status"],
            "recovery_policy": state["recovery_policy"],
            "failure_policy": state["failure_policy"],
            "current_index": state["current_index"],
            "steps": state["steps"],
            "last_error": state.get("last_error", ""),
            "recoveries": state.get("recoveries", []),
        }
        payload.update(self._legacy_totals(results))
        return payload

    def _legacy_totals(self, results: dict[str, Any]) -> dict[str, Any]:
        intake = results.get(Stage.INTAKE.value, {})
        coag = results.get(Stage.COAG.value, {})
        chlor = results.get(Stage.CHLOR.value, {})
        clearwell = results.get(Stage.CLEARWELL.value, {})
        quota = results.get(Stage.QUOTA.value, {})
        filters = results.get(Stage.FILTER.value, {})
        return {
            "coag_dose": coag.get("coag_dose", 0.0),
            "turb_dose": coag.get("turb_dose", 0.0),
            "chlor_dose": chlor.get("chlor_dose", 0.0),
            "min_level": clearwell.get("min_level", 0.0),
            "level": clearwell.get("level", 0.0),
            "quota": quota.get("quota", 0.0),
            "rotation": filters.get("rotation", []),
            "on_duty": filters.get("on_duty", ""),
            "backwash": filters.get("backwash", ""),
            "drains": self._rt.backwash.drain_count(),
            "audit_count": self._auditor.count(),
            "ph_stable": chlor.get("ph_stable", False),
            "ph_adjustment": chlor.get("ph_adjustment", 0.0),
            "flow": intake.get("flow", 0.0),
        }

    def _execute_step(self, state: dict[str, Any], stage: Stage) -> dict[str, Any]:
        if stage is Stage.INTAKE:
            return self._intake(state)
        if stage is Stage.COAG:
            return self._coag(state)
        if stage is Stage.CHLOR:
            return self._chlor(state)
        if stage is Stage.FILTER:
            return self._filter(state)
        if stage is Stage.CLEARWELL:
            return self._clearwell(state)
        if stage is Stage.QUOTA:
            return self._quota(state)
        if stage is Stage.AUDIT:
            return {"audit_count": self._auditor.count()}
        raise RuntimeError(f"unsupported stage {stage.value}")

    def _input(self, state: dict[str, Any], name: str, default: object = 0.0) -> Any:
        return state["input"].get(name, default)

    def _step_key(self, state: dict[str, Any], stage: Stage, name: str = "") -> str:
        suffix = f":{name}" if name else ""
        return f"cycle:{state['id']}:step:{stage.value}{suffix}"

    def _intake(self, state: dict[str, Any]) -> dict[str, Any]:
        flow = float(self._input(state, "flow"))
        samples = list(self._input(state, "samples", []))
        mixed = sum(float(item) for item in samples) / len(samples) if samples else 0.0
        marker_key = f"cycle:marker:{state['id']}:intake"
        if self._store.get(marker_key)[1]:
            return {"flow": flow, "turbidity": mixed, "samples": [float(item) for item in samples], "applied": False}
        readings = [float(item) for item in self._rt.trend.values()]
        readings.append(flow)
        readings = readings[-self._rt.trend.window() :]
        with self._store.transaction() as store:
            store.set_locked(FLOW_KEY, f"{flow:.4f}")
            store.set_locked(TURBIDITY_KEY, f"{mixed:.4f}")
            store.set_locked(FLOW_TREND_KEY, json.dumps(readings))
            store.set_locked(marker_key, "done")
        return {"flow": flow, "turbidity": mixed, "samples": [float(item) for item in samples], "applied": True}

    def _coag(self, state: dict[str, Any]) -> dict[str, Any]:
        flow = float(self._input(state, "flow"))
        samples = list(self._input(state, "samples", []))
        coag_dose, applied = self._rt.coag_doser.update_flow_and_dose_idempotent(
            flow, self._step_key(state, Stage.COAG, "dose")
        )
        coag_dose = float(f"{coag_dose:.4f}")
        turb_dose = float(self._rt.sampler.judge(samples))
        return {"coag_dose": coag_dose, "turb_dose": turb_dose, "applied": applied}

    def _chlor(self, state: dict[str, Any]) -> dict[str, Any]:
        verdict = self._rt.stabilizer.stabilize()
        if not verdict.stable:
            raise _Blocked(
                "pH is outside the stable band; chlorine is held until pH is corrected and recovery is confirmed",
                result={
                    "ph_stable": False,
                    "ph_adjustment": verdict.adjustment,
                    "ph_direction": verdict.direction,
                    "chlor_dose": 0.0,
                },
            )
        demand = float(self._input(state, "demand"))
        target = float(f"{0.3 + demand * 0.7:.4f}")
        dose, applied = self._rt.chlor_doser.apply_residual_idempotent(
            target, self._step_key(state, Stage.CHLOR, "dose")
        )
        return {
            "ph_stable": True,
            "ph_adjustment": 0.0,
            "ph_direction": verdict.direction,
            "target": target,
            "chlor_dose": dose,
            "applied": applied,
        }

    def _filter(self, state: dict[str, Any]) -> dict[str, Any]:
        rotation = self._rt.backwash.order_rotation()
        duty = self._rt.bank.on_duty()
        bed_id = str(self._input(state, "bed_id", ""))
        marker_key = self._step_key(state, Stage.FILTER, "backwash") if bed_id else ""
        backwash_started = bool(
            bed_id and not self._store.get(f"{MARKER_KEY_PREFIX}{marker_key}")[1]
        )
        with self._store.transaction() as store:
            if bed_id:
                self._rt.backwash.prepare_idempotent(bed_id, marker_key, store)
            store.set_locked(
                BANK_KEY,
                json.dumps([bed.snapshot() for bed in self._rt.bank.beds()], ensure_ascii=False),
            )
        return {
            "rotation": rotation,
            "on_duty": duty or "",
            "backwash": bed_id,
            "backwash_started": backwash_started,
        }

    def _clearwell(self, state: dict[str, Any]) -> dict[str, Any]:
        level = float(self._input(state, "level"))
        marker_key = f"cycle:marker:{state['id']}:clearwell"
        if self._store.get(marker_key)[1]:
            current = self._rt.well.level()
            return {"min_level": current, "level": current, "target": level, "applied": False}
        minimum = self._rt.well.level()
        final_level = level
        with self._store.transaction() as store:
            store.set_locked(LEVEL_KEY, f"{final_level:.4f}")
            store.set_locked(marker_key, "done")
        return {"min_level": min(minimum, final_level), "level": final_level, "target": level, "applied": True}

    def _quota(self, state: dict[str, Any]) -> dict[str, Any]:
        amount = float(self._input(state, "amount"))
        value, applied = self._rt.accumulator.add_idempotent(
            amount, self._step_key(state, Stage.QUOTA, "meter")
        )
        return {"quota": value, "amount": amount, "applied": applied}

    def history(self) -> list[dict[str, Any]]:
        """Return completed cycles retained for console and audit review."""

        return self._history()

    def _archive(self, state: dict[str, Any]) -> None:
        with self._store.transaction() as store:
            history = self._history()
            history = [item for item in history if item.get("id") != state.get("id")]
            history.append(state)
            store.set_locked(CYCLE_HISTORY_KEY, json.dumps(history, ensure_ascii=False))
            store.set_locked(CYCLE_KEY, json.dumps(state, ensure_ascii=False))

    def _history(self) -> list[dict[str, Any]]:
        raw, present = self._store.get(CYCLE_HISTORY_KEY)
        if not present:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _require(self) -> dict[str, Any]:
        state = self.get()
        if state is None:
            raise CycleError("no active treatment cycle")
        return state

    def _event(self, state: dict[str, Any], kind: str, detail: str) -> dict[str, str]:
        return {"id": uuid.uuid4().hex, "time": _now(), "key": uuid.uuid4().hex, "kind": kind, "detail": detail}

    def _save(self, state: dict[str, Any], events: list[dict[str, str]]) -> None:
        state["updated_at"] = _now()
        with self._store.transaction() as store:
            if events:
                from waterplant.audit.auditor import AUDIT_KEY

                raw = store.raw_locked(AUDIT_KEY, "[]")
                try:
                    items = json.loads(raw)
                except ValueError:
                    items = []
                if not isinstance(items, list):
                    items = []
                items.extend(events)
                store.set_locked(AUDIT_KEY, json.dumps(items, ensure_ascii=False))
            store.set_locked(CYCLE_KEY, json.dumps(state, ensure_ascii=False))

    def _jsonable(self, value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class _Blocked(Exception):
    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result
