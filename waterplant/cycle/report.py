"""Console projections over a cycle document and its journal."""

from __future__ import annotations

from .model import StepStatus
from .persist import CycleDoc


def step_map(doc: CycleDoc) -> dict[str, dict[str, object]]:
    return {record.name: record.as_dict() for record in doc.steps}


def legacy_response(doc: CycleDoc, chemical_audit_count: int) -> dict[str, object]:
    """Project a cycle into the fields the original /cycle returned."""

    steps = step_map(doc)

    def result_value(name: str, key: str, default: object) -> object:
        record = steps.get(name)
        if record is None:
            return default
        result = record.get("result")
        return result.get(key, default) if isinstance(result, dict) else default

    ph_result = steps.get("ph", {}).get("result", {})
    if not isinstance(ph_result, dict):
        ph_result = {}
    return {
        "cycle_id": doc.cycle_id,
        "status": doc.status,
        "resumable": doc.status == "interrupted",
        "breakpoint": doc.breakpoint,
        "interrupt_reason": doc.interrupt_reason,
        "interrupt_detail": doc.interrupt_detail,
        "coag_dose": result_value("coag", "dose", 0.0),
        "turb_dose": result_value("turbidity", "dose", 0.0),
        "chlor_dose": result_value("chlor", "dose", 0.0),
        "min_level": result_value("clearwell", "min_level", 0.0),
        "level": result_value("clearwell", "level", 0.0),
        "quota": result_value("quota", "quota", 0.0),
        "rotation": result_value("rotation", "rotation", []),
        "on_duty": result_value("rotation", "on_duty", ""),
        "backwash": result_value("backwash", "backwash", ""),
        "drains": result_value("backwash", "drains", 0),
        "audit_count": chemical_audit_count,
        "ph_stable": ph_result.get("stable", False),
        "ph_adjustment": ph_result.get("adjustment", 0.0),
        "completed_steps": [
            record.name
            for record in doc.steps
            if record.status == StepStatus.SUCCEEDED.value
        ],
        "resumed_count": doc.resumed_count,
    }


def cycle_detail(doc: CycleDoc) -> dict[str, object]:
    """Full step by step view for the console."""

    return {
        "cycle_id": doc.cycle_id,
        "status": doc.status,
        "resumable": doc.status == "interrupted",
        "breakpoint": doc.breakpoint,
        "interrupt_reason": doc.interrupt_reason,
        "interrupt_detail": doc.interrupt_detail,
        "resumed_count": doc.resumed_count,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "finished_at": doc.finished_at,
        "inputs": doc.inputs,
        "steps": [record.as_dict() for record in doc.steps],
    }
