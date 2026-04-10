from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger


class LoggingMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.error("No fastmcp context")
            raise ValueError("LoggingMiddleware: No fastmcp context")

        response = await call_next(context)
        if isinstance(response, ToolResult):
            # Truncate large responses to avoid logging huge query results
            content_str = str(response.content)
            if len(content_str) > 200:
                truncated_len = len(content_str)
                content_str = f"{content_str[:200]}... (truncated, {truncated_len} chars)"
            logger.debug(f"{context.method} returned {content_str}")
        else:
            response_str = str(response)
            if len(response_str) > 200:
                truncated_len = len(response_str)
                response_str = f"{response_str[:200]}... (truncated, {truncated_len} chars)"
            logger.debug(f"{context.method} returned {response_str}")
        return response
