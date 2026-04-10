"""Tool to reload the data layer after CSV files have been written."""

from pydantic import BaseModel, Field


class ReloadDataRequest(BaseModel):
    """Request to reload the data layer."""

    pass  # No parameters needed


class ReloadDataResponse(BaseModel):
    """Response from data reload operation."""

    success: bool = Field(..., description="Whether the reload was successful")
    message: str = Field(..., description="Human-readable status message")
    model_count: int | None = Field(None, description="Number of models loaded")
    explore_count: int | None = Field(None, description="Number of explores loaded")


async def reload_data(request: ReloadDataRequest) -> ReloadDataResponse:
    """Reload the data layer to pick up new CSV files.

    This triggers initialize_data_layer(force_reload=True) which:
    - Re-scans STATE_LOCATION for CSV files
    - Imports any new CSVs into DuckDB
    - Rebuilds the in-memory model/explore metadata
    """
    from data_layer import get_lookml_explores, get_lookml_models, initialize_data_layer

    try:
        initialize_data_layer(force_reload=True)

        models = get_lookml_models()
        explores = get_lookml_explores()

        return ReloadDataResponse(
            success=True,
            message="Data layer reloaded successfully",
            model_count=len(models),
            explore_count=len(explores),
        )
    except Exception as e:
        return ReloadDataResponse(
            success=False,
            message=f"Failed to reload data layer: {e}",
        )
