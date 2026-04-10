"""OpenBB"""

from .lifespan import lifespan as lifespan
from .openbb_client import OpenBBClient as openBBClient

__all__ = ["lifespan", "openBBClient"]
