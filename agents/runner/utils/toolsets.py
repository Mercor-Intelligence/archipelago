"""Record the tool list each assistant turn was generated under.

A harness whose tools change mid-run (a toolbelt) calls `record` with the exact list
it sent, right before appending the assistant message. The output's `toolsets` is
one full snapshot per change, keyed by the 0-based index of the first assistant
message in the saved transcript it applies to; SFT export splits tapes on it.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from runner.agents.models import LitellmAnyMessage, get_msg_role


class ToolsetRecorder:
    def __init__(self, initial_messages: Sequence[LitellmAnyMessage] = ()) -> None:
        """Count turns after the assistant messages a continued run inherits."""
        self._records: list[dict[str, Any]] = []
        self._last: list[Any] | None = None
        self._assistant_turns: int = sum(
            1 for m in initial_messages if get_msg_role(m) == "assistant"
        )

    def record(self, tools: list[Any]) -> None:
        """Note that the next assistant message was generated with `tools`, in order."""
        if tools != self._last:
            self._last = copy.deepcopy(tools)
            self._records.append(
                {"assistant_turn": self._assistant_turns, "tools": self._last}
            )
        self._assistant_turns += 1

    def output(self) -> dict[str, Any]:
        """`{"toolsets": records}` for the trajectory output, or `{}` if none ran."""
        return {"toolsets": self._records} if self._records else {}
