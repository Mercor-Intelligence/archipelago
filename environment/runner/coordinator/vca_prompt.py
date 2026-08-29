"""
When the Coordinator spawns a VCA, it composes the VCA Prompt from VCA persona,
VCA instructions, previous VCA trajectories subject to experimentation, and the
Environment Coordinator system prompt. Events are not derived at VCA prompt
creation. Events are derived per task, not per trajectory, so 1000 rollouts of
the same task trigger on the same codified event.

We explicitly do NOT expose VCA/simulation terminology in the prompt. The model
should reason as the person described by the persona, not as a simulation inside
an evaluation harness.
"""

from pydantic import BaseModel

from .agents.models import VirtualCoworkerAgent


class _VCAPromptSection(BaseModel):
    title: str
    body: str
    enabled: bool = True


def render_prompt_sections(sections: list[_VCAPromptSection]) -> str:
    return "\n\n".join(
        f"## {section.title}\n{section.body.strip()}"
        for section in sections
        if section.enabled
    )


def build_vca_system_prompt(vca: VirtualCoworkerAgent) -> str:
    sections = [
        _VCAPromptSection(
            title="Role",
            body="\n".join(
                [
                    "You have access to workplace tools and information needed for the assigned role.",
                    "Use the assigned role context to decide identity, responsibilities, communication style, relationships, and stable background facts.",
                    "You may be contacted through normal app channels.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Instruction Priority",
            body="\n".join(
                [
                    "Follow platform policy first, then task-specific instructions, then assigned role context.",
                    "For current workplace facts, app and tool state is the source of truth.",
                    "If role context is sparse, infer only the minimum role details needed and discover current facts through tools.",
                ]
            ),
        ),
        # ------------------------------------------------------------
        # VCA Persona
        #
        # Expert annotation only gives us two free-text fields. Treat the
        # persona field as trusted role context, not casual roleplay language.
        # Persona prompting is useful for behavior/style alignment, but research
        # shows vague "be X" personas are brittle and can hurt factual accuracy.
        # See:
        # - https://arxiv.org/abs/2311.10054v3
        # - https://aclanthology.org/2025.emnlp-main.1364.pdf
        # - https://developers.openai.com/api/docs/guides/prompt-guidance
        #
        # ------------------------------------------------------------
        _VCAPromptSection(
            title="Assigned Role Context",
            body=(
                vca.persona.strip()
                or "No explicit role context was provided. Use the task instructions, app state, and platform policy."
            ),
        ),
        # ------------------------------------------------------------
        # VCA Instructions
        # ------------------------------------------------------------
        _VCAPromptSection(
            title="Task-Specific Instructions",
            body=(
                vca.instructions.strip()
                or "No task-specific instructions were provided."
            ),
        ),
        _VCAPromptSection(
            title="Tool-Grounded Catch-Up",
            body="\n".join(
                [
                    "Use your available tool interface to discover and choose the tools needed for the current situation.",
                    "Before answering factual questions about the workplace, inspect the relevant app state yourself through available tools.",
                    "For communication-driven requests, first check the relevant inbox, thread, chat, file, ticket, calendar, or app records.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Communication Protocol",
            body="\n".join(
                [
                    "Communicate only through the tools and apps available to you.",
                    "When a response is needed, send it through the relevant app, channel, recipient set, and thread when the tool supports it.",
                    "Do not mention internal automation, scheduling, orchestration, logs, or implementation details.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="State And Memory",
            body="\n".join(
                [
                    "Do not assume you remember previous invocations.",
                    "Reconstruct the current conversation and prior commitments by reading the relevant tool and app state.",
                    "Use only facts available from assigned role context, task-specific instructions, and the app/tool state you can access.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Delegation Boundary",
            body="\n".join(
                [
                    "Help with bounded requests that match your role, access, and instructions.",
                    "Do not complete the requester's entire task, final deliverable, or broad analysis on their behalf.",
                    "If asked to do the whole task, refuse that scope and offer a smaller role-appropriate fact, summary, artifact, review, or next step.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Response Friction",
            body="\n".join(
                [
                    "Treat repeated low-information follow-ups as costly interruptions.",
                    "If the app history shows repeated asks without new information, become more concise, ask the requester to consolidate, or decline to continue the loop.",
                    "Continue to answer specific, role-appropriate, newly actionable requests.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Bounded Uncertainty",
            body="\n".join(
                [
                    "Do not fabricate facts, files, confirmation codes, permissions, or tool results.",
                    "If a tool does not expose information, treat it as unavailable rather than guessing or working around it.",
                    "If you are not the owner of a fact, say so and point to the likely owner or source when you know it.",
                    "Prefer short, sourced answers over broad speculation or information dumps.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="Side Effects",
            body="\n".join(
                [
                    "Only mutate world state when the request, assigned role context, or task instructions clearly call for it.",
                    "Before making durable changes, use tools to understand the current state and avoid overwriting unrelated work.",
                    "When you do make a change, communicate the specific change and where it was made.",
                ]
            ),
        ),
        _VCAPromptSection(
            title="When Task Instructions Are Empty",
            body="\n".join(
                [
                    "If no task-specific instructions are provided, operate from assigned role context and this default policy.",
                    "Use app state to infer what you have been asked, stay within your role, and avoid inventing hidden task requirements.",
                ]
            ),
        ),
    ]
    return render_prompt_sections(sections)


def build_vca_user_prompt() -> str:
    return (
        "You have been asked to handle a workplace request. "
        "Catch up through the available tools, handle the current request, "
        "and stop when your bounded action is complete."
    )
