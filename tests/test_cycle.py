"""Tests for the durable, resumable treatment cycle."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.cycle.model import CycleStatus, StepStatus
from waterplant.cycle.persist import ACTIVE_CYCLE_KEY
from waterplant.store import Store

CYCLE_BODY = {
    "flow": 800.0,
    "samples": [2.0, 4.0],
    "demand": 0.5,
    "level": 10.0,
    "bed_id": "b1",
    "zone": 1,
    "amount": 12.0,
}


class CycleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        return response.status, json.loads(response.body.decode("utf-8"))

    def test_cycle_runs_to_completion_and_records_each_step(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        status, body = self.call("POST", "/cycle", CYCLE_BODY)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["coag_dose"], 800.0)
        self.assertEqual(body["chlor_dose"], 0.65)
        self.assertEqual(body["quota"], 12.0)
        self.assertEqual(body["drains"], 1)
        # chemical dosing audit, not the cycle journal
        self.assertEqual(body["audit_count"], 2)

        _, status_body = self.call("GET", "/cycle/status")
        self.assertFalse(status_body["active"])
        steps = {step["name"]: step for step in self._history_steps(body["cycle_id"])}
        self.assertTrue(all(step["status"] == "succeeded" for step in steps.values()))

    def test_ph_gate_blocks_chlorine_and_keeps_completed_steps(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        self.call("POST", "/ph/read", {"value": 5.0})
        status, body = self.call("POST", "/cycle", CYCLE_BODY)
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "interrupted")
        self.assertEqual(body["breakpoint"], "chlor")
        self.assertEqual(body["interrupt_reason"], "ph_not_stable")
        self.assertEqual(body["chlor_dose"], 0.0)
        self.assertIn("coag", body["completed_steps"])
        self.assertNotIn("chlor", body["completed_steps"])
        # later steps never ran
        self.assertEqual(body["quota"], 0.0)

        # cannot start a second cycle while one is parked
        status, overlap = self.call("POST", "/cycle", CYCLE_BODY)
        self.assertEqual(status, 409)
        self.assertIn("resume or abort", overlap["error"])

        # resume while still unstable must stay parked and not dose chlorine
        status, still = self.call("POST", "/cycle/resume", {"operator": "alice"})
        self.assertEqual(status, 409)
        self.assertEqual(still["breakpoint"], "chlor")
        self.assertEqual(still["chlor_dose"], 0.0)
        self.assertEqual(still["resumed_count"], 1)

        # stabilise pH, then resume continues from the breakpoint
        self.call("POST", "/ph/read", {"value": 7.0})
        status, done = self.call("POST", "/cycle/resume", {"operator": "alice"})
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["chlor_dose"], 0.65)
        self.assertEqual(done["quota"], 12.0)

        _, totals = self.call("GET", "/audit/totals")
        self.assertEqual(totals["totals"], {"coagulant": 800.0, "chlorine": 0.65})

    def test_abort_retains_completed_work(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        self.call("POST", "/ph/read", {"value": 5.0})
        _, body = self.call("POST", "/cycle", CYCLE_BODY)
        status, aborted = self.call("POST", "/cycle/abort", {"operator": "dave"})
        self.assertEqual(status, 200)
        self.assertEqual(aborted["status"], "aborted")
        succeeded = [s["name"] for s in aborted["steps"] if s["status"] == "succeeded"]
        self.assertIn("coag", succeeded)
        self.assertNotIn("chlor", succeeded)
        _, totals = self.call("GET", "/audit/totals")
        self.assertEqual(totals["totals"], {"coagulant": 800.0})
        _, status_body = self.call("GET", "/cycle/status")
        self.assertFalse(status_body["active"])
        status, error = self.call("POST", "/cycle/resume", {})
        self.assertEqual(status, 409)

    def test_step_failure_parks_later_steps(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        original = self.server.runtime.backwash.start

        def fail_once(bed_id: str) -> None:
            if bed_id == "b1":
                raise RuntimeError("valve stuck")
            original(bed_id)

        self.server.runtime.backwash.start = fail_once
        status, body = self.call("POST", "/cycle", CYCLE_BODY)
        self.assertEqual(status, 409)
        self.assertEqual(body["breakpoint"], "backwash")
        self.assertEqual(body["interrupt_reason"], "step_failed")
        self.assertIn("valve stuck", body["interrupt_detail"])
        # chlorine already dosed, quota (later) not yet
        self.assertEqual(body["chlor_dose"], 0.65)
        self.assertEqual(body["quota"], 0.0)

        self.server.runtime.backwash.start = original
        status, done = self.call("POST", "/cycle/resume", {"operator": "bob"})
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["quota"], 12.0)
        # exactly one of each chemical despite the retry
        _, totals = self.call("GET", "/audit/totals")
        self.assertEqual(totals["totals"], {"coagulant": 800.0, "chlorine": 0.65})

    def test_crash_after_dose_does_not_double_dose_on_restart(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        runtime = self.server.runtime

        # chlorine physically applied and audited, then power loss before commit
        original_apply = runtime.chlor_doser.apply_once

        def crash_after_dose(ref: str):
            dose, applied = original_apply(ref)
            raise RuntimeError("power loss after dose")

        runtime.chlor_doser.apply_once = crash_after_dose
        _, body = self.call("POST", "/cycle", CYCLE_BODY)
        self.assertEqual(body["breakpoint"], "chlor")
        raw = json.loads(self.store.get(ACTIVE_CYCLE_KEY)[0])
        for step in raw["steps"]:
            if step["name"] == "chlor":
                step["status"] = StepStatus.RUNNING.value
                step["error"] = ""
        raw["status"] = CycleStatus.RUNNING.value
        raw["breakpoint"] = ""
        raw["interrupt_reason"] = ""
        raw["interrupt_detail"] = ""
        self.store.put(ACTIVE_CYCLE_KEY, json.dumps(raw))
        runtime.chlor_doser.apply_once = original_apply

        # process restart: new server over the same store
        restarted = Server(self.store)
        status, active = self._get(restarted, "/cycle/status")
        self.assertEqual(active["cycle"]["status"], "interrupted")
        self.assertEqual(active["cycle"]["breakpoint"], "chlor")
        self.assertEqual(active["cycle"]["interrupt_reason"], "process_interrupted")

        status, done = self._post(restarted, "/cycle/resume", {"operator": "carol"})
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "completed")
        totals = restarted.runtime.auditor.totals()
        self.assertEqual(totals, {"coagulant": 800.0, "chlorine": 0.65})

    def test_invalid_inputs_are_rejected_before_any_step(self) -> None:
        status, body = self.call("POST", "/cycle", {**CYCLE_BODY, "bed_id": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])
        _, active = self.call("GET", "/cycle/status")
        self.assertFalse(active["active"])

        status, body = self.call("POST", "/cycle", {**CYCLE_BODY, "flow": -1})
        self.assertEqual(status, 400)

    def test_events_record_step_results_interrupt_and_recovery(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        self.call("POST", "/ph/read", {"value": 5.0})
        _, body = self.call("POST", "/cycle", CYCLE_BODY)
        self.call("POST", "/ph/read", {"value": 7.0})
        self.call("POST", "/cycle/resume", {"operator": "alice"})
        _, events = self.call("GET", "/cycle/events", {"cycle_id": body["cycle_id"]})
        kinds = [event["kind"] for event in events["events"]]
        self.assertIn("cycle.started", kinds)
        self.assertIn("cycle.blocked", kinds)
        self.assertIn("cycle.resumed", kinds)
        self.assertIn("cycle.completed", kinds)
        blocked = [event for event in events["events"] if event["kind"] == "cycle.blocked"][0]
        self.assertEqual(blocked["step"], "chlor")
        resumed = [event for event in events["events"] if event["kind"] == "cycle.resumed"][0]
        self.assertEqual(resumed["operator"], "alice")

    def test_filter_bank_survives_restart(self) -> None:
        self.call("POST", "/filter/add", {"id": "b1", "zone": 1, "load": 3.0})
        restarted = Server(self.store)
        self.assertEqual(restarted.runtime.bank.bed_ids(), ["b1"])
        self.assertEqual(restarted.runtime.bank.bed("b1").zone, 1)

    def _history_steps(self, cycle_id: str) -> list[dict]:
        _, history = self.call("GET", "/cycle/history")
        for cycle in history["cycles"]:
            if cycle["cycle_id"] == cycle_id:
                return cycle["steps"]
        self.fail("cycle not in history")

    @staticmethod
    def _get(server: Server, path: str):
        response = server.dispatch("GET", path, None)
        return response.status, json.loads(response.body.decode("utf-8"))

    @staticmethod
    def _post(server: Server, path: str, payload: dict):
        response = server.dispatch("POST", path, payload)
        return response.status, json.loads(response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
