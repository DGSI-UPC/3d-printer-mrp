from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger
from typing import List, Dict, Optional

from . import crud, database, utils
from .models import (
    Material, Product, Provider, ProductionOrder, PurchaseOrder, SimulationEvent,
    SimulationState, InitialConditions, ProductionStartRequest, PurchaseOrderRequest,
    StatusResponse, SimulationStatus, DataExport
)
from .simulation import FactorySimulation


# --- Global variable for simulation ---
# This is okay for a simple example, but for concurrent users or more complex state,
# you might need a more robust way to manage simulation instances.
current_simulation: Optional[FactorySimulation] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await database.connect_to_mongo()
    await load_simulation_state() # Try to load existing state on startup
    yield
    # Shutdown
    await database.close_mongo_connection()

async def load_simulation_state():
    """Loads the simulation state from DB or initializes if not found."""
    global current_simulation
    logger.info("Attempting to load simulation state...")
    state = await crud.get_simulation_state()
    config = await crud.get_config("random_order_config", {}) # Load config too
    products_dict = await crud.get_all_items(crud.COLLECTIONS["products"])
    materials_dict = await crud.get_all_items(crud.COLLECTIONS["materials"])
    providers_dict = await crud.get_all_items(crud.COLLECTIONS["providers"])

    products = [Product(**p) for p in products_dict]
    materials = [Material(**m) for m in materials_dict]
    providers = [Provider(**p) for p in providers_dict]

    if state and state.is_initialized and products and materials and providers:
        logger.info(f"Found existing simulation state for Day {state.current_day}. Loading...")
        current_simulation = FactorySimulation(state, products, materials, providers, config)
    else:
        logger.info("No valid simulation state found or base data missing. Simulation needs initialization.")
        # Create a default, uninitialized state object if none exists
        if not state:
             default_state = SimulationState(storage_capacity=0, daily_production_capacity=0, is_initialized=False)
             await crud.save_simulation_state(default_state)
        current_simulation = None # Mark as not ready

app = FastAPI(
    title="MRP Factory Simulation API",
    description="API for managing and simulating a 3D printer factory MRP.",
    version="1.0.0",
    lifespan=lifespan
)

# --- CORS Middleware ---
# Allows requests from your Streamlit frontend (adjust origins if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all for development, restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helper Function ---
def get_sim() -> FactorySimulation:
    """Gets the current simulation instance, raising an error if not initialized."""
    if current_simulation is None or not current_simulation.state.is_initialized:
        raise HTTPException(status_code=409, detail="Simulation not initialized. Please POST /simulation/initialize first.")
    return current_simulation

# --- API Endpoints ---

# --- Simulation Management ---
@app.post("/simulation/initialize", response_model=SimulationState, status_code=201)
async def initialize_simulation(initial_conditions: InitialConditions):
    """Initializes or re-initializes the simulation with given conditions."""
    global current_simulation
    logger.info("Received request to initialize simulation.")

    # Clear existing data
    await database.clear_database()

    # Save base data (Materials, Products, Providers)
    for material in initial_conditions.materials:
        await crud.create_item(crud.COLLECTIONS["materials"], material.model_dump())
    for product in initial_conditions.products:
        # Pre-calculate required materials for BOM display consistency if needed? Or do it on demand.
        await crud.create_item(crud.COLLECTIONS["products"], product.model_dump())
    for provider in initial_conditions.providers:
        await crud.create_item(crud.COLLECTIONS["providers"], provider.model_dump())

    # Save config
    await crud.save_config("random_order_config", initial_conditions.random_order_config)

    # Create initial simulation state
    initial_state = SimulationState(
        current_day=0,
        inventory=initial_conditions.initial_inventory,
        storage_capacity=initial_conditions.storage_capacity,
        daily_production_capacity=initial_conditions.daily_production_capacity,
        is_initialized=True # Mark as initialized
    )
    saved_state = await crud.save_simulation_state(initial_state)

    # Create the simulation instance
    current_simulation = FactorySimulation(
        initial_state=saved_state,
        products=initial_conditions.products,
        materials=initial_conditions.materials,
        providers=initial_conditions.providers,
        config={"random_order_config": initial_conditions.random_order_config}
    )

    await current_simulation.log_sim_event("simulation_initialized", {"initial_conditions": initial_conditions.model_dump(exclude={'products', 'providers', 'materials'})})
    logger.info("Simulation initialized successfully.")
    return saved_state

