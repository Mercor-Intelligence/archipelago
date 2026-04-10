"""USPTO offline mode data ingestion.

This module provides USPTO-specific implementations for the generic
data ingestion framework.
"""

from .factory import patent_grant_record_factory
from .persister import USPTOPatentPersister

__all__ = ["USPTOPatentPersister", "patent_grant_record_factory"]
