from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger


class LoggingMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        # Handle requests that may not have fastmcp_context (internal/non-tool routes)
        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.debug(f"Request without fastmcp context: {context.method}")
            return await call_next(context)

        response = await call_next(context)
        if isinstance(response, ToolResult):
            logger.debug(f"{context.method} returned {response.content}")
        else:
            logger.debug(f"{context.method} returned {response}")
        return response
