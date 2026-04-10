from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class FieldInfo(BaseModel):
    mnemonic: str
    description: str
    data_type: str | None = None
    request_types: list[
        Literal[
            "BeqsRequest", "ReferenceDataRequest", "HistoricalDataRequest", "IntradayBarRequest"
        ]
    ] = []
    service: str | None = None
    openbb_mapping: str | None = None


def load_fields(path: str | Path) -> list[FieldInfo]:
    with open(path) as f:
        raw = yaml.safe_load(f)
    raw = raw if raw is not None else []
    return [FieldInfo(**item) for item in raw]


# Load fields dynamically
FIELDS_PATH = Path(__file__).parent.parent / "data/fields.yaml"
FIELDS = load_fields(FIELDS_PATH)
