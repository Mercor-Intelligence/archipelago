from typing import Any

from shared.models.fields import FIELDS


def fetch_field_data(request_type: str = "") -> dict[str, Any]:
    if request_type:
        data = [f.model_dump() for f in FIELDS if request_type in f.request_types]
    else:
        data = [f.model_dump() for f in FIELDS]

    return {"fields": data}
