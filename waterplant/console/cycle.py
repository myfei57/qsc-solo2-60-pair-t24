"""HTTP handlers for the durable treatment control cycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.cycle import CycleConflict, CycleInputs, cycle_detail, legacy_response

from .http import Request, Response, error_response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _status_for(doc) -> int:
    # 200 when the cycle ran to completion; 409 when it stopped at a safe
    # point and needs an explicit operator resume or abort.
    return 200 if doc.status == "completed" else 409


def cycle(server: "Server", request: Request) -> Response:
    try:
        inputs = CycleInputs.from_payload(request.payload)
        doc = server.runtime.cycles.start(inputs)
    except CycleConflict as exc:
        return error_response(409, str(exc))
    return json_response(
        legacy_response(doc, server.runtime.auditor.count()), status=_status_for(doc)
    )


def cycle_resume(server: "Server", request: Request) -> Response:
    try:
        doc = server.runtime.cycles.resume(operator=request.str_field("operator", ""))
    except CycleConflict as exc:
        return error_response(409, str(exc))
    return json_response(
        legacy_response(doc, server.runtime.auditor.count()), status=_status_for(doc)
    )


def cycle_abort(server: "Server", request: Request) -> Response:
    try:
        doc = server.runtime.cycles.abort(operator=request.str_field("operator", ""))
    except CycleConflict as exc:
        return error_response(409, str(exc))
    return json_response(cycle_detail(doc))


def cycle_status(server: "Server", request: Request) -> Response:
    doc = server.runtime.cycles.active()
    if doc is None:
        return json_response({"active": False})
    return json_response({"active": True, "cycle": cycle_detail(doc)})


def cycle_events(server: "Server", request: Request) -> Response:
    cycle_id = request.str_field("cycle_id", "")
    events = server.runtime.cycles.events(cycle_id or None)
    return json_response({"events": [event.as_dict() for event in events]})


def cycle_history(server: "Server", request: Request) -> Response:
    docs = server.runtime.cycles.history()
    return json_response({"cycles": [cycle_detail(doc) for doc in docs]})
