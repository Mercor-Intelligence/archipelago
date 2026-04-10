from fastapi import APIRouter, Query

from shared.models.fields import FIELDS, FieldInfo

router = APIRouter()


@router.get("/fields", response_model=list[FieldInfo])
def get_fields(
    request_type: str | None = Query(None),
    service: str | None = Query(None),
    mnemonic: str | None = Query(None),
) -> list[FieldInfo]:
    results = FIELDS

    if request_type:
        results = [f for f in results if request_type in f.request_types]
    if service:
        results = [f for f in results if f.service == service]
    if mnemonic:
        results = [f for f in results if f.mnemonic.lower() == mnemonic.lower()]

    return results
