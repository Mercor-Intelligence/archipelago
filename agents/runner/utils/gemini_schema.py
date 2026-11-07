"""Preserve tagged tool unions through Gemini's OpenAPI schema conversion."""

from copy import deepcopy
from typing import Any

from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam


def preserve_gemini_tool_unions(
    tools: list[ChatCompletionToolParam],
) -> list[ChatCompletionToolParam]:
    """Express disjoint, tagged oneOf branches using Gemini's supported anyOf.

    LiteLLM drops oneOf entirely. Requiring each branch's distinct tag keeps
    the alternatives disjoint and lets callable Pydantic discriminators select
    a branch before its field defaults are applied.
    """
    result = deepcopy(tools)
    for tool in result:
        schema = tool.get("function", {}).get("parameters")
        if isinstance(schema, dict):
            _normalize(schema, schema)
    return result


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(target, dict):
            return node
        target = target.get(part.replace("~1", "/").replace("~0", "~"))
    if not isinstance(target, dict) or "$ref" in target:
        return node
    return {**deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}


def _normalize(
    node: Any, root: dict[str, Any], seen: frozenset[str] = frozenset()
) -> None:
    if isinstance(node, list):
        for item in node:
            _normalize(item, root, seen)
        return
    if not isinstance(node, dict):
        return
    converted = False
    variants = node.get("oneOf")
    if (
        "anyOf" not in node
        and isinstance(variants, list)
        and variants
        and all(isinstance(branch, dict) for branch in variants)
        and not any(branch.get("$ref") in seen for branch in variants)
    ):
        branches = [_resolve(branch, root) for branch in variants]
        discriminator = node.get("discriminator", {})
        field = (
            discriminator.get("propertyName", "type")
            if isinstance(discriminator, dict)
            else "type"
        )
        tags = []
        for branch in branches:
            prop = branch.get("properties", {}).get(field, {})
            tag = prop.get("const", prop.get("default"))
            if tag is None and len(prop.get("enum", [])) == 1:
                tag = prop["enum"][0]
            if not isinstance(tag, str) or not tag or tag in tags:
                break
            tags.append(tag)
        if len(tags) == len(branches):
            for branch, tag in zip(branches, tags, strict=True):
                prop = branch["properties"][field]
                prop.pop("const", None)
                prop.pop("default", None)
                prop.pop("nullable", None)
                prop.update(type="string", enum=[tag])
                if isinstance(prop.get("description"), str):
                    prop["description"] = prop["description"].removeprefix(
                        "(Optional) "
                    )
                required = list(branch.get("required", []))
                if field not in required:
                    required.append(field)
                branch["required"] = required
            node.pop("oneOf")
            node.pop("discriminator", None)
            node["anyOf"] = branches
            converted = True
            for branch, variant in zip(branches, variants, strict=True):
                ref = variant.get("$ref")
                branch_seen = seen | {ref} if isinstance(ref, str) else seen
                _normalize(branch, root, branch_seen)
    for key, value in list(node.items()):
        if key != "anyOf" or not converted:
            _normalize(value, root, seen)


def gemini_action_tool_choice(tools: list[ChatCompletionToolParam]) -> str:
    """Use auto for unions: Gemini's forced-call decoding can emit empty results."""
    return "auto" if _has_union(tools) else "required"


def _has_union(node: Any) -> bool:
    if isinstance(node, list):
        return any(_has_union(item) for item in node)
    if not isinstance(node, dict):
        return False
    for keyword in ("oneOf", "anyOf"):
        variants = node.get(keyword)
        if (
            isinstance(variants, list)
            and sum(
                isinstance(branch, dict) and branch.get("type") != "null"
                for branch in variants
            )
            > 1
        ):
            return True
    return any(_has_union(value) for value in node.values())
