"""FastAPI application initialization."""

import logging

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from openbb_app.lifespan import lifespan
from openbb_app.openbb_client import get_openbb_client

from .config import settings
from .middleware.error_handler import BloombergErrorMiddleware
from .routes import (
    field_router,
    hello_router,
    refdata_router,
    stream_router,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Initialize FastAPI app
app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

# Add Bloomberg error handling middleware (first to catch all errors)
app.add_middleware(BloombergErrorMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(hello_router, tags=["basic"])
app.include_router(stream_router, tags=["streaming"])
app.include_router(field_router, tags=["info"])
app.include_router(refdata_router, tags=["refdata"])


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "BLPAPI Emulator", "version": settings.app_version}


@app.get("/health", tags=["health"])
async def health_check():
    """Detailed health check with full provider status."""
    try:
        client = get_openbb_client()
        health = client.get_health()

        return {
            "status": health.status.value,
            "providers": [{"name": p.name, "error": p.error} for p in health.providers],
            "error": health.error,
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "error": str(e)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
