"""TaxJar State Helper - Parses TaxJar data from snapshot.

This helper extracts TaxJar transaction data from the snapshot,
including orders, refunds, nexus regions, rates, and other entities.
"""

import io
import json
import zipfile
from typing import Any

from loguru import logger

from runner.models import AgentTrajectoryOutput


def _load_json_from_zip(
    zip_file: zipfile.ZipFile, filename: str
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Load a JSON file from the snapshot zip.

    Args:
        zip_file: Open ZipFile object
        filename: Name of JSON file to load (can be partial path)

    Returns:
        Parsed JSON data or None if not found
    """
    # Try to find the file in the zip
    matching_files = [
        f for f in zip_file.namelist() if f.endswith(filename) or filename in f
    ]

    if not matching_files:
        logger.debug(f"File not found in snapshot: {filename}")
        return None

    # Use the first match
    file_path = matching_files[0]
    try:
        content = zip_file.read(file_path)
        return json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to parse {file_path}: {e}")
        return None


def _extract_orders_from_json(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract order records from TaxJar JSON format.

    The data format has:
    [
        {"params": ["order_001"], "response": {"order": {...}}},
        ...
    ]

    Args:
        data: Raw JSON data from orders.json

    Returns:
        List of order dictionaries
    """
    orders = []
    for item in data:
        response = item.get("response", {})
        order = response.get("order")
        if order:
            orders.append(order)
    return orders


def _extract_refunds_from_json(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract refund records from TaxJar JSON format.

    Args:
        data: Raw JSON data from refunds.json

    Returns:
        List of refund dictionaries
    """
    refunds = []
    for item in data:
        response = item.get("response", {})
        refund = response.get("refund")
        if refund:
            refunds.append(refund)
    return refunds


def _extract_nexus_regions_from_json(
    data: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract nexus regions from TaxJar JSON format.

    Can handle both list format (with params/response) and direct response format.

    Args:
        data: Raw JSON data from nexus_regions.json

    Returns:
        List of nexus region dictionaries
    """
    # Check if it's direct response format
    if isinstance(data, dict) and "regions" in data:
        return data["regions"]

    # Handle list format with params/response structure
    if isinstance(data, list):
        for item in data:
            response = item.get("response", {})
            regions = response.get("regions")
            if regions:
                return regions

    return []


def _extract_rates_from_json(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract rate records from TaxJar JSON format.

    Args:
        data: Raw JSON data from rates.json

    Returns:
        List of rate dictionaries
    """
    rates = []
    for item in data:
        response = item.get("response", {})
        rate = response.get("rate")
        if rate:
            rates.append(rate)
    return rates


def _extract_summary_rates_from_json(
    data: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract summary rates from TaxJar JSON format.

    Args:
        data: Raw JSON data from summary_rates.json

    Returns:
        List of summary rate dictionaries
    """
    # Check if it's direct response format
    if isinstance(data, dict) and "summary_rates" in data:
        return data["summary_rates"]

    # Handle list format
    if isinstance(data, list):
        for item in data:
            response = item.get("response", {})
            summary_rates = response.get("summary_rates")
            if summary_rates:
                return summary_rates

    return []


async def taxjar_state_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """Parse TaxJar data from final snapshot.

    Extracts TaxJar transaction data including orders, refunds, nexus regions,
    rates, and other relevant entities.

    Args:
        initial_snapshot_bytes: BytesIO of initial snapshot zip
        final_snapshot_bytes: BytesIO of final snapshot zip
        trajectory: Agent trajectory output

    Returns:
        Dictionary with TaxJar state:
        - orders: List of order transactions
        - refunds: List of refund transactions
        - nexus_regions: List of nexus region configurations
        - rates: List of tax rates
        - summary_rates: List of summary rates
    """
    # Reset BytesIO position for reading
    final_snapshot_bytes.seek(0)

    result = {
        "orders": [],
        "refunds": [],
        "nexus_regions": [],
        "rates": [],
        "summary_rates": [],
    }

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
            # List all files for debugging
            all_files = final_zip.namelist()
            logger.debug(f"Snapshot contains {len(all_files)} files")

            # Log TaxJar-related files specifically
            taxjar_files = [
                f
                for f in all_files
                if "taxjar" in f.lower()
                or "orders" in f.lower()
                or "refunds" in f.lower()
            ]
            apps_data_files = [
                f for f in all_files if ".apps_data" in f or "apps_data" in f
            ]
            logger.info(f"TaxJar-related files in snapshot: {taxjar_files}")
            logger.info(f".apps_data files in snapshot: {apps_data_files}")

            # Find and load TaxJar data files
            # Look in APPS_DATA_ROOT paths first, then legacy paths
            # Priority: .apps_data/taxjar/user/ > .apps_data/taxjar/synthetic/ > legacy paths

            # Load orders
            orders_data = (
                _load_json_from_zip(final_zip, ".apps_data/taxjar/user/orders.json")
                or _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/synthetic/orders.json"
                )
                or _load_json_from_zip(final_zip, "data/user/orders.json")
                or _load_json_from_zip(final_zip, "data/synthetic/orders.json")
                or _load_json_from_zip(final_zip, "orders.json")
            )
            if orders_data and isinstance(orders_data, list):
                result["orders"] = _extract_orders_from_json(orders_data)
                logger.info(f"Loaded {len(result['orders'])} orders from snapshot")
            else:
                logger.warning(
                    "No orders.json found in snapshot - TaxJar calculations will return 0"
                )

            # Load refunds
            refunds_data = (
                _load_json_from_zip(final_zip, ".apps_data/taxjar/user/refunds.json")
                or _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/synthetic/refunds.json"
                )
                or _load_json_from_zip(final_zip, "data/user/refunds.json")
                or _load_json_from_zip(final_zip, "data/synthetic/refunds.json")
                or _load_json_from_zip(final_zip, "refunds.json")
            )
            if refunds_data and isinstance(refunds_data, list):
                result["refunds"] = _extract_refunds_from_json(refunds_data)
                logger.info(f"Loaded {len(result['refunds'])} refunds from snapshot")

            # Load nexus regions
            nexus_data = (
                _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/user/nexus_regions.json"
                )
                or _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/synthetic/nexus_regions.json"
                )
                or _load_json_from_zip(final_zip, "data/user/nexus_regions.json")
                or _load_json_from_zip(final_zip, "data/synthetic/nexus_regions.json")
                or _load_json_from_zip(final_zip, "nexus_regions.json")
            )
            if nexus_data:
                result["nexus_regions"] = _extract_nexus_regions_from_json(nexus_data)
                logger.info(
                    f"Loaded {len(result['nexus_regions'])} nexus regions from snapshot"
                )

            # Load rates
            rates_data = (
                _load_json_from_zip(final_zip, ".apps_data/taxjar/user/rates.json")
                or _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/synthetic/rates.json"
                )
                or _load_json_from_zip(final_zip, "data/user/rates.json")
                or _load_json_from_zip(final_zip, "data/synthetic/rates.json")
                or _load_json_from_zip(final_zip, "rates.json")
            )
            if rates_data and isinstance(rates_data, list):
                result["rates"] = _extract_rates_from_json(rates_data)
                logger.info(f"Loaded {len(result['rates'])} rates from snapshot")

            # Load summary rates
            summary_data = (
                _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/user/summary_rates.json"
                )
                or _load_json_from_zip(
                    final_zip, ".apps_data/taxjar/synthetic/summary_rates.json"
                )
                or _load_json_from_zip(final_zip, "data/user/summary_rates.json")
                or _load_json_from_zip(final_zip, "data/synthetic/summary_rates.json")
                or _load_json_from_zip(final_zip, "summary_rates.json")
            )
            if summary_data:
                result["summary_rates"] = _extract_summary_rates_from_json(summary_data)
                logger.info(
                    f"Loaded {len(result['summary_rates'])} summary rates from snapshot"
                )

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip file: {e}")
    except Exception as e:
        logger.error(f"Error parsing TaxJar state from snapshot: {e}")

    # Reset BytesIO position after use for potential reuse
    final_snapshot_bytes.seek(0)

    return result
