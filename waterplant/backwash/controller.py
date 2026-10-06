"""Backwash controller.

Every backwash follows the same sequence: the bed is closed first, then the
drain is opened for it. The command queue is persisted so an interrupted
console can replay or discard pending work.
"""

from __future__ import annotations

import json

from waterplant.filter.bank import Bank
from waterplant.filter.report import BANK_KEY
from waterplant.store.commands import append_command, clear_commands, load_commands
from waterplant.store.store import Store

from .report import BackwashState
from .schedule import now_unix

COMMAND_KEY = "backwash:commands"
DRAIN_KEY_PREFIX = "backwash:drain:"
MARKER_KEY_PREFIX = "backwash:cycle-marker:"
SPILL_KEY = "backwash:spill"


class Controller:
    """Sequences backwash commands against the filter bank."""

    def __init__(self, bank: Bank, store: Store) -> None:
        self._bank = bank
        self._store = store

    def start(self, bed_id: str) -> None:
        """Close the bed and only then open its drain, in one persisted batch."""

        self.start_idempotent(bed_id, f"standalone:{bed_id}")

    def start_idempotent(self, bed_id: str, key: str) -> bool:
        """Close and drain a bed once for the supplied cycle step key."""

        marker_key = f"{MARKER_KEY_PREFIX}{key}"
        if self._store.get(marker_key)[1]:
            self._bank.close(bed_id)
            return False
        with self._store.transaction() as store:
            return self.prepare_idempotent(bed_id, key, store)

    def prepare_idempotent(
        self, bed_id: str, key: str, store: Store, marker_key: str | None = None
    ) -> bool:
        """Prepare the bed and writes inside the caller's held store transaction."""

        marker_key = marker_key or f"{MARKER_KEY_PREFIX}{key}"
        if store.has_locked(marker_key):
            self._bank.close(bed_id)
            return False
        self._bank.close(bed_id)
        drain_key = f"{DRAIN_KEY_PREFIX}{bed_id}"
        if not self._bank.is_closed(bed_id):
            store.set_locked(SPILL_KEY, "true")
        store.set_locked(drain_key, str(now_unix()))
        store.set_locked(marker_key, bed_id)
        store.set_locked(
            BANK_KEY,
            json.dumps([bed.snapshot() for bed in self._bank.beds()], ensure_ascii=False),
        )
        return True

    def _drain(self, bed_id: str) -> None:
        if not self._bank.is_closed(bed_id):
            self._store.put(SPILL_KEY, "true")
        self._store.put(f"{DRAIN_KEY_PREFIX}{bed_id}", str(now_unix()))

    def enqueue(self, bed_id: str) -> None:
        append_command(self._store, COMMAND_KEY, bed_id)

    def replay(self) -> list[str]:
        """Re-run every queued command in the order it was written."""

        commands = self.command_list()
        for bed_id in commands:
            self.start(bed_id)
        return commands

    def recover(self) -> None:
        """Discard commands left over from a filter that already recovered."""

        clear_commands(self._store, COMMAND_KEY)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    def select(self, zone: int) -> str:
        """Resolve the bed that currently occupies a zone."""

        mapping = self._bank.mapping()
        if zone not in mapping:
            raise ValueError(f"no filter bed for zone {zone}")
        return mapping[zone]

    def order_rotation(self) -> list[str]:
        """Rotate duty to the dirtiest bed first."""

        order = [bed.id for bed in sorted(self._bank.beds(), key=lambda bed: -bed.load)]
        if order:
            self._bank.rotate(order[0])
        return order

    def eligible(self) -> list[str]:
        return [bed.id for bed in self._bank.beds() if not bed.closed]

    def drains(self) -> list[str]:
        drains: list[str] = []
        for key in self._store.keys():
            if key.startswith(DRAIN_KEY_PREFIX):
                drains.append(key[len(DRAIN_KEY_PREFIX) :])
        return drains

    def drain_count(self) -> int:
        return len(self.drains())

    def last_drain(self) -> str | None:
        drains = self.drains()
        if not drains:
            return None
        return drains[-1]

    def spilled(self) -> bool:
        return self._store.get(SPILL_KEY)[1]

    def state(self) -> BackwashState:
        return BackwashState(commands=self.command_list(), drains=self.drains())

    def describe(self) -> str:
        state = self.state()
        return f"backwash commands={len(state.commands)} drains={len(state.drains)}"
