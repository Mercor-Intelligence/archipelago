"""Permission CRUD tools matching Tableau REST API v3.x behavior.

Implements 3 permission management tools:
- tableau_grant_permission (idempotent)
- tableau_list_permissions
- tableau_revoke_permission

All tools follow Tableau API v3.x specifications validated against official docs.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import os
import uuid
from datetime import datetime, timezone

from db.repositories.permission_repository import PermissionRepository
from db.session import get_session
from models import (
    TableauGrantPermissionInput,
    TableauGrantPermissionOutput,
    TableauListPermissionsInput,
    TableauListPermissionsOutput,
    TableauRevokePermissionInput,
    TableauRevokePermissionOutput,
)


def _get_permission_endpoint(resource_type: str, resource_id: str, site_id: str) -> str:
    """Get the permission endpoint for a resource type.

    Args:
        resource_type: 'project', 'workbook', or 'datasource'
        resource_id: ID of the resource
        site_id: Site UUID

    Returns:
        API endpoint path
    """
    resource_plural = {
        "project": "projects",
        "workbook": "workbooks",
        "datasource": "datasources",
    }
    return f"sites/{site_id}/{resource_plural[resource_type]}/{resource_id}/permissions"


async def _list_permissions_http(
    input_data: TableauListPermissionsInput,
) -> TableauListPermissionsOutput:
    """List permissions via Tableau REST API.

    Args:
        input_data: List permissions request

    Returns:
        List of permissions for the resource
    """
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_content_url = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_content_url, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_content_url,
        personal_access_token=f"{token_name}:{token_secret}",
    )
    await client.sign_in()

    endpoint = _get_permission_endpoint(
        input_data.resource_type, input_data.resource_id, client.site_id
    )
    response_data = await client.get(endpoint)

    # Parse permissions from response
    permissions = []
    perms_data = response_data.get("permissions", {})

    # Handle granteeCapabilities array
    grantee_caps = perms_data.get("granteeCapabilities", [])
    for gc in grantee_caps:
        # Determine grantee type and ID
        if "user" in gc:
            grantee_type = "user"
            grantee_id = gc["user"].get("id")
        elif "group" in gc:
            grantee_type = "group"
            grantee_id = gc["group"].get("id")
        else:
            continue

        # Extract capabilities
        caps = gc.get("capabilities", {}).get("capability", [])
        for cap in caps:
            cap_name = cap.get("name")
            cap_mode = cap.get("mode")
            if cap_name and cap_mode:
                permissions.append(
                    TableauGrantPermissionOutput(
                        id=str(uuid.uuid4()),  # Generate ID since API doesn't return one
                        resource_type=input_data.resource_type,
                        resource_id=input_data.resource_id,
                        grantee_type=grantee_type,
                        grantee_id=grantee_id,
                        capability=cap_name,
                        mode=cap_mode,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )

    return TableauListPermissionsOutput(permissions=permissions)


async def _grant_permission_http(
    input_data: TableauGrantPermissionInput,
) -> TableauGrantPermissionOutput:
    """Grant permission via Tableau REST API.

    Args:
        input_data: Grant permission request

    Returns:
        Granted permission details
    """
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_content_url = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_content_url, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_content_url,
        personal_access_token=f"{token_name}:{token_secret}",
    )
    await client.sign_in()

    endpoint = _get_permission_endpoint(
        input_data.resource_type, input_data.resource_id, client.site_id
    )

    # Build request payload
    grantee_key = input_data.grantee_type
    payload = {
        "permissions": {
            "granteeCapabilities": [
                {
                    grantee_key: {"id": input_data.grantee_id},
                    "capabilities": {
                        "capability": [{"name": input_data.capability, "mode": input_data.mode}]
                    },
                }
            ]
        }
    }

    await client.put(endpoint, payload)

    return TableauGrantPermissionOutput(
        id=str(uuid.uuid4()),
        resource_type=input_data.resource_type,
        resource_id=input_data.resource_id,
        grantee_type=input_data.grantee_type,
        grantee_id=input_data.grantee_id,
        capability=input_data.capability,
        mode=input_data.mode,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


async def _revoke_permission_http(
    input_data: TableauRevokePermissionInput,
) -> TableauRevokePermissionOutput:
    """Revoke permission via Tableau REST API.

    Args:
        input_data: Revoke permission request

    Returns:
        Success status
    """
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_content_url = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_content_url, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_content_url,
        personal_access_token=f"{token_name}:{token_secret}",
    )
    await client.sign_in()

    # Validate grantee_type before building URL
    if input_data.grantee_type not in ("user", "group"):
        raise ValueError(
            f"Invalid grantee_type '{input_data.grantee_type}'. Must be 'user' or 'group'."
        )

    # Build delete URL with path parameters
    grantee_type_plural = "users" if input_data.grantee_type == "user" else "groups"
    resource_plural = {
        "project": "projects",
        "workbook": "workbooks",
        "datasource": "datasources",
    }

    endpoint = (
        f"sites/{client.site_id}/{resource_plural[input_data.resource_type]}/"
        f"{input_data.resource_id}/permissions/{grantee_type_plural}/"
        f"{input_data.grantee_id}/{input_data.capability}/{input_data.mode}"
    )

    await client.delete(endpoint)

    return TableauRevokePermissionOutput(success=True)


async def tableau_grant_permission(
    input_data: TableauGrantPermissionInput,
) -> TableauGrantPermissionOutput:
    """Grant a permission on a resource to a user or group (idempotent)."""
    # HTTP mode: call Tableau REST API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _grant_permission_http(input_data)

    # Local mode: use database
    async with get_session() as session:
        permission = await PermissionRepository.grant_permission(
            session=session,
            site_id=input_data.site_id,
            resource_type=input_data.resource_type,
            resource_id=input_data.resource_id,
            grantee_type=input_data.grantee_type,
            grantee_id=input_data.grantee_id,
            capability=input_data.capability,
            mode=input_data.mode,
        )

        return TableauGrantPermissionOutput(
            id=permission.id,
            resource_type=permission.resource_type,
            resource_id=permission.resource_id,
            grantee_type=permission.grantee_type,
            grantee_id=permission.grantee_id,
            capability=permission.capability,
            mode=permission.mode,
            created_at=permission.created_at.isoformat(),
        )


async def tableau_list_permissions(
    input_data: TableauListPermissionsInput,
) -> TableauListPermissionsOutput:
    """List all permissions for a resource."""
    # HTTP mode: call Tableau REST API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _list_permissions_http(input_data)

    # Local mode: use database
    async with get_session() as session:
        permissions = await PermissionRepository.list_permissions(
            session=session,
            site_id=input_data.site_id,
            resource_type=input_data.resource_type,
            resource_id=input_data.resource_id,
        )

        return TableauListPermissionsOutput(
            permissions=[
                TableauGrantPermissionOutput(
                    id=perm.id,
                    resource_type=perm.resource_type,
                    resource_id=perm.resource_id,
                    grantee_type=perm.grantee_type,
                    grantee_id=perm.grantee_id,
                    capability=perm.capability,
                    mode=perm.mode,
                    created_at=perm.created_at.isoformat(),
                )
                for perm in permissions
            ]
        )


async def tableau_revoke_permission(
    input_data: TableauRevokePermissionInput,
) -> TableauRevokePermissionOutput:
    """Revoke a permission from a resource."""
    # HTTP mode: call Tableau REST API directly
    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _revoke_permission_http(input_data)

    # Local mode: use database
    async with get_session() as session:
        await PermissionRepository.revoke_permission(
            session=session,
            site_id=input_data.site_id,
            resource_type=input_data.resource_type,
            resource_id=input_data.resource_id,
            grantee_id=input_data.grantee_id,
            capability=input_data.capability,
            mode=input_data.mode,
        )

        return TableauRevokePermissionOutput(success=True)
