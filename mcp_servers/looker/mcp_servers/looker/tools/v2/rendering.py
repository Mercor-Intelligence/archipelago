"""Rendering tools for V2 Looker API."""

from .models import (
    DownloadRenderedFileRequest,
    DownloadRenderedFileResponse,
    ExportDashboardResponse,
    RenderLookRequest,
)


async def looker_export_dashboard_pdf(
    dashboard_id: str,
    width: int | None = None,
    height: int | None = None,
) -> ExportDashboardResponse:
    """Export a Dashboard as PDF."""
    from query_store import get_next_render_task_id

    # Generate a deterministic render task ID using incrementing counter
    render_task_id = f"render_{get_next_render_task_id()}"

    return ExportDashboardResponse(
        render_task_id=render_task_id,
        dashboard_id=dashboard_id,
        format="pdf",
        status="pending",
    )


async def looker_export_dashboard_png(
    dashboard_id: str,
    width: int | None = None,
    height: int | None = None,
) -> ExportDashboardResponse:
    """Export a Dashboard as PNG."""
    from query_store import get_next_render_task_id

    # Generate a deterministic render task ID using incrementing counter
    render_task_id = f"render_{get_next_render_task_id()}"

    return ExportDashboardResponse(
        render_task_id=render_task_id,
        dashboard_id=dashboard_id,
        format="png",
        status="pending",
    )


async def looker_render_look(request: RenderLookRequest) -> ExportDashboardResponse:
    """Render a Look as PDF or PNG."""
    from query_store import get_next_render_task_id

    # Generate a deterministic render task ID using incrementing counter
    render_task_id = f"render_{get_next_render_task_id()}"

    return ExportDashboardResponse(
        render_task_id=render_task_id,
        dashboard_id=request.look_id,  # Reusing field for look_id
        format=request.format,
        status="pending",
    )


async def looker_download_rendered_file(
    request: DownloadRenderedFileRequest,
) -> DownloadRenderedFileResponse:
    """Download a rendered file (PDF/PNG) from a completed render task."""
    # In mock mode, return a success response with placeholder data
    return DownloadRenderedFileResponse(
        render_task_id=request.render_task_id,
        status="success",
        content_type="application/pdf",
        file_data=None,  # Would contain actual bytes in live mode
        file_size=0,
    )
