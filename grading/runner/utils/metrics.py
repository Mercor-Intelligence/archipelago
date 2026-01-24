import time

from datadog_api_client import Configuration, ThreadedApiClient
from datadog_api_client.v2.api.metrics_api import MetricsApi
from datadog_api_client.v2.model.metric_intake_type import MetricIntakeType
from datadog_api_client.v2.model.metric_payload import MetricPayload
from datadog_api_client.v2.model.metric_point import MetricPoint
from datadog_api_client.v2.model.metric_series import MetricSeries
from loguru import logger

from runner.utils.settings import get_settings

settings = get_settings()

_api_client: ThreadedApiClient | None = None

if settings.DATADOG_API_KEY:
    configuration = Configuration()
    configuration.api_key["apiKeyAuth"] = settings.DATADOG_API_KEY
    _api_client = ThreadedApiClient(configuration)

BASE_TAGS = [f"env:{settings.ENV.value}", "service:rl-studio-grading"]


def increment(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
    if not _api_client:
        return

    all_tags = BASE_TAGS + (tags or [])
    try:
        series = MetricSeries(
            metric=metric,
            type=MetricIntakeType.COUNT,
            points=[MetricPoint(timestamp=int(time.time()), value=float(value))],
            tags=all_tags,
        )
        MetricsApi(api_client=_api_client).submit_metrics(
            body=MetricPayload(series=[series])
        )
    except Exception as e:
        logger.debug(f"Error sending metric to Datadog: {e}")


def gauge(metric: str, value: float, tags: list[str] | None = None) -> None:
    if not _api_client:
        return

    all_tags = BASE_TAGS + (tags or [])
    try:
        series = MetricSeries(
            metric=metric,
            type=MetricIntakeType.GAUGE,
            points=[MetricPoint(timestamp=int(time.time()), value=value)],
            tags=all_tags,
        )
        MetricsApi(api_client=_api_client).submit_metrics(
            body=MetricPayload(series=[series])
        )
    except Exception as e:
        logger.debug(f"Error sending metric to Datadog: {e}")
