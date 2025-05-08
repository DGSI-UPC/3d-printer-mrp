import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from dotenv import load_dotenv
from loguru import logger
from typing import List, Optional

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "mrp_simulation_db")

client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None

async def connect_to_mongo():
    global client, db
    logger.info("Connecting to MongoDB...")
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client[DATABASE_NAME]
        # Test connection
        await client.admin.command('ping')
        logger.info("Successfully connected to MongoDB.")
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        # Depending on the application requirements, you might want to exit or handle this differently
        raise

async def close_mongo_connection():
    global client
    if client:
        logger.info("Closing MongoDB connection...")
        client.close()
        logger.info("MongoDB connection closed.")

def get_database() -> AsyncIOMotorDatabase:
    if db is None:
        # This should ideally not happen if connect_to_mongo is called at startup
        raise Exception("Database not initialized. Call connect_to_mongo first.")
    return db

# Define collection names (optional but good practice)
COLLECTIONS = {
    "materials": "materials",
    "products": "products",
    "providers": "providers",
    "production_orders": "production_orders",
    "purchase_orders": "purchase_orders",
    "simulation_state": "simulation_state",
    "events": "events",
    "config": "config" # For storing things like random order params
}

async def get_collection(collection_name: str):
    database = get_database()
    if collection_name not in COLLECTIONS.values():
         # Or handle dynamically if needed, but explicit is safer
        raise ValueError(f"Unknown collection name: {collection_name}")
    return database[collection_name]

async def clear_database():
    """Clears all known collections in the database. Use with caution!"""
    logger.warning("Clearing all collections in the database...")
    database = get_database()
    for collection_name in COLLECTIONS.values():
        await database[collection_name].delete_many({})
    logger.info("Database cleared.")

async def export_collection_to_json(collection_name: str) -> List[dict]:
    """Exports all documents from a collection to a list of dicts."""
    collection = await get_collection(collection_name)
    documents = await collection.find({}).to_list(length=None) # Get all documents
    # Convert ObjectId to str if necessary for JSON serialization
    for doc in documents:
        if '_id' in doc:
            doc['_id'] = str(doc['_id'])
    return documents

async def import_data_to_collection(collection_name: str, data: List[dict]):
    """Imports data into a collection, replacing existing documents if IDs match."""
    if not data:
        return 0
    collection = await get_collection(collection_name)
    # Simple approach: Clear existing and insert new.
    # More sophisticated would be upsert based on a unique business key (like 'id') if '_id' isn't stable/present.
    # Let's assume the 'id' field from our models is the key.
    count = 0
    # Clear collection before import for simplicity in this context
    await collection.delete_many({})
    if data:
        result = await collection.insert_many(data)
        count = len(result.inserted_ids)
    logger.info(f"Imported {count} documents into collection '{collection_name}'.")
    return count