from loguru import logger


def create_login_tool(auth_service):
    """
    Create a login tool function.

    Returns a function that can be registered with @mcp.tool()
    """

    async def login(username: str, password: str) -> dict:
        """
        Authenticate user and obtain an access token for API authorization.

        Returns a JWT token that must be included in subsequent authenticated API calls.
        Token validity period depends on server configuration.

        Args:
            username: User's login name (case-sensitive)
            password: User's password (case-sensitive)

        Returns:
            On success: {'user': {'username': str, ...}, 'token': 'jwt_string'}
            On failure: {'error': {'code': 401, 'message': 'Invalid username or password'}}
            On missing params: {'error': {'code': 400, 'message': '...required'}}
        """
        logger.info(f"[mcp-auth] Login attempt for: {username}")

        if not username or not password:
            return {"error": {"code": 400, "message": "Username and password are required"}}

        user = auth_service.validate_user(username, password)
        if not user:
            return {"error": {"code": 401, "message": "Invalid username or password"}}

        token = auth_service.get_or_create_token(username, user)

        logger.info(f"[mcp-auth] Login successful for: {username}")
        return {"user": user, "token": token}

    return login
