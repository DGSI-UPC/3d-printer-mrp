from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger
from typing import List, Dict, Optional, Tuple

from . import crud, database, utils
from .models import (
    Material, Product, Provider, ProductionOrder, PurchaseOrder, SimulationEvent,
    SimulationState, InitialConditions, ProductionStartRequest, PurchaseOrderRequest,
    StatusResponse, SimulationStatus, DataExport,
    InventoryStatusResponse, InventoryDetail, ItemForecastResponse # Added ItemForecastResponse
)
from .simulation import FactorySimulation


current_simulation: Optional[FactorySimulation] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect_to_mongo()
    await load_simulation_state()
    yield
    await database.close_mongo_connection()

async def load_simulation_state():
    global current_simulation
    logger.info("Attempting to load simulation state...")
    state_dict = await crud.get_simulation_state()

    if state_dict:
        state_dict.setdefault('committed_inventory', {})
        state = SimulationState(**state_dict)
    else:
        state = None

    config = await crud.get_config("random_order_config", {})
    products_list = await crud.get_all_items(crud.COLLECTIONS["products"])
    materials_list = await crud.get_all_items(crud.COLLECTIONS["materials"])
    providers_list = await crud.get_all_items(crud.COLLECTIONS["providers"])

    products = [Product(**p) for p in products_list]
    materials = [Material(**m) for m in materials_list]
    providers = [Provider(**p) for p in providers_list]

    if state and state.is_initialized and products and materials and providers:
        logger.info(f"Found existing simulation state for Day {state.current_day}. Loading...")
        current_simulation = FactorySimulation(state, products, materials, providers, config)
    else:
        logger.info("No valid simulation state found or base data missing. Simulation needs initialization.")
        if not state:
             default_state = SimulationState(
                 storage_capacity=0,
                 daily_production_capacity=0,
                 is_initialized=False,
                 committed_inventory={}
            )
             await crud.save_simulation_state(default_state)
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

