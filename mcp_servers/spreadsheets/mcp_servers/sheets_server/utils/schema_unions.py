"""Merge object ``oneOf`` unions into one object node for Gemini.

A discriminated union of models (``Annotated[A | B | ..., Discriminator(...)]``)
is emitted by pydantic as ``oneOf``. Gemini function declarations have no
``oneOf``: Studio's Gemini gate (``find_gemini_schema_violations``) rejects it,
and so does the google-genai typed ``Schema``. ``mcp_schema.flatten_schema`` only
collapses ``anyOf``, so a ``oneOf`` reaches ``tools/list`` untouched.

Keeping just the first branch would hide every other variant's fields (the bug
FOR-304 fixed in Word v2), so every branch is merged instead:

- ``properties`` is the union of every branch's properties. A property that
  branches declare differently is widened (nullable if any branch allows null,
  ``number`` when branches mix ``integer`` and ``number``); when its meaning
  differs per variant, the description says so per variant.
- ``required`` is what every branch requires, plus the discriminator.
- The discriminator (the property each branch pins to its own string tag, e.g.
  ``type``) gets an ``enum`` of every tag.
- The node description lists the extra fields each variant requires.

This changes only the advertised schema. Runtime validation still runs the real
pydantic union, so every payload that validated before still validates, and
every payload that was rejected is still rejected.
"""

from copy import deepcopy
from typing import Any

_UNION_KEYS = ("oneOf",)


_DEFS_KEYS = ("$defs", "definitions")


