"""
VCA prompting is intentionally minimal for now and will be fully spec'ed later.

When the Coordinator spawns a VCA, it composes the VCA Prompt from VCA persona,
VCA instructions, previous VCA trajectories subject to experimentation, and the
Environment Coordinator system prompt. Events are not derived at VCA prompt
creation. Events are derived per task, not per trajectory, so 1000 rollouts of
the same task trigger on the same codified event.
"""

from .agents.models import VirtualCoworkerAgent


def build_vca_initial_prompt(vca: VirtualCoworkerAgent) -> str:
    parts = [vca.persona.strip(), vca.instructions.strip()]
    return "\n\n".join(part for part in parts if part)
