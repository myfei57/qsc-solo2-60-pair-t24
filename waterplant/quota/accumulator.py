"""Chlorine consumption accumulator with a periodic wrap."""

from __future__ import annotations

from waterplant.store.epoch import advance_epoch, load_float, save_float
from waterplant.store.store import Store

from .report import AccumState

ACCUM_KEY = "quota:chlorine-accumulator"
EPOCH_CAP = 100.0


class Accumulator:
    """Runs a wrapping total so the meter never overflows."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def add(self, amount: float) -> float:
        return advance_epoch(self._store, ACCUM_KEY, amount, EPOCH_CAP)

    def add_idempotent(self, amount: float, key: str) -> tuple[float, bool]:
        """Add once for ``key``; replay returns the previously metered value."""

        marker_key = f"quota:marker:{key}"
        with self._store.transaction() as store:
            raw, present = store.get_locked(marker_key)
            if present:
                return float(raw), False
            current = float(store.raw_locked(ACCUM_KEY, "0.0"))
            next_value = current + amount
            if EPOCH_CAP > 0 and next_value >= EPOCH_CAP:
                next_value -= EPOCH_CAP
            store.set_locked(ACCUM_KEY, f"{next_value:.4f}")
            store.set_locked(marker_key, f"{next_value:.4f}")
        return float(f"{next_value:.4f}"), True

    def value(self) -> float:
        value, _ = load_float(self._store, ACCUM_KEY)
        return value

    def remaining(self, limit: float) -> float:
        remaining = limit - self.value()
        return remaining if remaining > 0 else 0.0

    def reset(self) -> None:
        save_float(self._store, ACCUM_KEY, 0.0)

    def state(self) -> AccumState:
        return AccumState(value=self.value())

    def describe(self) -> str:
        return f"quota accumulator value={self.value():.4f}"
