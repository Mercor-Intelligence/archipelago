"""
Data Delivery API client for document parsing with S3-backed caching.
"""

from .client import DataDeliveryClient, DataDeliveryConfig

__all__ = ["DataDeliveryClient", "DataDeliveryConfig"]