@app.post("/simulation/advance_day", response_model=SimulationState)
async def advance_simulation_day():
    """Advances the simulation by one day."""
    sim = get_sim()
    try:
        new_state = await sim.run_day()
        return new_state
    except Exception as e:
        logger.exception("Error during simulation day advance:")
        raise HTTPException(status_code=500, detail=f"Simulation error: {str(e)}")

@app.get("/simulation/status", response_model=SimulationStatus)
async def get_simulation_status():
    """Gets the current high-level status of the simulation."""
    try:
        sim = get_sim()
        state = sim.state

        pending_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Pending"})
        in_progress_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress"})
        # Correctly count pending purchase orders from the state managed by the simulation object
        pending_purch = len(state.pending_purchase_orders)

        total_units = sim.get_total_inventory_units()
        utilization = (total_units / state.storage_capacity * 100) if state.storage_capacity > 0 else 0

        return SimulationStatus(
            current_day=state.current_day,
            total_inventory_units=total_units,
            storage_capacity=state.storage_capacity,
            storage_utilization=round(utilization, 2),
            pending_production_orders=len(pending_prod),
            in_progress_production_orders=len(in_progress_prod),
            pending_purchase_orders=pending_purch,
        )
    except HTTPException as http_exc: # Catch the "not initialized" error specifically
        raise http_exc
    except Exception as e:
         logger.exception("Error getting simulation status:")
         raise HTTPException(status_code=500, detail="Failed to retrieve simulation status.")


@app.get("/simulation/state", response_model=SimulationState)
async def get_full_simulation_state():
    """Gets the detailed current state of the simulation."""
    sim = get_sim()
    return sim.state


# --- Data Definition Endpoints ---
@app.get("/materials", response_model=List[Material])
async def list_materials():
    items = await crud.get_all_items(crud.COLLECTIONS["materials"])
    return [Material(**item) for item in items]

@app.get("/products", response_model=List[Product])
async def list_products():
    items = await crud.get_all_items(crud.COLLECTIONS["products"])
    return [Product(**item) for item in items]

@app.get("/providers", response_model=List[Provider])
async def list_providers():
    items = await crud.get_all_items(crud.COLLECTIONS["providers"])
    return [Provider(**item) for item in items]

# --- Production Order Endpoints ---
@app.get("/production/orders", response_model=List[ProductionOrder])
async def list_production_orders(status: Optional[str] = Query(None, description="Filter by status (e.g., Pending, In Progress)")):
    query = {"status": status} if status else {}
    items = await crud.get_all_items(crud.COLLECTIONS["production_orders"], query=query)
    # Sort by creation date maybe?
    items.sort(key=lambda x: x.get('created_at'), reverse=True)
    return [ProductionOrder(**item) for item in items]

@app.get("/production/orders/{order_id}", response_model=ProductionOrder)
async def get_production_order(order_id: str):
    item = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
    if not item:
        raise HTTPException(status_code=404, detail="Production order not found")
    return ProductionOrder(**item)

@app.post("/production/orders/start", response_model=Dict[str, str])
async def start_production_orders(request: ProductionStartRequest):
    """Attempts to start production for the selected orders."""
    sim = get_sim()
    try:
        results = await sim.start_production(request.order_ids)
        return results
    except Exception as e:
        logger.exception(f"Error starting production for orders {request.order_ids}:")
        raise HTTPException(status_code=500, detail=f"Failed to start production: {str(e)}")


# --- Purchase Order Endpoints ---
@app.post("/purchase/orders", response_model=PurchaseOrder, status_code=201)
async def create_purchase_order(request: PurchaseOrderRequest):
    """Creates a new purchase order."""
    sim = get_sim()
    try:
        po = await sim.place_purchase_order(
            material_id=request.material_id,
            provider_id=request.provider_id,
            quantity=request.quantity
        )
        return po
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Error creating purchase order:")
        raise HTTPException(status_code=500, detail=f"Failed to create purchase order: {str(e)}")


@app.get("/purchase/orders", response_model=List[PurchaseOrder])
async def list_purchase_orders(status: Optional[str] = Query(None, description="Filter by status (e.g., Ordered, Arrived)")):
    query = {"status": status} if status else {}
    items = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"], query=query)
    items.sort(key=lambda x: x.get('order_date'), reverse=True)
    return [PurchaseOrder(**item) for item in items]

