"""PermissionRepository for managing permission operations.

This repository handles all database operations for permissions, following
Tableau REST API v3.x behavior patterns.
"""

from uuid import uuid4

from db.models import Datasource, Group, Permission, Project, User, Workbook
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession


class PermissionRepository:
    """Repository for permission management matching Tableau API behavior."""

    # Valid enum values for permissions
    VALID_RESOURCE_TYPES = ["project", "workbook", "datasource"]
    VALID_GRANTEE_TYPES = ["user", "group"]
    VALID_CAPABILITIES = ["Read", "Write", "ChangePermissions"]
    VALID_MODES = ["Allow", "Deny"]

    @staticmethod
    def _validate_enum(value: str, valid_values: list[str], field_name: str) -> None:
        """Validate that value is in allowed set.

        Args:
            value: Value to validate
            valid_values: List of allowed values
            field_name: Name of field for error message

        Raises:
            ValueError: If value not in valid_values
        """
        if value not in valid_values:
            raise ValueError(
                f"Invalid {field_name} '{value}'. Must be one of: {', '.join(valid_values)}"
            )

    @staticmethod
    async def grant_permission(
        session: AsyncSession,
        site_id: str,
        resource_type: str,
        resource_id: str,
        grantee_type: str,
        grantee_id: str,
        capability: str,
        mode: str,
    ) -> Permission:
        """Grant a permission on a resource to a user or group (idempotent).

        Args:
            session: Database session
            site_id: Site identifier (for API consistency)
            resource_type: Type of resource ('project', 'workbook', 'datasource')
            resource_id: Resource UUID
            grantee_type: Type of grantee ('user', 'group')
            grantee_id: Grantee UUID
            capability: Permission capability ('Read', 'Write', 'ChangePermissions')
            mode: Permission mode ('Allow', 'Deny')

        Returns:
            Created or existing Permission instance

        Raises:
            ValueError: If any validation fails

        Note:
            Does not commit the transaction. Caller is responsible for committing
            via the context manager to ensure transactional integrity.
            This operation is idempotent - granting the same permission twice
            returns the existing permission.

            site_id is included for API consistency but resources are not
            directly scoped to sites in the current schema.
        """
        # Validate enums
        PermissionRepository._validate_enum(
            resource_type, PermissionRepository.VALID_RESOURCE_TYPES, "resource_type"
        )
        PermissionRepository._validate_enum(
            grantee_type, PermissionRepository.VALID_GRANTEE_TYPES, "grantee_type"
        )
        PermissionRepository._validate_enum(
            capability, PermissionRepository.VALID_CAPABILITIES, "capability"
        )
        PermissionRepository._validate_enum(mode, PermissionRepository.VALID_MODES, "mode")

        # Validate resource exists
        await PermissionRepository._validate_resource_exists(session, resource_type, resource_id)

        # Validate grantee exists
        await PermissionRepository._validate_grantee_exists(session, grantee_type, grantee_id)

        # Check if permission already exists (idempotency)
        existing = await PermissionRepository._get_permission(
            session, resource_type, resource_id, grantee_type, grantee_id, capability, mode
        )

        if existing:
            return existing

        # Create new permission
        permission = Permission(
            id=str(uuid4()),
            resource_type=resource_type,
            resource_id=resource_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            capability=capability,
            mode=mode,
        )
        session.add(permission)
        await session.flush()  # Flush to get generated values without committing
        return permission

    @staticmethod
    async def list_permissions(
        session: AsyncSession,
        site_id: str,
        resource_type: str,
        resource_id: str,
    ) -> list[Permission]:
        """List all permissions for a resource.

        Args:
            session: Database session
            site_id: Site identifier (for API consistency)
            resource_type: Type of resource ('project', 'workbook', 'datasource')
            resource_id: Resource UUID

        Returns:
            List of Permission instances for the resource (ordered by created_at, id)

        Raises:
            ValueError: If resource_type is invalid or resource doesn't exist

        Note:
            site_id is included for API consistency but resources are not
            directly scoped to sites in the current schema.
        """
        # Validate resource_type
        PermissionRepository._validate_enum(
            resource_type, PermissionRepository.VALID_RESOURCE_TYPES, "resource_type"
        )

        # Validate resource exists
        await PermissionRepository._validate_resource_exists(session, resource_type, resource_id)

        # Query permissions for this resource with deterministic ordering
        stmt = (
            select(Permission)
            .where(
                and_(
                    Permission.resource_type == resource_type,
                    Permission.resource_id == resource_id,
                )
            )
            .order_by(Permission.created_at, Permission.id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def revoke_permission(
        session: AsyncSession,
        site_id: str,
        resource_type: str,
        resource_id: str,
        grantee_id: str,
        capability: str,
        mode: str,
    ) -> bool:
        """Revoke a permission from a resource.

        Args:
            session: Database session
            site_id: Site identifier (for API consistency)
            resource_type: Type of resource ('project', 'workbook', 'datasource')
            resource_id: Resource UUID
            grantee_id: Grantee UUID (user or group)
            capability: Permission capability to revoke
            mode: Permission mode ('Allow', 'Deny')

        Returns:
            True (always returns True unless an exception is raised)

        Raises:
            ValueError: If validation fails or permission not found

        Note:
            Does not commit the transaction. Caller is responsible for committing.
            This method revokes only the specific permission matching the provided
            mode (Allow or Deny), consistent with Tableau REST API behavior.

            site_id is included for API consistency but resources are not
            directly scoped to sites in the current schema.
        """
        # Validate enums
        PermissionRepository._validate_enum(
            resource_type, PermissionRepository.VALID_RESOURCE_TYPES, "resource_type"
        )
        PermissionRepository._validate_enum(
            capability, PermissionRepository.VALID_CAPABILITIES, "capability"
        )
        PermissionRepository._validate_enum(mode, PermissionRepository.VALID_MODES, "mode")

        # Validate resource exists
        await PermissionRepository._validate_resource_exists(session, resource_type, resource_id)

        # Validate grantee exists (check both user and group)
        await PermissionRepository._validate_grantee_exists_by_id(session, grantee_id)

        # Find the specific permission to revoke (matching mode)
        stmt = select(Permission).where(
            and_(
                Permission.resource_type == resource_type,
                Permission.resource_id == resource_id,
                Permission.grantee_id == grantee_id,
                Permission.capability == capability,
                Permission.mode == mode,
            )
        )
        result = await session.execute(stmt)
        permission = result.scalar_one_or_none()

        if not permission:
            raise ValueError(
                f"Permission not found: {capability} ({mode}) on {resource_type} {resource_id} "
                f"for grantee {grantee_id}"
            )

        # Delete the specific permission
        await session.delete(permission)

        await session.flush()  # Flush deletions without committing
        return True

    @staticmethod
    async def _validate_resource_exists(
        session: AsyncSession,
        resource_type: str,
        resource_id: str,
    ) -> None:
        """Validate that a resource exists.

        Args:
            session: Database session
            resource_type: Type of resource
            resource_id: Resource UUID

        Raises:
            ValueError: If resource doesn't exist
        """
        if resource_type == "project":
            stmt = select(Project).where(Project.id == resource_id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                raise ValueError(f"Project with id {resource_id} does not exist")

        elif resource_type == "workbook":
            stmt = select(Workbook).where(Workbook.id == resource_id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                raise ValueError(f"Workbook with id {resource_id} does not exist")

        elif resource_type == "datasource":
            stmt = select(Datasource).where(Datasource.id == resource_id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                raise ValueError(f"Datasource with id {resource_id} does not exist")

    @staticmethod
    async def _validate_grantee_exists(
        session: AsyncSession,
        grantee_type: str,
        grantee_id: str,
    ) -> None:
        """Validate that a grantee (user or group) exists.

        Args:
            session: Database session
            grantee_type: Type of grantee ('user', 'group')
            grantee_id: Grantee UUID

        Raises:
            ValueError: If grantee doesn't exist
        """
        if grantee_type == "user":
            stmt = select(User).where(User.id == grantee_id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                raise ValueError(f"User with id {grantee_id} does not exist")

        elif grantee_type == "group":
            stmt = select(Group).where(Group.id == grantee_id)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                raise ValueError(f"Group with id {grantee_id} does not exist")

    @staticmethod
    async def _validate_grantee_exists_by_id(
        session: AsyncSession,
        grantee_id: str,
    ) -> None:
        """Validate that a grantee exists as either a user or group.

        Used when grantee_type is not known (e.g., in revoke operations).

        Args:
            session: Database session
            grantee_id: Grantee UUID

        Raises:
            ValueError: If grantee doesn't exist as either user or group
        """
        # Check if it's a user
        user_stmt = select(User).where(User.id == grantee_id)
        user_result = await session.execute(user_stmt)
        if user_result.scalar_one_or_none():
            return  # Found as user

        # Check if it's a group
        group_stmt = select(Group).where(Group.id == grantee_id)
        group_result = await session.execute(group_stmt)
        if group_result.scalar_one_or_none():
            return  # Found as group

        # Not found as either user or group
        raise ValueError(f"Grantee with id {grantee_id} does not exist as user or group")

    @staticmethod
    async def _get_permission(
        session: AsyncSession,
        resource_type: str,
        resource_id: str,
        grantee_type: str,
        grantee_id: str,
        capability: str,
        mode: str,
    ) -> Permission | None:
        """Get existing permission if it exists (for idempotency check).

        Args:
            session: Database session
            resource_type: Type of resource
            resource_id: Resource UUID
            grantee_type: Type of grantee
            grantee_id: Grantee UUID
            capability: Permission capability
            mode: Permission mode

        Returns:
            Existing Permission or None
        """
        stmt = select(Permission).where(
            and_(
                Permission.resource_type == resource_type,
                Permission.resource_id == resource_id,
                Permission.grantee_type == grantee_type,
                Permission.grantee_id == grantee_id,
                Permission.capability == capability,
                Permission.mode == mode,
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
