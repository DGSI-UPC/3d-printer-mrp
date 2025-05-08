from .database import get_collection, COLLECTIONS
from .models import (
    Material, Product, Provider, ProductionOrder, PurchaseOrder,
    SimulationEvent, SimulationState
)
from typing import List, Optional, Dict, Any
from loguru import logger
from bson import ObjectId # Import ObjectId if you need to query by MongoDB's default _id

# Generic CRUD functions (can be specialized if needed)

async def create_item(collection_name: str, item_data: dict) -> dict:
    collection = await get_collection(collection_name)
    result = await collection.insert_one(item_data)
    created_item = await collection.find_one({"_id": result.inserted_id})
    # Convert ObjectId to str for consistency if needed, though motor might handle it
    if created_item and '_id' in created_item and not isinstance(created_item['_id'], str):
         created_item['_id'] = str(created_item['_id'])
    return created_item

async def get_item_by_id(collection_name: str, item_id: str, id_field: str = "id") -> Optional[dict]:
    collection = await get_collection(collection_name)
    # Use the custom 'id' field for lookup, not MongoDB's '_id' unless specified
    item = await collection.find_one({id_field: item_id})
    if item and '_id' in item and not isinstance(item['_id'], str):
         item['_id'] = str(item['_id'])
    return item

async def get_items(collection_name: str, query: Optional[Dict[str, Any]] = None, limit: int = 100) -> List[dict]:
    collection = await get_collection(collection_name)
    cursor = collection.find(query or {}).limit(limit)
    items = await cursor.to_list(length=limit)
    for item in items:
        if '_id' in item and not isinstance(item['_id'], str):
            item['_id'] = str(item['_id'])
    return items

async def get_all_items(collection_name: str, query: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Gets all items matching the query from a collection."""
    collection = await get_collection(collection_name)
    cursor = collection.find(query or {})
    items = await cursor.to_list(length=None) # Use None to get all documents
    for item in items:
        if '_id' in item and not isinstance(item['_id'], str):
            item['_id'] = str(item['_id'])
    return items


async def update_item(collection_name: str, item_id: str, update_data: dict, id_field: str = "id") -> Optional[dict]:
    collection = await get_collection(collection_name)
    # Ensure we don't try to update the immutable 'id' field if it's part of update_data
    update_data.pop(id_field, None)
    update_data.pop('_id', None) # Also remove mongo's _id if present

    if not update_data:
        logger.warning(f"No update data provided for item {item_id} in {collection_name}")
        # Optionally return the existing item or None/error
        return await get_item_by_id(collection_name, item_id, id_field)


    result = await collection.update_one({id_field: item_id}, {"$set": update_data})
    if result.matched_count:
        updated_item = await collection.find_one({id_field: item_id})
        if updated_item and '_id' in updated_item and not isinstance(updated_item['_id'], str):
            updated_item['_id'] = str(updated_item['_id'])
        return updated_item
    return None

async def delete_item(collection_name: str, item_id: str, id_field: str = "id") -> bool:
    collection = await get_collection(collection_name)
    result = await collection.delete_one({id_field: item_id})
    return result.deleted_count > 0

# --- Specific CRUD operations ---

async def get_simulation_state() -> Optional[SimulationState]:
    state_dict = await get_item_by_id(COLLECTIONS["simulation_state"], "singleton_state")
    return SimulationState(**state_dict) if state_dict else None

async def save_simulation_state(state: SimulationState) -> SimulationState:
    collection = await get_collection(COLLECTIONS["simulation_state"])
    state_dict = state.model_dump()
    # Use upsert=True to create the document if it doesn't exist
    await collection.update_one(
        {"id": "singleton_state"},
        {"$set": state_dict},
        upsert=True
    )
    # Fetch the state again to ensure consistency (optional but good practice)
    updated_state = await get_simulation_state()
    return updated_state

async def log_event(event: SimulationEvent) -> SimulationEvent:
    event_dict = event.model_dump()
    created_event_dict = await create_item(COLLECTIONS["events"], event_dict)
    return SimulationEvent(**created_event_dict)

async def get_config(config_key: str, default: Any = None) -> Any:
    config_doc = await get_item_by_id(COLLECTIONS["config"], config_key)
    return config_doc['value'] if config_doc else default

async def save_config(config_key: str, value: Any):
    collection = await get_collection(COLLECTIONS["config"])
    await collection.update_one(
        {"id": config_key},
        {"$set": {"id": config_key, "value": value}},
        upsert=True
    )