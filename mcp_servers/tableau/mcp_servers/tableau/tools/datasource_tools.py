"""Datasource CRUD tools implementation.

This module implements all 5 datasource tools:
- Create
- List (with filtering and pagination)
- Get
- Update (partial updates)
- Delete (with cascade)
"""

from db.repositories.datasource_repository import DatasourceRepository
from db.session import get_session
from models import (
    TableauCreateDatasourceInput,
    TableauCreateDatasourceOutput,
    TableauDeleteDatasourceInput,
    TableauDeleteDatasourceOutput,
    TableauGetDatasourceInput,
    TableauGetDatasourceOutput,
    TableauListDatasourcesInput,
    TableauListDatasourcesOutput,
    TableauUpdateDatasourceInput,
    TableauUpdateDatasourceOutput,
)


async def tableau_create_datasource(
    input_data: TableauCreateDatasourceInput,
) -> TableauCreateDatasourceOutput:
    """Create a new datasource in a project."""
    async with get_session() as session:
        repo = DatasourceRepository(session)

        datasource = await repo.create(
            site_id=input_data.site_id,
            name=input_data.name,
            project_id=input_data.project_id,
            owner_id=input_data.owner_id,
            connection_type=input_data.connection_type,
            description=input_data.description,
        )

        return TableauCreateDatasourceOutput(
            id=datasource.id,
            name=datasource.name,
            project_id=datasource.project_id,
            owner_id=datasource.owner_id,
            connection_type=datasource.connection_type,
            description=datasource.description,
            created_at=datasource.created_at.isoformat(),
            updated_at=datasource.updated_at.isoformat(),
        )


async def tableau_list_datasources(
    input_data: TableauListDatasourcesInput,
) -> TableauListDatasourcesOutput:
    """List datasources with optional project filtering and pagination."""
    async with get_session() as session:
        repo = DatasourceRepository(session)

        datasources, total_count = await repo.list_datasources(
            site_id=input_data.site_id,
            project_id=input_data.project_id,
            page_number=input_data.page_number,
            page_size=input_data.page_size,
        )

        datasource_outputs = [
            TableauCreateDatasourceOutput(
                id=ds.id,
                name=ds.name,
                project_id=ds.project_id,
                owner_id=ds.owner_id,
                connection_type=ds.connection_type,
                description=ds.description,
                created_at=ds.created_at.isoformat(),
                updated_at=ds.updated_at.isoformat(),
            )
            for ds in datasources
        ]

        return TableauListDatasourcesOutput(
            datasources=datasource_outputs,
            total_count=total_count,
            page_number=input_data.page_number,
            page_size=input_data.page_size,
        )


async def tableau_get_datasource(
    input_data: TableauGetDatasourceInput,
) -> TableauGetDatasourceOutput:
    """Get a datasource by ID."""
    async with get_session() as session:
        repo = DatasourceRepository(session)

        datasource = await repo.get_by_id(input_data.datasource_id, input_data.site_id)
        if not datasource:
            raise ValueError(f"Datasource with id {input_data.datasource_id} not found")

        return TableauGetDatasourceOutput(
            id=datasource.id,
            name=datasource.name,
            project_id=datasource.project_id,
            owner_id=datasource.owner_id,
            connection_type=datasource.connection_type,
            description=datasource.description,
            created_at=datasource.created_at.isoformat(),
            updated_at=datasource.updated_at.isoformat(),
        )


async def tableau_update_datasource(
    input_data: TableauUpdateDatasourceInput,
) -> TableauUpdateDatasourceOutput:
    """Update datasource name, description, or connection type."""
    async with get_session() as session:
        repo = DatasourceRepository(session)

        datasource = await repo.update(
            datasource_id=input_data.datasource_id,
            site_id=input_data.site_id,
            name=input_data.name,
            description=input_data.description,
            connection_type=input_data.connection_type,
        )

        if not datasource:
            raise ValueError(f"Datasource with id {input_data.datasource_id} not found")

        return TableauUpdateDatasourceOutput(
            id=datasource.id,
            name=datasource.name,
            project_id=datasource.project_id,
            owner_id=datasource.owner_id,
            connection_type=datasource.connection_type,
            description=datasource.description,
            created_at=datasource.created_at.isoformat(),
            updated_at=datasource.updated_at.isoformat(),
        )


async def tableau_delete_datasource(
    input_data: TableauDeleteDatasourceInput,
) -> TableauDeleteDatasourceOutput:
    """Delete a datasource."""
    async with get_session() as session:
        repo = DatasourceRepository(session)

        deleted = await repo.delete(input_data.datasource_id, input_data.site_id)

        if not deleted:
            raise ValueError(f"Datasource with id {input_data.datasource_id} not found")

        return TableauDeleteDatasourceOutput(
            success=True,
            message=f"Datasource {input_data.datasource_id} deleted successfully",
        )
