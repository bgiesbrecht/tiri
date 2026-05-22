"""Collector — records thumbs-up/down on a persisted ConversationTurn."""

from __future__ import annotations

from tiri.providers.base import StoreProvider, StoreProviderError


_VALID_SIGNALS = frozenset({"up", "down"})


class Collector:
    def __init__(self, store: StoreProvider) -> None:
        self._store = store

    async def record(
        self,
        conversation_id: str,
        turn_id: str,
        signal: str,
        comment: str = "",
    ) -> None:
        """Attach a feedback signal to an existing turn.

        Updates `turn.feedback_signal` on the persisted ConversationTurn and
        also writes a `feedback:{conv}:{turn}` row so the Proposer can scan
        without rehydrating every turn.

        Raises `StoreProviderError` if the turn does not exist.
        """
        if signal not in _VALID_SIGNALS:
            raise ValueError(
                f"signal must be one of {sorted(_VALID_SIGNALS)}; got {signal!r}"
            )
        key = f"conv:{conversation_id}:turn:{turn_id}"
        turn = await self._store.get(key)
        if turn is None:
            raise StoreProviderError(
                f"Cannot record feedback: turn {turn_id} not found in "
                f"conversation {conversation_id}"
            )
        turn["feedback_signal"] = signal
        await self._store.put(key, turn)
        await self._store.put(
            f"feedback:{conversation_id}:{turn_id}",
            {"signal": signal, "comment": comment},
        )
