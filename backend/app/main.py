from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger
from typing import List, Dict, Optional, Tuple

from . import crud, database, utils
from .models import (
    Material, Product, Provider, ProductionOrder, PurchaseOrder, SimulationEvent,
    SimulationState, InitialConditions, ProductionStartRequest, PurchaseOrderRequest,
    StatusResponse, SimulationStatus, DataExport
)
from .simulation import FactorySimulation


# --- Global variable for simulation ---
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
    state_dict = await crud.get_simulation_state() # Fetches as dict
    
    # Ensure state_dict is not None and 'committed_inventory' exists before creating SimulationState
    if state_dict:
        state_dict.setdefault('committed_inventory', {}) # Ensure field exists for older states
        state = SimulationState(**state_dict)
    else:
        state = None

    config = await crud.get_config("random_order_config", {})
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
        if not state:
             default_state_data = SimulationState(
                 storage_capacity=0, 
                 daily_production_capacity=0, 
                 is_initialized=False,
                 committed_inventory={} # Ensure this is set for new default states
            ).model_dump()
             # SimulationState model already has default for committed_inventory if not provided
             # But explicit here ensures it's in the dict before saving if we were creating a raw dict
             await crud.save_simulation_state(SimulationState(**default_state_data))
        current_simulation = None

app = FastAPI(
    title="MRP Factory Simulation API",
    description="API for managing and simulating a 3D printer factory MRP.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_sim() -> FactorySimulation:
    if current_simulation is None or not current_simulation.state.is_initialized:
        raise HTTPException(status_code=409, detail="Simulation not initialized. Please POST /simulation/initialize first.")
    return current_simulation

# --- API Endpoints ---

# --- Simulation Management ---
@app.post("/simulation/initialize", response_model=SimulationState, status_code=201)
async def initialize_simulation(initial_conditions: InitialConditions):
    global current_simulation
    logger.info("Received request to initialize simulation.")
    await database.clear_database()

    for material in initial_conditions.materials:
        await crud.create_item(crud.COLLECTIONS["materials"], material.model_dump())
    for product in initial_conditions.products:
        await crud.create_item(crud.COLLECTIONS["products"], product.model_dump())
    for provider in initial_conditions.providers:
        await crud.create_item(crud.COLLECTIONS["providers"], provider.model_dump())
    await crud.save_config("random_order_config", initial_conditions.random_order_config)

    initial_state = SimulationState(
        current_day=0,
        inventory=initial_conditions.initial_inventory,
        committed_inventory={}, # Initialize as empty
        storage_capacity=initial_conditions.storage_capacity,
        daily_production_capacity=initial_conditions.daily_production_capacity,
        is_initialized=True
    )
    saved_state = await crud.save_simulation_state(initial_state)

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
    sim = get_sim()
    try:
        new_state = await sim.run_day()
        return new_state
    except Exception as e:
        logger.exception("Error during simulation day advance:")
        raise HTTPException(status_code=500, detail=f"Simulation error: {str(e)}")

@app.get("/simulation/status", response_model=SimulationStatus)
async def get_simulation_status_api(): # Renamed to avoid conflict with imported SimulationStatus model
    try:
        sim = get_sim()
        state = sim.state

        pending_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Pending"})
        accepted_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Accepted"}) # New
        in_progress_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress"})
        
        pending_purch = len(state.pending_purchase_orders)
        total_units = sim.get_total_inventory_units() # Physical inventory for storage calculation
        utilization = (total_units / state.storage_capacity * 100) if state.storage_capacity > 0 else 0

        return SimulationStatus(
            current_day=state.current_day,
            total_inventory_units=total_units,
            storage_capacity=state.storage_capacity,
            storage_utilization=round(utilization, 2),
            pending_production_orders=len(pending_prod),
            accepted_production_orders=len(accepted_prod), # New
            in_progress_production_orders=len(in_progress_prod),
            pending_purchase_orders=pending_purch,
        )
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
         logger.exception("Error getting simulation status:")
         raise HTTPException(status_code=500, detail="Failed to retrieve simulation status.")

@app.get("/simulation/state", response_model=SimulationState)
async def get_full_simulation_state():
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
async def list_production_orders(status: Optional[str] = Query(None, description="Filter by status (e.g., Pending, Accepted, In Progress)")):
    query = {"status": status} if status else {}
    # Ensure 'required_materials' and 'committed_materials' are correctly fetched as dicts
    items_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"], query=query)
    
    orders = []
    for item_data in items_raw:
        # Ensure default empty dicts for material fields if they are None or missing
        item_data.setdefault('required_materials', {})
        item_data.setdefault('committed_materials', {})
        orders.append(ProductionOrder(**item_data))
        
    orders.sort(key=lambda x: x.created_at, reverse=True) # Example sort
    return orders

@app.get("/production/orders/{order_id}", response_model=ProductionOrder)
async def get_production_order(order_id: str):
    item = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
    if not item:
        raise HTTPException(status_code=404, detail="Production order not found")
    item.setdefault('required_materials', {})
    item.setdefault('committed_materials', {})
    return ProductionOrder(**item)

@app.post("/production/orders/{order_id}/accept", response_model=StatusResponse)
async def accept_production_order_api(order_id: str): # Renamed
    sim = get_sim()
    try:
        success, message = await sim.accept_production_order(order_id)
        if success:
            return StatusResponse(message=message)
        else:
            raise HTTPException(status_code=400, detail=message)
    except Exception as e:
        logger.exception(f"Error accepting production order {order_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to accept order: {str(e)}")

@app.post("/production/orders/{order_id}/order_missing_materials", response_model=Dict[str, str])
async def order_missing_materials_for_production_order_api(order_id: str): # Renamed
    sim = get_sim()
    try:
        results = await sim.place_purchase_order_for_shortages(order_id)
        if "error" in results:
             raise HTTPException(status_code=400, detail=results["error"])
        return results
    except HTTPException as http_exc: # Propagate HTTP exceptions from sim layer if any
        raise http_exc
    except Exception as e:
        logger.exception(f"Error ordering missing materials for production order {order_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to order materials: {str(e)}")

@app.post("/production/orders/start", response_model=Dict[str, str])
async def start_production_orders(request: ProductionStartRequest):
    sim = get_sim()
    try:
        # This method in simulation.py now expects orders to be in "Accepted" state
        results = await sim.start_production(request.order_ids)
        return results
    except Exception as e:
        logger.exception(f"Error starting production for orders {request.order_ids}:")
        raise HTTPException(status_code=500, detail=f"Failed to start production: {str(e)}")

# --- Purchase Order Endpoints ---
@app.post("/purchase/orders", response_model=PurchaseOrder, status_code=201)
async def create_purchase_order(request: PurchaseOrderRequest):
    sim = get_sim()
    try:
        po = await sim.place_purchase_order(
            material_id=request.material_id,
            provider_id=request.provider_id,
            quantity=request.quantity
        )
        await crud.save_simulation_state(sim.state) # Save state after PO placed successfully
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
@app.get("/inventory", response_model=Dict[str, Dict[str, int]])
async def get_inventory():
    """Gets the current physical and committed inventory levels."""
    sim = get_sim()
    return {
        "physical": sim.state.inventory,
        "committed": sim.state.committed_inventory
    }

# --- Event Log Endpoint ---
@app.get("/events", response_model=List[SimulationEvent])
async def list_events(limit: int = Query(100, description="Maximum number of events to return")):
    items = await crud.get_items(crud.COLLECTIONS["events"], limit=limit, sort_field="timestamp", sort_order=-1)
    return [SimulationEvent(**item) for item in items]


# --- Data Import/Export ---
@app.get("/data/export", response_model=DataExport)
async def export_data():
    try:
        sim_state_raw = await crud.get_item_by_id(crud.COLLECTIONS["simulation_state"], "singleton_state")
        sim_state_dict = sim_state_raw if sim_state_raw else {}
        sim_state_dict.setdefault('committed_inventory', {}) # Ensure field exists
        sim_state = SimulationState(**sim_state_dict) if sim_state_raw else SimulationState(storage_capacity=0, daily_production_capacity=0, is_initialized=False, committed_inventory={})


        events_raw = await crud.get_all_items(crud.COLLECTIONS["events"])
        prod_orders_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"])
        purch_orders_raw = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"])
        products_raw = await crud.get_all_items(crud.COLLECTIONS["products"])
        materials_raw = await crud.get_all_items(crud.COLLECTIONS["materials"])
        providers_raw = await crud.get_all_items(crud.COLLECTIONS["providers"])

        # Ensure material dicts in prod_orders are correctly formatted
        valid_prod_orders = []
        for p_order in prod_orders_raw:
            p_order.setdefault('required_materials', {})
            p_order.setdefault('committed_materials', {})
            valid_prod_orders.append(ProductionOrder(**p_order))


        return DataExport(
            simulation_state=sim_state,
            events=[SimulationEvent(**e) for e in events_raw],
            production_orders=valid_prod_orders,
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
    global current_simulation
    logger.warning("Received request to import data. This will overwrite existing simulation state.")
    try:
        await database.clear_database()

        await crud.import_data_to_collection(crud.COLLECTIONS["materials"], [m.model_dump() for m in data.materials])
        await crud.import_data_to_collection(crud.COLLECTIONS["products"], [p.model_dump() for p in data.products])
        await crud.import_data_to_collection(crud.COLLECTIONS["providers"], [p.model_dump() for p in data.providers])
        
        # Ensure 'required_materials' and 'committed_materials' are dicts for production orders on import
        production_orders_to_import = []
        for p_order_model in data.production_orders:
            p_order_dict = p_order_model.model_dump()
            p_order_dict.setdefault('required_materials', {})
            p_order_dict.setdefault('committed_materials', {})
            production_orders_to_import.append(p_order_dict)
        await crud.import_data_to_collection(crud.COLLECTIONS["production_orders"], production_orders_to_import)

        await crud.import_data_to_collection(crud.COLLECTIONS["purchase_orders"], [po.model_dump() for po in data.purchase_orders])
        await crud.import_data_to_collection(crud.COLLECTIONS["events"], [e.model_dump() for e in data.events])

        # Ensure simulation_state has committed_inventory upon import
        imported_sim_state_dict = data.simulation_state.model_dump()
        imported_sim_state_dict.setdefault('committed_inventory', {})
        final_sim_state = SimulationState(**imported_sim_state_dict)
        await crud.save_simulation_state(final_sim_state)

        config = await crud.get_config("random_order_config", {})
        current_simulation = FactorySimulation(
             initial_state=final_sim_state,
             products=data.products,
             materials=data.materials,
             providers=data.providers,
             config=config
         )

        logger.info("Data import completed successfully.")
        return StatusResponse(message="Data imported successfully.")
    except Exception as e:
        logger.exception("Error during data import:")
        await database.clear_database() 
        current_simulation = None 
        raise HTTPException(status_code=500, detail=f"Data import failed: {str(e)}")

@app.get("/", response_model=StatusResponse)
async def read_root():
    return StatusResponse(message="MRP Factory Simulation API is running.")