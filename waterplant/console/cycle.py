"""One full, durable treatment control cycle."""

from __future__ import annotations

from waterplant.chlor import validate_demand
from waterplant.clearwell import validate_level
from waterplant.cycle import CycleError, CycleManager, STATUS_COMPLETED
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import validate_flow
from waterplant.quota import validate_amount

from .http import Request, RequestError, Response, json_response
from .runtime import Runtime


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Start a cycle whose completed steps are durably recorded."""

    validate_flow(request.float_field("flow"))
    request.float_list("samples")
    validate_demand(request.float_field("demand"))
    validate_factor(rt.calibration.current())
    validate_zone(request.int_field("zone"))
    validate_amount(request.float_field("amount"))
    validate_level(request.float_field("level"))

    manager = CycleManager(rt)
    try:
        state = manager.start(dict(request.payload))
    except CycleError as exc:
        raise RequestError(409, str(exc)) from exc
    status = 200 if state["status"] == STATUS_COMPLETED else 409
    return json_response(manager.response_payload(state), status=status)


def get_cycle(rt: Runtime) -> Response:
    """Return the current cycle checkpoint, including every step result."""

    manager = CycleManager(rt)
    state = manager.get()
    if state is None:
        return json_response({"present": False}, status=404)
    return json_response(manager.response_payload(state))


def get_cycle_history(rt: Runtime) -> Response:
    """List retained completed cycles; partial cycles are never discarded."""

    manager = CycleManager(rt)
    return json_response({"cycles": [manager.response_payload(state) for state in manager.history()]})


def recover_cycle(rt: Runtime, request: Request) -> Response:
    """Resume an interrupted cycle after explicit duty-operator confirmation."""

    manager = CycleManager(rt)
    try:
        state = manager.recover(dict(request.payload))
    except CycleError as exc:
        raise RequestError(409, str(exc)) from exc
    status = 200 if state["status"] == STATUS_COMPLETED else 409
    return json_response(manager.response_payload(state), status=status)
