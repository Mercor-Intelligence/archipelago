"""Reference implementation of the agent contract.

echo_agent is the simplest possible agent that satisfies every part
of the registered-agent contract: it implements the run() signature,
emits the final_answer log on completion, and returns a valid
AgentTrajectoryOutput. It does not call any LLM and does not connect
to MCP, so it runs in O(1) wall time and is suitable as a smoke-test
target and a copy-paste starting point for new agent contributors.
"""

import time

from loguru import logger

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
)


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    start = time.time()

    last_user_message = ""
    for msg in reversed(run_input.initial_messages):
        if msg.get("role") == "user":
            last_user_message = msg.get("content", "")
            break

    answer = f"echo: {last_user_message}"

    logger.bind(message_type="final_answer").info(answer)

    messages = list(run_input.initial_messages) + [
        {"role": "assistant", "content": answer}
    ]

    return AgentTrajectoryOutput(
        messages=messages,
        status=AgentStatus.COMPLETED,
        time_elapsed=time.time() - start,
    )
