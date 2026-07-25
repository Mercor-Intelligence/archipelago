from loguru import logger


async def list_items() -> dict:
    """List items tool - returns a list of items."""
    logger.info("Listing items")

    return {
        "items": [
            {"id": 1, "name": "Item 1", "description": "First item"},
            {"id": 2, "name": "Item 2", "description": "Second item"},
            {"id": 3, "name": "Item 3", "description": "Third item"},
        ]
    }


async def get_item(item_id: int) -> dict:
    """Get a specific item by ID."""
    logger.info(f"Getting item: {item_id}")

    # Mock items database
    items = {
        1: {"id": 1, "name": "Item 1", "description": "First item", "price": 10.99},
        2: {"id": 2, "name": "Item 2", "description": "Second item", "price": 20.99},
        3: {"id": 3, "name": "Item 3", "description": "Third item", "price": 30.99},
    }

    item = items.get(item_id)
    if not item:
        return {"error": {"code": 404, "message": f"Item {item_id} not found"}}

    return {"item": item}