# --- Inventory Endpoint ---
@app.get("/inventory", response_model=Dict[str, int])
async def get_inventory():
    """Gets the current inventory levels."""
    sim = get_sim()
    return sim.state.inventory

# --- Event Log Endpoint ---
@app.get("/events", response_model=List[SimulationEvent])
async def list_events(limit: int = Query(100, description="Maximum number of events to return")):
     # Fetch sorted by timestamp descending implicitly via log order usually, but explicit sort is safer
    items = await crud.get_items(crud.COLLECTIONS["events"], limit=limit) # Simplistic pagination
    # Explicit sort if needed: items.sort(key=lambda x: x.get('timestamp'), reverse=True)
    return [SimulationEvent(**item) for item in items]


# --- Data Import/Export ---
@app.get("/data/export", response_model=DataExport)
async def export_data():
    """Exports the current simulation state, events, and base data."""
    try:
        sim_state_raw = await crud.get_item_by_id(crud.COLLECTIONS["simulation_state"], "singleton_state")
        sim_state = SimulationState(**sim_state_raw) if sim_state_raw else SimulationState(storage_capacity=0, daily_production_capacity=0, is_initialized=False) # Default if somehow missing

        events_raw = await crud.get_all_items(crud.COLLECTIONS["events"])
        prod_orders_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"])
        purch_orders_raw = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"])
        products_raw = await crud.get_all_items(crud.COLLECTIONS["products"])
        materials_raw = await crud.get_all_items(crud.COLLECTIONS["materials"])
        providers_raw = await crud.get_all_items(crud.COLLECTIONS["providers"])

        return DataExport(
            simulation_state=sim_state,
            events=[SimulationEvent(**e) for e in events_raw],
            production_orders=[ProductionOrder(**p) for p in prod_orders_raw],
            purchase_orders=[PurchaseOrder(**po) for po in purch_orders_raw],
            products=[Product(**p) for p in products_raw],
            materials=[Material(**m) for m in materials_raw],
            providers=[Provider(**prov) for prov in providers_raw]
        )
    except Exception as e:
        logger.exception("Error during data export:")
        raise HTTPException(status_code=500, detail=f"Data export failed: {str(e)}")

@app.post("/data/import", response_model=StatusResponse)
async def import_data(data: DataExport = Body(...)):
    """Imports data from a JSON structure, overwriting existing data."""
    global current_simulation
    logger.warning("Received request to import data. This will overwrite existing simulation state.")
    try:
        # Clear existing data first
        await database.clear_database()

        # Import base data
        await crud.import_data_to_collection(crud.COLLECTIONS["materials"], [m.model_dump() for m in data.materials])
        await crud.import_data_to_collection(crud.COLLECTIONS["products"], [p.model_dump() for p in data.products])
        await crud.import_data_to_collection(crud.COLLECTIONS["providers"], [p.model_dump() for p in data.providers])

        # Import transactional data
        await crud.import_data_to_collection(crud.COLLECTIONS["production_orders"], [p.model_dump() for p in data.production_orders])
        await crud.import_data_to_collection(crud.COLLECTIONS["purchase_orders"], [po.model_dump() for po in data.purchase_orders])
        await crud.import_data_to_collection(crud.COLLECTIONS["events"], [e.model_dump() for e in data.events])

        # Import and save simulation state
        await crud.save_simulation_state(data.simulation_state)

        # Load the imported state into the running simulation instance
        config = await crud.get_config("random_order_config", {}) # Reload config just in case
        current_simulation = FactorySimulation(
             initial_state=data.simulation_state,
             products=data.products,
             materials=data.materials,
             providers=data.providers,
             config=config
         )

        logger.info("Data import completed successfully.")
        return StatusResponse(message="Data imported successfully.")
    except Exception as e:
        logger.exception("Error during data import:")
        await database.clear_database() # Attempt to clean up if import fails mid-way
        current_simulation = None # Mark simulation as uninitialized
        raise HTTPException(status_code=500, detail=f"Data import failed: {str(e)}")

# --- Root Endpoint ---
@app.get("/", response_model=StatusResponse)
async def read_root():
    return StatusResponse(message="MRP Factory Simulation API is running.")