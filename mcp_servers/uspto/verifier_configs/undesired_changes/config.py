from pydantic import BaseModel

from ..helpers import config_from_pydantic


class UndesiredChangesInput(BaseModel):
    pass


UNDESIRED_CHANGES_CONFIG = config_from_pydantic(
    config_id="undesired_changes",
    name="Undesired Changes Verifier",
    description="Universal verifier that identifies files that were incorrectly created, modified, or deleted. "
    "Outputs integer counts for each type of undesired change.",
    input_model=UndesiredChangesInput,
)
