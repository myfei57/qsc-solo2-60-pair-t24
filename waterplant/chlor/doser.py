"""Chlorine dosing controller."""

from __future__ import annotations

from waterplant.audit import Auditor
from waterplant.clearwell import RESIDUAL_KEY, Well
from waterplant.store.store import Store

from .report import ChlorState

CHLORINE = "chlorine"


class Doser:
    """Follows the residual target held by the clear well."""

    def __init__(self, store: Store) -> None:
        self._store = store
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

    def apply_residual_idempotent(self, target: float, key: str) -> tuple[float, bool]:
        """Apply and audit the target once for the supplied step key."""

        existing = self._auditor.entry_for_key(key)
        if existing is not None:
            return float(existing.detail), False
        dose = target
        entry = self._auditor.make_entry(CHLORINE, f"{dose:.4f}", key)
        self._auditor.append_entries_atomically([entry], {RESIDUAL_KEY: f"{target:.4f}"})
        return float(f"{dose:.4f}"), True

    def target_for_demand(self, demand: float) -> float:
        return 0.3 + demand * 0.7

    def state(self) -> ChlorState:
        return ChlorState(target=self.current_target(), dose=self.dose_for_residual())

    def describe(self) -> str:
        state = self.state()
        return f"chlorine target={state.target:.4f} dose={state.dose:.4f}"