@app.post("/simulation/initialize", response_model=SimulationState, status_code=201)
async def initialize_simulation(initial_conditions: InitialConditions):
    global current_simulation
    logger.info("Received request to initialize simulation.")
    await database.clear_database()

    for material_item in initial_conditions.materials: # Renamed to avoid conflict
        await crud.create_item(crud.COLLECTIONS["materials"], material_item.model_dump())
    for product_item in initial_conditions.products:  # Renamed
        await crud.create_item(crud.COLLECTIONS["products"], product_item.model_dump())
    for provider_item in initial_conditions.providers: # Renamed
        await crud.create_item(crud.COLLECTIONS["providers"], provider_item.model_dump())
    await crud.save_config("random_order_config", initial_conditions.random_order_config)

    initial_state = SimulationState(
        current_day=0,
        inventory=initial_conditions.initial_inventory,
        committed_inventory={},
        storage_capacity=initial_conditions.storage_capacity,
        daily_production_capacity=initial_conditions.daily_production_capacity,
        is_initialized=True
    )
    saved_state = await crud.save_simulation_state(initial_state)

    materials_models = initial_conditions.materials
    products_models = initial_conditions.products
    providers_models = initial_conditions.providers

    current_simulation = FactorySimulation(
        initial_state=saved_state,
        products=products_models,
        materials=materials_models,
        providers=providers_models,
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
async def get_simulation_status_api():
    try:
        sim = get_sim()
        state = sim.state

        pending_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Pending"})
        accepted_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Accepted"})
        in_progress_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress"})

        pending_purch_count = len(state.pending_purchase_orders) # Count of PO documents
        total_units = sim.get_total_inventory_units()
        utilization = (total_units / state.storage_capacity * 100) if state.storage_capacity > 0 else 0

        return SimulationStatus(
            current_day=state.current_day,
            total_inventory_units=total_units,
            storage_capacity=state.storage_capacity,
            storage_utilization=round(utilization, 2),
            pending_production_orders=len(pending_prod),
            accepted_production_orders=len(accepted_prod),
            in_progress_production_orders=len(in_progress_prod),
            pending_purchase_orders=pending_purch_count,
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

@app.get("/materials", response_model=List[Material])
async def list_materials_api(): # Renamed
    items = await crud.get_all_items(crud.COLLECTIONS["materials"])
    return [Material(**item) for item in items]

@app.get("/products", response_model=List[Product])
async def list_products_api(): # Renamed
    items = await crud.get_all_items(crud.COLLECTIONS["products"])
    return [Product(**item) for item in items]

@app.get("/providers", response_model=List[Provider])
async def list_providers_api(): # Renamed
    items = await crud.get_all_items(crud.COLLECTIONS["providers"])
    return [Provider(**item) for item in items]

@app.get("/production/orders", response_model=List[ProductionOrder])
async def list_production_orders(status: Optional[str] = Query(None, description="Filter by status (e.g., Pending, Accepted, In Progress, Fulfilled)")):
    query = {"status": status} if status else {}
    items_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"], query=query)

    orders = []
    for item_data in items_raw:
        item_data.setdefault('required_materials', {})
        item_data.setdefault('committed_materials', {})
        orders.append(ProductionOrder(**item_data))

    orders.sort(key=lambda x: x.created_at, reverse=True)
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
async def accept_production_order_api(order_id: str):
    sim = get_sim()
    try:
        success, message = await sim.accept_production_order(order_id)
        if success:
            return StatusResponse(message=message)
        else:
            # Distinguish between client error (400/409) and server error (500)
            if "cannot be accepted" in message.lower() or "insufficient" in message.lower(): # Typical for material shortage
                 raise HTTPException(status_code=409, detail=message) # Conflict, cannot accept
            raise HTTPException(status_code=400, detail=message) # Generic bad request
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.exception(f"Error accepting production order {order_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to accept order: {str(e)}")

@app.post("/production/orders/{order_id}/fulfill_accepted_from_stock", response_model=StatusResponse)
async def fulfill_accepted_order_from_stock_api(order_id: str):
    sim = get_sim()
    try:
        success, message = await sim.fulfill_accepted_order_from_stock(order_id)
        if success:
            return StatusResponse(message=message)
        else:
            if "insufficient finished product" in message.lower():
                 raise HTTPException(status_code=409, detail=message)
            raise HTTPException(status_code=400, detail=message)
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.exception(f"Error fulfilling accepted order {order_id} from stock:")
        raise HTTPException(status_code=500, detail=f"Failed to fulfill order from stock: {str(e)}")

@app.post("/production/orders/{order_id}/order_missing_materials", response_model=Dict[str, str])
async def order_missing_materials_for_production_order_api(order_id: str):
    sim = get_sim()
    try:
        results = await sim.place_purchase_order_for_shortages(order_id)
        if "error" in results and len(results) == 1: # Check if only error key exists
             raise HTTPException(status_code=400, detail=results["error"])
        return results
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.exception(f"Error ordering missing materials for production order {order_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to order materials: {str(e)}")

@app.post("/production/orders/start", response_model=Dict[str, str])
async def start_production_orders(request: ProductionStartRequest):
    sim = get_sim()
    try:
        results = await sim.start_production(request.order_ids)
        # Check results for any individual failures to potentially return a mixed status code
        # For simplicity, if any part of the request leads to an exception not caught by sim.start_production,
        # it will fall to the generic 500. sim.start_production returns dict of messages.
        # If all messages indicate issues, it's still a 200 from API but UI shows errors.
        return results
    except Exception as e:
        logger.exception(f"Error starting production for orders {request.order_ids}:")
        raise HTTPException(status_code=500, detail=f"Failed to start production: {str(e)}")

@app.post("/purchase/orders", response_model=PurchaseOrder, status_code=201)
async def create_purchase_order_api(request: PurchaseOrderRequest): # Renamed
    sim = get_sim()
    try:
        po = await sim.place_purchase_order(
            material_id=request.material_id,
            provider_id=request.provider_id,
            quantity=request.quantity
        )
        await crud.save_simulation_state(sim.state)
        return po
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Error creating purchase order:")
        raise HTTPException(status_code=500, detail=f"Failed to create purchase order: {str(e)}")

@app.get("/purchase/orders", response_model=List[PurchaseOrder])
async def list_purchase_orders_api(status: Optional[str] = Query(None, description="Filter by status (e.g., Ordered, Arrived)")): #Renamed
    query = {"status": status} if status else {}
    items = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"], query=query)
    items.sort(key=lambda x: x.get('order_date'), reverse=True)
    return [PurchaseOrder(**item) for item in items]

@app.get("/inventory", response_model=InventoryStatusResponse)
async def get_inventory_api(): # Renamed
    sim = get_sim()
    inventory_items: Dict[str, InventoryDetail] = {}

    on_order_quantities: Dict[str, int] = {}
    pending_pos_raw = await crud.get_items(crud.COLLECTIONS["purchase_orders"], {"status": "Ordered"}, limit=None) # Get all pending
    for po_dict in pending_pos_raw:
        po = PurchaseOrder(**po_dict)
        on_order_quantities[po.material_id] = on_order_quantities.get(po.material_id, 0) + po.quantity_ordered

    all_item_ids = set(sim.state.inventory.keys()) | set(sim.state.committed_inventory.keys()) | set(on_order_quantities.keys())

    for mat_id, mat_obj in sim.materials.items(): all_item_ids.add(mat_id)
    for prod_id, prod_obj in sim.products.items(): all_item_ids.add(prod_id)

    for item_id in sorted(list(all_item_ids)):
        item_name = "Unknown Item"; item_type = "Unknown"
        if item_id in sim.materials: item_name = sim.materials[item_id].name; item_type = "Material"
        elif item_id in sim.products: item_name = sim.products[item_id].name; item_type = "Product"

        physical = sim.state.inventory.get(item_id, 0)
        committed = sim.state.committed_inventory.get(item_id, 0) # This is total committed
        on_order = on_order_quantities.get(item_id, 0) if item_type == "Material" else 0
        projected_available = physical + on_order - committed # For materials, this includes on_order. For products, physical - committed.

        inventory_items[item_id] = InventoryDetail(
            item_id=item_id, name=item_name, type=item_type,
            physical=physical, committed=committed, on_order=on_order,
            projected_available=projected_available
        )
    return InventoryStatusResponse(items=inventory_items)

@app.get("/inventory/forecast/{item_id}", response_model=ItemForecastResponse)
async def get_item_forecast_api(item_id: str, days: int = Query(7, ge=1, le=90)):
    sim = get_sim()
    try:
        # Check if item_id is valid first to provide a clear 404 if not found
        if not (item_id in sim.materials or item_id in sim.products):
            raise HTTPException(status_code=404, detail=f"Item with ID '{item_id}' not found as a material or product.")
        
        forecast_data = await sim.get_item_forecast(item_id, days)
        return forecast_data
    except ValueError as ve: # Catch specific errors from simulation logic if needed
        raise HTTPException(status_code=400, detail=str(ve))
    except HTTPException as http_exc:
        raise http_exc # Re-raise existing HTTPExceptions
    except Exception as e:
        logger.exception(f"Error generating forecast for item {item_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to generate forecast: {str(e)}")


@app.get("/events", response_model=List[SimulationEvent])
async def list_events(limit: int = Query(100, description="Maximum number of events to return")):
    items = await crud.get_items(crud.COLLECTIONS["events"], limit=limit, sort_field="timestamp", sort_order=-1)
    return [SimulationEvent(**item) for item in items]

@app.get("/data/export", response_model=DataExport)
async def export_data():
    try:
        sim_state_raw = await crud.get_item_by_id(crud.COLLECTIONS["simulation_state"], "singleton_state")
        sim_state_dict = sim_state_raw if sim_state_raw else {}
        sim_state_dict.setdefault('committed_inventory', {})
        sim_state = SimulationState(**sim_state_dict) if sim_state_raw else SimulationState(storage_capacity=0, daily_production_capacity=0, is_initialized=False, committed_inventory={})

        events_raw = await crud.get_all_items(crud.COLLECTIONS["events"])
        prod_orders_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"])
        purch_orders_raw = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"])
        products_raw = await crud.get_all_items(crud.COLLECTIONS["products"])
        materials_raw = await crud.get_all_items(crud.COLLECTIONS["materials"])
        providers_raw = await crud.get_all_items(crud.COLLECTIONS["providers"])

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

        imported_materials = [m.model_dump() for m in data.materials]
        imported_products = [p.model_dump() for p in data.products]
        imported_providers = [p.model_dump() for p in data.providers]

        await crud.import_data_to_collection(crud.COLLECTIONS["materials"], imported_materials)
        await crud.import_data_to_collection(crud.COLLECTIONS["products"], imported_products)
        await crud.import_data_to_collection(crud.COLLECTIONS["providers"], imported_providers)

        production_orders_to_import = []
        for p_order_model in data.production_orders:
            p_order_dict = p_order_model.model_dump()
            p_order_dict.setdefault('required_materials', {})
            p_order_dict.setdefault('committed_materials', {})
            production_orders_to_import.append(p_order_dict)
        await crud.import_data_to_collection(crud.COLLECTIONS["production_orders"], production_orders_to_import)

        await crud.import_data_to_collection(crud.COLLECTIONS["purchase_orders"], [po.model_dump() for po in data.purchase_orders])
        await crud.import_data_to_collection(crud.COLLECTIONS["events"], [e.model_dump() for e in data.events])

        imported_sim_state_dict = data.simulation_state.model_dump()
        imported_sim_state_dict.setdefault('committed_inventory', {})
        final_sim_state = SimulationState(**imported_sim_state_dict)
        await crud.save_simulation_state(final_sim_state)

        # Re-fetch config or use one from import if available
        config = await crud.get_config("random_order_config", {}) # Or data.config if part of DataExport
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