"""USPTO response transformation helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MIME_TYPE_MAP = {
    "PDF": "application/pdf",
    "TIFF": "image/tiff",
    "XML": "application/xml",
    "HTML": "text/html",
}

BAG_KEY_MAP = {
    "patentFileWrapperDataBag": "results",
    "statusCodeBag": "statusCodes",
    "documentBag": "documents",
    "downloadOptionBag": "downloadOptions",
    "foreignPriorityBag": "foreignPriorityClaims",
}

FIELD_NAME_MAP = {
    "pageTotalQuantity": "pageCount",
    "applicationStatusDescriptionText": "statusDescriptionText",
}

MIME_FIELDS = {"format", "mimeType", "mimeTypeIdentifier"}


def normalize_mime_type(value: str) -> str:
    """Normalize MIME types to standard values."""
    return MIME_TYPE_MAP.get(value.upper(), value)


def normalize_key(key: str) -> str:
    """Normalize field names and remove Bag suffixes."""
    if key in BAG_KEY_MAP:
        return BAG_KEY_MAP[key]
    if key in FIELD_NAME_MAP:
        return FIELD_NAME_MAP[key]
    if key.endswith("Bag"):
        return key[: -len("Bag")]
    return key


def normalize_item(
    item: Any,
    *,
    normalize_mime_values: bool = False,
    mime_fields: Iterable[str] | None = None,
) -> Any:
    """Recursively normalize keys and optionally MIME values."""
    if isinstance(item, list):
        return [
            normalize_item(
                entry,
                normalize_mime_values=normalize_mime_values,
                mime_fields=mime_fields,
            )
            for entry in item
        ]

    if isinstance(item, dict):
        normalized: dict[str, Any] = {}
        mime_field_set = set(mime_fields or [])
        for key, value in item.items():
            normalized_key = normalize_key(key)
            if isinstance(value, str):
                if normalize_mime_values:
                    value = normalize_mime_type(value)
                elif normalized_key in mime_field_set:
                    value = normalize_mime_type(value)
            normalized[normalized_key] = normalize_item(
                value,
                normalize_mime_values=normalize_mime_values,
                mime_fields=mime_fields,
            )
        return normalized

    if normalize_mime_values and isinstance(item, str):
        return normalize_mime_type(item)

    return item


def transform_search_results(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO search response to normalized format."""
    transformed: dict[str, Any] = {}
    raw_results = raw_response.get("patentFileWrapperDataBag")
    if raw_results is None:
        raw_results = raw_response.get("results") or []

    results: list[dict[str, Any]] = []
    for item in raw_results or []:
        if not isinstance(item, dict):
            continue
        normalized_item = {}
        for key, value in item.items():
            if key == "applicationMetaData" and isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    normalized_nested_key = normalize_key(nested_key)
                    if normalized_nested_key not in normalized_item:
                        normalized_item[normalized_nested_key] = normalize_item(nested_value)
                continue
            normalized_key = normalize_key(key)
            normalized_item[normalized_key] = normalize_item(value)
        results.append(normalized_item)

    for key, value in raw_response.items():
        if key in ("patentFileWrapperDataBag", "results", "raw_uspto_response"):
            continue
        transformed[normalize_key(key)] = normalize_item(value)

    transformed["results"] = results
    transformed["raw_uspto_response"] = raw_response.get("raw_uspto_response", raw_response)
    return transformed