def merge_object_unions(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` with every all-object ``oneOf`` merged.

    Run it on the raw pydantic schema, before ``flatten_schema``: branches are
    usually ``$ref``s into ``$defs``, and the raw definitions still carry the
    ``const``/``default`` that name each variant (``flatten_schema`` strips
    ``default``, which is the only place a ``type: str = "page_break"`` tag lives).
    Branch ``$ref``s are resolved for the merge; any ``$ref`` left inside the
    merged properties is inlined later by ``flatten_schema`` as usual.
    """
    root = deepcopy(schema)
    defs: dict[str, Any] = {}
    for defs_key in _DEFS_KEYS:
        if isinstance(root.get(defs_key), dict):
            defs.update(
                {f"#/{defs_key}/{name}": d for name, d in root[defs_key].items()}
            )
    for defs_key in _DEFS_KEYS:
        if isinstance(root.get(defs_key), dict):
            # Merge unions inside the definitions too (walking mutates them in place).
            for name, definition in root[defs_key].items():
                root[defs_key][name] = defs[f"#/{defs_key}/{name}"] = _walk(
                    definition, defs
                )
    for key, value in list(root.items()):
        if key not in _DEFS_KEYS:
            root[key] = _walk(value, defs)
    return _merge_node(root, defs)


def _resolve(branch: Any, defs: dict[str, Any]) -> Any:
    """Follow a local ``$ref`` to its definition (one hop, as pydantic emits them)."""
    if isinstance(branch, dict) and isinstance(branch.get("$ref"), str):
        target = defs.get(branch["$ref"])
        if isinstance(target, dict):
            resolved = deepcopy(target)
            resolved.update({k: v for k, v in branch.items() if k != "$ref"})
            return resolved
    return branch


def _walk(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_walk(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    for key, value in list(node.items()):
        node[key] = _walk(value, defs)  # bottom-up: nested unions merge first
    return _merge_node(node, defs)


def _merge_node(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Replace this node's own all-object union with the merged object."""
    for union_key in _UNION_KEYS:
        branches = node.get(union_key)
        if not isinstance(branches, list):
            continue
        merged = _merge_branches(
            [_resolve(b, defs) for b in branches], node.get("description")
        )
        if merged is None:
            continue
        rest = {
            k: v
            for k, v in node.items()
            if k not in (union_key, "discriminator", "description", "type")
        }
        rest.update(merged)
        node = rest
    return node


def _is_null(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null"


def _literal_values(prop: dict[str, Any]) -> list[Any] | None:
    if "const" in prop:
        return [prop["const"]]
    enum = prop.get("enum")
    return list(enum) if isinstance(enum, list) else None


def _tag_value(prop: Any) -> str | None:
    """The string a branch pins this property to (const, 1-value enum, or default)."""
    if not isinstance(prop, dict):
        return None
    values = _literal_values(prop)
    if values is None and isinstance(prop.get("default"), str):
        values = [prop["default"]]
    if values is not None and len(values) == 1 and isinstance(values[0], str):
        return values[0]
    return None


def _find_discriminator(branches: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    for name in branches[0]["properties"]:
        tags = [_tag_value(b["properties"].get(name)) for b in branches]
        if all(tags) and len(set(tags)) == len(tags):
            return name, [t for t in tags if t]
    return None, []


def _types(prop: dict[str, Any]) -> tuple[set[str], bool]:
    """(non-null JSON types, nullable) of a property schema."""
    found: set[str] = set()
    declared = prop.get("type")
    if isinstance(declared, str):
        found.add(declared)
    elif isinstance(declared, list):
        found.update(t for t in declared if isinstance(t, str))
    for option in prop.get("anyOf") or []:
        if isinstance(option, dict) and isinstance(option.get("type"), str):
            found.add(option["type"])
    nullable = "null" in found or prop.get("nullable") is True
    found.discard("null")
    return found, nullable


def _widen(target: dict[str, Any], other: dict[str, Any]) -> None:
    """Relax ``target`` in place so it also admits what ``other`` admits."""
    target_values, other_values = _literal_values(target), _literal_values(other)
    target.pop("const", None)
    if target_values is None or other_values is None:
        target.pop("enum", None)
    else:
        target["enum"] = target_values + [
            v for v in other_values if v not in target_values
        ]

    target_types, target_nullable = _types(target)
    other_types, other_nullable = _types(other)
    if target_types != other_types and target_types | other_types == {
        "integer",
        "number",
    }:
        # integer is a subset of number; advertise the wider one.
        for key in ("type", "anyOf"):
            target.pop(key, None)
        target["type"] = "number"
        if target_nullable or other_nullable:
            target["anyOf"] = [{"type": "number"}, {"type": "null"}]
            target.pop("type")
            target["nullable"] = True
        return
    if other_nullable and not target_nullable:
        if isinstance(target.get("anyOf"), list):
            target["anyOf"] = [*target["anyOf"], {"type": "null"}]
        target["nullable"] = True


def _merge_descriptions(per_variant: list[tuple[str, str | None]]) -> str | None:
    """One description, or per-variant descriptions when the meaning differs."""
    distinct: dict[str, list[str]] = {}
    for tag, description in per_variant:
        if description:
            distinct.setdefault(description, []).append(tag)
    if len(distinct) <= 1:
        return next(iter(distinct), None)
    return "Depends on the variant. " + " | ".join(
        f"{', '.join(tags)}: {description}" for description, tags in distinct.items()
    )


def _merge_branches(
    branches: list[Any], description: str | None
) -> dict[str, Any] | None:
    """Merge object branches into one object schema, or None if they are not all objects."""
    objects = [b for b in branches if not _is_null(b)]
    if len(objects) < 2 or not all(
        isinstance(b, dict)
        and b.get("type") == "object"
        and isinstance(b.get("properties"), dict)
        for b in objects
    ):
        return None

    discriminator, tags = _find_discriminator(objects)
    labels = (
        tags if discriminator else [f"variant {i + 1}" for i in range(len(objects))]
    )

    properties: dict[str, Any] = {}
    descriptions: dict[str, list[tuple[str, str | None]]] = {}
    for label, branch in zip(labels, objects, strict=True):
        for name, prop in branch["properties"].items():
            if not isinstance(prop, dict):
                properties.setdefault(name, prop)
                continue
            descriptions.setdefault(name, []).append((label, prop.get("description")))
            if name not in properties:
                properties[name] = deepcopy(prop)
            elif isinstance(properties[name], dict):
                _widen(properties[name], prop)

    for name, per_variant in descriptions.items():
        merged_description = _merge_descriptions(per_variant)
        if merged_description and isinstance(properties[name], dict):
            properties[name]["description"] = merged_description

    required = [
        name
        for name in objects[0].get("required") or []
        if all(name in (b.get("required") or []) for b in objects[1:])
    ]
    for name, prop in properties.items():
        needed_by = [
            label
            for label, branch in zip(labels, objects, strict=True)
            if name in (branch.get("required") or [])
        ]
        if (
            needed_by
            and name not in required
            and name != discriminator
            and isinstance(prop, dict)
        ):
            note = f"(Required for: {', '.join(needed_by)})"
            prop["description"] = (
                f"{prop['description']} {note}" if prop.get("description") else note
            )

    if discriminator is not None:
        tag_prop = properties[discriminator]
        for key in ("const", "default", "anyOf", "nullable"):
            tag_prop.pop(key, None)
        tag_prop["type"] = "string"
        tag_prop["enum"] = tags
        tag_prop["description"] = (
            f"Variant selector: one of {', '.join(map(repr, tags))}"
        )
        if discriminator not in required:
            required.insert(0, discriminator)

    per_variant_required = []
    for label, branch in zip(labels, objects, strict=True):
        own = [
            n
            for n in branch.get("required") or []
            if n != discriminator and n not in required
        ]
        per_variant_required.append(f"{label} -> {', '.join(own) if own else '(none)'}")
    selector = (
        f"selected by '{discriminator}'" if discriminator else "matching one shape"
    )
    note = (
        f"(Exactly one variant, {selector}. Additional required fields per variant: "
        f"{'; '.join(per_variant_required)}.)"
    )

    merged: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "description": f"{description} {note}" if description else note,
    }
    if required:
        merged["required"] = required
    if len(objects) != len(branches):
        merged["nullable"] = True
    return merged
