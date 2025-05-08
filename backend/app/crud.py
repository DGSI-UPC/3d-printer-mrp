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

async def get_items(collection_name: str, query: Optional[Dict[str, Any]] = None, limit: int = 100, sort_field: Optional[str] = None, sort_order: int = 1) -> List[dict]:
    collection = await get_collection(collection_name)
    cursor = collection.find(query or {})
    if sort_field:
        cursor = cursor.sort(sort_field, sort_order)
    cursor = cursor.limit(limit)
    items = await cursor.to_list(length=limit)
    for item in items:
        if '_id' in item and not isinstance(item['_id'], str):
            item['_id'] = str(item['_id'])
    return items

async def get_all_items(collection_name: str, query: Optional[Dict[str, Any]] = None, sort_field: Optional[str] = None, sort_order: int = 1) -> List[dict]:
    """Gets all items matching the query from a collection."""
    collection = await get_collection(collection_name)
    cursor = collection.find(query or {})
    if sort_field:
        cursor = cursor.sort(sort_field, sort_order)
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

async def get_simulation_state() -> Optional[dict]: # Return dict to handle missing fields gracefully before Pydantic
    state_dict = await get_item_by_id(COLLECTIONS["simulation_state"], "singleton_state")
    return state_dict

async def save_simulation_state(state: SimulationState) -> SimulationState:
    collection = await get_collection(COLLECTIONS["simulation_state"])
    state_dict = state.model_dump()
    await collection.update_one(
        {"id": "singleton_state"},
        {"$set": state_dict},
        upsert=True
    )
    updated_state_dict = await get_simulation_state()
    updated_state_dict.setdefault('committed_inventory', {}) # Ensure on refetch
    return SimulationState(**updated_state_dict)

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

async def import_data_to_collection(collection_name: str, data: List[dict]):
    """Imports data into a collection. Clears existing data before import."""
    if not data and collection_name != COLLECTIONS["production_orders"]: # Allow empty import for non-essential data if needed
        # However, for core data like materials, products, if data is empty, it means they are being cleared.
        # Production orders can be empty.
        pass # Do not insert if data is empty for some collections

    collection = await get_collection(collection_name)
    await collection.delete_many({}) # Clear existing data
    count = 0
    if data: # Only insert if there's data to insert
        try:
            result = await collection.insert_many(data, ordered=False) # ordered=False can speed up, but stops on first error.
            count = len(result.inserted_ids)
        except Exception as e:
            logger.error(f"Error inserting data into {collection_name}: {e}")
            # Optionally, handle partial inserts or re-raise
            raise
    logger.info(f"Imported {count} documents into collection '{collection_name}'.")
    return count