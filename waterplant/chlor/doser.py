"""Chlorine dosing controller."""

from __future__ import annotations

from waterplant.audit import Auditor
from waterplant.clearwell import Well
from waterplant.store.store import Store

from .report import ChlorState

CHLORINE = "chlorine"


class Doser:
    """Follows the residual target held by the clear well."""

    def __init__(self, store: Store) -> None:
        self._well = Well(store)
        self._auditor = Auditor(store)

    def current_target(self) -> float:
        """Residual target read fresh from the clear well on every call."""

        return self._well.residual_target()

    def dose_for_residual(self) -> float:
        return self.current_target()

    def apply_residual(self) -> float:
        """Record a chlorine dose and return the amount applied."""

        dose = self.dose_for_residual()
        self._auditor.record(CHLORINE, f"{dose:.4f}")
        return dose

    def apply_once(self, ref: str) -> tuple[float, bool]:
        """Apply the residual dose once per ``ref``.

        A repeated or resumed call returns the already applied dose instead
        of dosing a second time.
        """

        dose = self.dose_for_residual()
        entry, applied = self._auditor.record_once(CHLORINE, f"{dose:.4f}", ref)
        try:
            return float(entry.detail), applied
        except ValueError:
            return dose, applied

    def target_for_demand(self, demand: float) -> float:
        return 0.3 + demand * 0.7

    def state(self) -> ChlorState:
        return ChlorState(target=self.current_target(), dose=self.dose_for_residual())

    def describe(self) -> str:
        state = self.state()
        return f"chlorine target={state.target:.4f} dose={state.dose:.4f}"
