"""LookML management tools."""

from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field


async def generate_lookml(
    ctx: Context,
    model_name: Annotated[
        str,
        Field(
            description="Name for the generated LookML model",
            default="seeded_data",
        ),
    ] = "seeded_data",
    connection: Annotated[
        str,
        Field(
            description="Database connection name or LookML constant reference",
            default="@{database_connection}",
        ),
    ] = "@{database_connection}",
) -> dict:
    """Generate LookML view and model files from CSV data."""
    from lookml_generator import ensure_lookml_generated

    result = ensure_lookml_generated(model_name=model_name, connection=connection)

    if "error" in result:
        return {"error": result["error"]}

    return {
        "success": True,
        "model": result["model"],
        "views": result["views"],
        "files": list(result["files"].keys()),
        "output_dir": result["output_dir"],
    }


async def get_generated_lookml(
    ctx: Context,
    view_name: Annotated[
        str,
        Field(description="Name of the view to get LookML for (e.g., 'service_requests')"),
    ],
) -> dict:
    """Get the generated LookML content for a specific view."""
    from lookml_generator import get_lookml_for_view

    content = get_lookml_for_view(view_name)

    if content is None:
        return {
            "error": f"No CSV data found for view: {view_name}",
            "hint": "View name should match a CSV filename (without .csv extension)",
        }

    return {
        "view_name": view_name,
        "lookml": content,
    }


async def list_available_views(ctx: Context) -> dict:
    """List all views that can be generated from CSV data."""
    from duckdb_query_executor import get_available_seeded_views

    views = get_available_seeded_views()

    return {
        "views": views,
        "count": len(views),
    }


async def deploy_lookml(
    ctx: Context,
    model_name: Annotated[
        str,
        Field(
            description="Name for the LookML model",
            default="seeded_data",
        ),
    ] = "seeded_data",
    connection: Annotated[
        str,
        Field(
            description="Database connection name or LookML constant reference",
            default="@{database_connection}",
        ),
    ] = "@{database_connection}",
    trigger_looker_deploy: Annotated[
        bool,
        Field(
            description="Whether to trigger Looker deploy webhook after Git push",
            default=True,
        ),
    ] = True,
) -> dict:
    """Deploy generated LookML to Looker via Git."""
    from lookml_deployer import deploy_lookml_full, get_deploy_config_from_env

    config = get_deploy_config_from_env()

    if config is None:
        # Fall back to local generation when Git isn't configured
        from lookml_generator import ensure_lookml_generated

        result = ensure_lookml_generated(model_name=model_name, connection=connection)

        if "error" in result:
            return {"error": result["error"]}

        return {
            "success": True,
            "deployed": False,
            "mode": "local",
            "message": "Git not configured - LookML files generated locally",
            "model": result["model"],
            "views": result["views"],
            "files": list(result["files"].keys()),
            "output_dir": result["output_dir"],
        }

    # Override config with provided values
    config.project_name = model_name
    config.connection_name = connection

    result = await deploy_lookml_full(config, trigger_deploy=trigger_looker_deploy)

    return result