def transform_application_details(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO application detail response to normalized format."""
    normalized = normalize_item(raw_response)
    application_metadata = normalized.get("applicationMetaData")
    if isinstance(application_metadata, dict):
        for key, value in application_metadata.items():
            if key not in normalized:
                normalized[key] = value
    if "bibliographic" not in normalized:
        normalized["bibliographic"] = _extract_bibliographic_data(normalized, raw_response)
    if "prosecutionEvents" not in normalized:
        normalized["prosecutionEvents"] = _extract_prosecution_events(normalized)
    normalized["raw_uspto_response"] = raw_response.get("raw_uspto_response", raw_response)
    return normalized


def transform_documents(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO documents response to normalized format."""
    transformed: dict[str, Any] = {}
    raw_documents = raw_response.get("documentBag")
    if raw_documents is None:
        raw_documents = raw_response.get("documents") or []

    documents = []
    for item in raw_documents or []:
        if not isinstance(item, dict):
            continue
        documents.append(normalize_item(item, mime_fields=MIME_FIELDS))

    for key, value in raw_response.items():
        if key in ("documentBag", "documents", "raw_uspto_response"):
            continue
        transformed[normalize_key(key)] = normalize_item(value, mime_fields=MIME_FIELDS)

    transformed["documents"] = documents
    transformed["raw_uspto_response"] = raw_response.get("raw_uspto_response", raw_response)
    return transformed


def transform_status_codes(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO status codes response to normalized format."""
    transformed: dict[str, Any] = {}
    raw_codes = raw_response.get("statusCodeBag")
    if raw_codes is None:
        raw_codes = raw_response.get("statusCodes") or []

    status_codes = []
    for item in raw_codes or []:
        if not isinstance(item, dict):
            continue
        code = item.get("statusCode", item.get("applicationStatusCode"))
        desc = item.get("statusDescriptionText", item.get("applicationStatusDescriptionText"))
        status_codes.append(
            {
                "statusCode": "" if code is None else str(code),
                "statusDescriptionText": "" if desc is None else str(desc),
            }
        )

    for key, value in raw_response.items():
        if key in ("statusCodeBag", "statusCodes", "raw_uspto_response"):
            continue
        transformed[normalize_key(key)] = normalize_item(value)

    transformed["statusCodes"] = status_codes
    if not transformed.get("version"):
        transformed["version"] = raw_response.get("version") or "unknown"
    transformed["raw_uspto_response"] = raw_response.get("raw_uspto_response", raw_response)
    return transformed


def transform_foreign_priority(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Transform USPTO foreign priority response to normalized format."""
    claim_keys = {
        "foreignApplicationNumber",
        "foreignFilingDate",
        "ipOfficeCode",
        "ipOfficeName",
        "priorityClaimIndicator",
        "foreignCountryCode",
        "country",
        "applicationNumberText",
        "filingDate",
        "ipOfficeCountry",
    }
    bag_keys = (
        "foreignPriorityBag",
        "foreignPriorityClaims",
        "foreignPriorityClaim",
        "foreignPriority",
        "priorityClaims",
        "priorityClaim",
    )

    def _extract_claims(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            for item in items:
                if any(key in item for key in bag_keys):
                    nested = _extract_claims(item)
                    if nested:
                        return nested
            if any(claim_keys.intersection(item.keys()) for item in items):
                return items
            return []
        if isinstance(payload, dict):
            for key in bag_keys:
                value = payload.get(key)
                if value is not None:
                    nested = _extract_claims(value)
                    if nested:
                        return nested
            if claim_keys.intersection(payload.keys()):
                return [payload]
            for value in payload.values():
                nested = _extract_claims(value)
                if nested:
                    return nested
        return []

    transformed: dict[str, Any] = {}
    raw_claims = raw_response.get("foreignPriorityBag")
    if raw_claims is None:
        raw_claims = raw_response.get("foreignPriorityClaims")
    if not raw_claims:
        raw_claims = raw_response

    claims = []
    for item in _extract_claims(raw_claims):
        if not isinstance(item, dict):
            continue
        claims.append(normalize_item(item))

    for key, value in raw_response.items():
        if key in ("foreignPriorityBag", "foreignPriorityClaims", "raw_uspto_response"):
            continue
        transformed[normalize_key(key)] = normalize_item(value)

    transformed["foreignPriorityClaims"] = claims
    transformed["raw_uspto_response"] = raw_response.get("raw_uspto_response", raw_response)
    return transformed


def _extract_bibliographic_data(
    normalized: dict[str, Any],
    raw_response: dict[str, Any],
) -> dict[str, Any]:
    source = normalized.get("applicationMetaData")
    if not isinstance(source, dict):
        source = {}
    bibliographic_fields = [
        "inventionTitle",
        "filingDate",
        "publicationDate",
        "publicationNumber",
        "patentNumber",
        "patentIssueDate",
        "firstNamedApplicant",
        "assigneeEntityName",
        "inventorNameArrayText",
    ]
    bibliographic: dict[str, Any] = {}
    for field in bibliographic_fields:
        if field in normalized:
            bibliographic[field] = normalized.get(field)
        elif field in source:
            bibliographic[field] = source.get(field)
        elif field in raw_response:
            bibliographic[field] = raw_response.get(field)
    return bibliographic


def _extract_prosecution_events(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_keys = [
        "prosecutionHistory",
        "prosecutionEvents",
        "prosecutionEvent",
        "prosecutionHistoryBag",
        "prosecutionEventBag",
        "eventBag",
        "eventDataBag",
        "eventData",
        "event",
        "events",
    ]
    raw_events: list[dict[str, Any]] = []
    for key in candidate_keys:
        value = normalized.get(key)
        if isinstance(value, list):
            raw_events = [item for item in value if isinstance(item, dict)]
            if raw_events:
                break

    events = []
    for item in raw_events:
        events.append(
            {
                "eventCode": item.get("eventCode") or item.get("eventCodeText"),
                "eventDate": item.get("eventDate") or item.get("eventDateText"),
                "description": item.get("eventDescriptionText") or item.get("description"),
                "documentReference": item.get("documentIdentifier")
                or item.get("documentReference"),
            }
        )
    return events


def extract_prosecution_events_from_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return normalized prosecution events from a raw USPTO payload."""
    if not payload or not isinstance(payload, dict):
        return []
    normalized = normalize_item(payload)
    return _extract_prosecution_events(normalized)
