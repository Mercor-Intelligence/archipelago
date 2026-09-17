"""
Prompt construction for the Stirrup agent
"""

from loguru import logger

BASE_SYSTEM_PROMPT = (
    "You are an AI agent that will be given a specific task. "
    "You are to complete that task using the tools provided in {max_turns} steps. "
    "You will need to call the finish tool as your last step. IMPORTANT: The 'reason' "
    "parameter you pass to the finish tool IS your final answer - put your complete "
    "response, summary, or explanation directly in the 'reason' parameter. Any text "
    "you write outside the finish tool call will be ignored."
)

NO_USER_INTERACTION_GUIDANCE = (
    " You are not able to interact with the user during the task."
)

# Appended when the abandon_task tool is enabled, so the model is actually told
# the tool exists. Withheld otherwise: advertising a tool that is not in the
# tool list invites a call that the harness would answer with "Unknown tool".
ABANDON_TASK_GUIDANCE = (
    " If you conclude the task cannot be completed at all - the required data is "
    "unavailable, the request is self-contradictory, or the provided tools cannot "
    "accomplish it - call the abandon_task tool with a brief reason instead of "
    "finish. Do not abandon merely because the task is hard or turns are running "
    "low; in that case keep working and finish with your best effort."
)

# The one substitution the base system prompt supports.
MAX_TURNS_PLACEHOLDER = "{max_turns}"


def render_base_system_prompt(template: str, max_turns: int) -> str:
    """Substitute the step count into a base system prompt.

    NOT `str.format`. Once the base prompt is a free-text TEXTAREA in the agent
    config, every brace an author types would be a format field: a prompt
    containing a JSON example raises `KeyError`, a stray `{` or `}` raises
    `ValueError`, and `{0}` raises `IndexError`. This runs inside
    `_build_initial_messages` before the turn loop starts, so none of those are
    caught — the whole trajectory would die as an unhandled error, and because
    that is retryable it would burn the same budget again. Under a batch that is
    every trajectory in the world. Same reasoning, and same fix, as
    `scripted_turns_agent.render_staged_files_notice`.

    A literal replacement of the one supported token cannot raise, and it lets
    braces mean what an author writing a JSON example intends: themselves.

    A template that omits the token gets the step count APPENDED rather than
    dropped. The model must be told its budget — Stirrup's turn warnings count
    down against it — so a cosmetic omission warns and proceeds instead of
    silently shipping a prompt with no budget in it.
    """
    if MAX_TURNS_PLACEHOLDER in template:
        return template.replace(MAX_TURNS_PLACEHOLDER, str(max_turns))
    logger.warning(
        f"base_system_prompt has no {MAX_TURNS_PLACEHOLDER} placeholder; "
        f"appending the {max_turns}-step budget so the model still knows it"
    )
    stripped = template.strip()
    budget = f"You have {max_turns} steps to complete the task."
    return f"{stripped}\n\n{budget}" if stripped else budget


def build_system_prompt(
    max_turns: int,
    custom_system_prompt: str | None = None,
    input_files: list[str] | None = None,
    base_system_prompt: str | None = None,
    enable_abandon_task: bool = False,
) -> str:
    """
    Build the complete system prompt matching Stirrup's format.

    Args:
        max_turns: Maximum number of turns for the agent
        custom_system_prompt: Optional custom instructions
        input_files: Optional list of input file paths
        base_system_prompt: Optional override for BASE_SYSTEM_PROMPT. Blank or
            unset keeps the built-in default, so an existing world's prompt is
            byte-identical. Supports the {max_turns} token; see
            render_base_system_prompt.
        enable_abandon_task: Whether the abandon_task tool is in the tool list

    Returns:
        Complete system prompt string
    """
    parts: list[str] = []

    # isinstance, not truthiness: ``agent_config_values`` is an unvalidated
    # passthrough dict, so an API- or import-set config can store a number or a
    # list here. ``.strip()`` on one raises inside _build_initial_messages —
    # before the first model turn, where nothing catches it — and the whole
    # trajectory dies as an unhandled (retryable) error. Fall back to the
    # built-in default instead, and say so rather than swallowing it.
    if base_system_prompt is not None and not isinstance(base_system_prompt, str):
        logger.warning(
            f"base_system_prompt is {type(base_system_prompt).__name__}, not a "
            "string; using the built-in default"
        )
        base_system_prompt = None
    template = base_system_prompt.strip() if base_system_prompt else ""
    parts.append(render_base_system_prompt(template or BASE_SYSTEM_PROMPT, max_turns))

    parts.append(NO_USER_INTERACTION_GUIDANCE)

    if enable_abandon_task:
        parts.append(ABANDON_TASK_GUIDANCE)

    if input_files:
        files_section = (
            "\n\nThe following input files have been provided for this task:"
        )
        for file_path in input_files:
            files_section += f"\n- {file_path}"
        parts.append(files_section)

    # 4. Custom system prompt (if provided)
    if custom_system_prompt:
        parts.append(
            f"\n\nFollow these instructions from the User:\n{custom_system_prompt}"
        )

    return "".join(parts)


def build_turn_warning_message(turns_remaining: int) -> str:
    """
    Build the turn warning message matching Stirrup's format.

    Args:
        turns_remaining: Number of turns remaining

    Returns:
        Warning message string
    """
    if turns_remaining == 1:
        return (
            "This is the last turn. Call the finish tool now with your complete "
            "final answer in the 'reason' parameter."
        )
    return (
        f"You have {turns_remaining} turns remaining to complete the task. "
        "Please continue. Remember you will need a separate turn to call the finish "
        "tool with your final answer in the 'reason' parameter."
    )
