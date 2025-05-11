from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger
from typing import List, Dict, Optional, Tuple

from . import crud, database, utils
from .models import (
    Material, Product, Provider, ProductionOrder, PurchaseOrder, SimulationEvent,
    SimulationState, InitialConditions, FinancialConfig, # Added FinancialConfig
    ProductionStartRequest, PurchaseOrderRequest,
    StatusResponse, SimulationStatus, DataExport,
    InventoryStatusResponse, InventoryDetail, ItemForecastResponse,
    FinancialPageData # Added for the new finances endpoint
)
from .simulation import FactorySimulation


current_simulation: Optional[FactorySimulation] = None
current_financial_config: Optional[FinancialConfig] = None # Store financial config globally

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect_to_mongo()
    await load_simulation_state_and_config() # Renamed for clarity
    yield
    await database.close_mongo_connection()

async def load_simulation_state_and_config():
    global current_simulation, current_financial_config
    logger.info("Attempting to load simulation state and configurations...")
    
    state_dict = await crud.get_simulation_state()
    if state_dict:
        state_dict.setdefault('committed_inventory', {})
        # Ensure current_balance exists, default if not (for backwards compatibility if loading old state)
        state_dict.setdefault('current_balance', 0.0) 
        state = SimulationState(**state_dict)
    else:
        state = None

    # Load financial config
    financial_config_dict = await crud.get_config("financial_config")
    if financial_config_dict:
        current_financial_config = FinancialConfig(**financial_config_dict)
    else:
        # Create a default financial config if none exists - useful for first run after update
        default_fc = FinancialConfig()
        await crud.save_config("financial_config", default_fc.model_dump())
        current_financial_config = default_fc
        logger.info("No financial_config found, created default.")


    # Load other configs (e.g., random_order_config)
    random_order_cfg_dict = await crud.get_config("random_order_config", {}) # Keep existing config loading

    products_list = await crud.get_all_items(crud.COLLECTIONS["products"])
    materials_list = await crud.get_all_items(crud.COLLECTIONS["materials"])
    providers_list = await crud.get_all_items(crud.COLLECTIONS["providers"])

    products = [Product(**p) for p in products_list]
    materials = [Material(**m) for m in materials_list]
    providers = [Provider(**p) for p in providers_list]

    if state and state.is_initialized and products and materials and providers and current_financial_config:
        logger.info(f"Found existing simulation state for Day {state.current_day}. Loading with financial config...")
        current_simulation = FactorySimulation(
            initial_state=state, 
            products=products, 
            materials=materials, 
            providers=providers, 
            config={"random_order_config": random_order_cfg_dict}, # Pass general config
            financial_config=current_financial_config # Pass financial config
        )
    else:
        logger.info("No valid simulation state found, base data missing, or financial config missing. Simulation needs initialization.")
        if not state: # Create a default shell state if none exists at all
             default_state_shell = SimulationState(
                 storage_capacity=0,
                 daily_production_capacity=0,
                 is_initialized=False,
                 committed_inventory={},
                 current_balance=current_financial_config.initial_balance if current_financial_config else 0.0
            )
             await crud.save_simulation_state(default_state_shell)
        current_simulation = None


app = FastAPI(
    title="MRP Factory Simulation API",
    description="API for managing and simulating a 3D printer factory MRP.",
    version="1.1.0", # Incremented version for financial features
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
    if current_financial_config is None: # Should be loaded by lifespan
        raise HTTPException(status_code=500, detail="Financial configuration not loaded. Critical error.")
    return current_simulation

@app.post("/simulation/initialize", response_model=SimulationState, status_code=201)
async def initialize_simulation_endpoint(initial_conditions: InitialConditions): # Renamed for clarity
    global current_simulation, current_financial_config
    logger.info("Received request to initialize simulation.")
    await database.clear_database() # Clears all collections

    for material_item in initial_conditions.materials:
        await crud.create_item(crud.COLLECTIONS["materials"], material_item.model_dump())
    for product_item in initial_conditions.products:
        await crud.create_item(crud.COLLECTIONS["products"], product_item.model_dump())
    for provider_item in initial_conditions.providers:
        await crud.create_item(crud.COLLECTIONS["providers"], provider_item.model_dump())
    
    # Save configurations
    await crud.save_config("random_order_config", initial_conditions.random_order_config)
    await crud.save_config("financial_config", initial_conditions.financial_config.model_dump())
    current_financial_config = initial_conditions.financial_config # Update global immediately

    initial_state = SimulationState(
        current_day=0,
        inventory=initial_conditions.initial_inventory,
        committed_inventory={},
        storage_capacity=initial_conditions.storage_capacity,
        daily_production_capacity=initial_conditions.daily_production_capacity,
        current_balance=initial_conditions.financial_config.initial_balance, # Set initial balance
        is_initialized=True
    )
    saved_state = await crud.save_simulation_state(initial_state)

    current_simulation = FactorySimulation(
        initial_state=saved_state,
        products=initial_conditions.products,
        materials=initial_conditions.materials,
        providers=initial_conditions.providers,
        config={"random_order_config": initial_conditions.random_order_config},
        financial_config=initial_conditions.financial_config
    )
    await current_simulation.log_sim_event(
        "simulation_initialized", 
        {"initial_conditions": initial_conditions.model_dump(exclude={'products', 'providers', 'materials', 'financial_config.product_prices'})}, # Exclude potentially large dicts from event
        is_financial=True, amount=initial_conditions.financial_config.initial_balance # Log initial balance as a "transaction"
        )
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
        # Consider if this should be a 500 or a more specific client error if applicable
        raise HTTPException(status_code=500, detail=f"Simulation error: {str(e)}")

@app.get("/simulation/status", response_model=SimulationStatus)
async def get_simulation_status_api():
    try:
        sim = get_sim() # Ensures sim is initialized
        state = sim.state

        pending_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Pending"})
        accepted_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Accepted"})
        in_progress_prod = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress"})

        pending_purch_count = len(state.pending_purchase_orders)
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
            current_balance=state.current_balance # Added current balance
        )
    except HTTPException as http_exc: # Re-raise if it's our "sim not initialized"
        raise http_exc
    except Exception as e:
         logger.exception("Error getting simulation status:")
         # Return a different status if sim isn't initialized, rather than 500
         # get_sim() handles the 409 for not initialized.
         raise HTTPException(status_code=500, detail="Failed to retrieve simulation status.")


@app.get("/simulation/state", response_model=SimulationState)
async def get_full_simulation_state():
    sim = get_sim()
    return sim.state

# --- CRUD for Materials, Products, Providers (no changes needed here for finance) ---
@app.get("/materials", response_model=List[Material])
async def list_materials_api():
    items = await crud.get_all_items(crud.COLLECTIONS["materials"])
    return [Material(**item) for item in items]

@app.get("/products", response_model=List[Product])
async def list_products_api():
    items = await crud.get_all_items(crud.COLLECTIONS["products"])
    return [Product(**item) for item in items]

@app.get("/providers", response_model=List[Provider])
async def list_providers_api():
    items = await crud.get_all_items(crud.COLLECTIONS["providers"])
    return [Provider(**item) for item in items]

# --- Production Order Endpoints (no direct changes needed for finance logic itself) ---
@app.get("/production/orders", response_model=List[ProductionOrder])
async def list_production_orders(status: Optional[str] = Query(None, description="Filter by status")):
    query = {"status": status} if status else {}
    items_raw = await crud.get_all_items(crud.COLLECTIONS["production_orders"], query=query)
    orders = []
    for item_data in items_raw:
        item_data.setdefault('required_materials', {})
        item_data.setdefault('committed_materials', {})
        item_data.setdefault('revenue_collected', False) # Ensure field present
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
    item.setdefault('revenue_collected', False)
    return ProductionOrder(**item)

@app.post("/production/orders/{order_id}/accept", response_model=StatusResponse)
async def accept_production_order_api(order_id: str):
    sim = get_sim()
    try:
        success, message = await sim.accept_production_order(order_id)
        if success:
            return StatusResponse(message=message)
        else:
            if "cannot be accepted" in message.lower() or "insufficient" in message.lower():
                 raise HTTPException(status_code=409, detail=message)
            raise HTTPException(status_code=400, detail=message)
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
        if "error" in results and len(results) == 1:
             raise HTTPException(status_code=400, detail=results["error"])
        # Check for insufficient funds messages within results
        for mat_id, msg in results.items():
            if "Insufficient funds" in msg: # This message comes from ValueError in place_purchase_order
                raise HTTPException(status_code=402, detail=f"For material {mat_id}: {msg}") # HTTP 402 Payment Required
        return results
    except ValueError as ve: # Catch direct ValueErrors like insufficient funds if not caught above
        if "Insufficient funds" in str(ve):
            raise HTTPException(status_code=402, detail=str(ve))
        raise HTTPException(status_code=400, detail=str(ve))
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
        return results
    except Exception as e:
        logger.exception(f"Error starting production for orders {request.order_ids}:")
        raise HTTPException(status_code=500, detail=f"Failed to start production: {str(e)}")

# --- Purchase Order Endpoints ---
@app.post("/purchase/orders", response_model=PurchaseOrder, status_code=201)
async def create_purchase_order_api(request: PurchaseOrderRequest):
    sim = get_sim()
    try:
        # Note: place_purchase_order in simulation.py now calculates cost and checks balance.
        # It might raise ValueError for insufficient funds.
        po = await sim.place_purchase_order(
            material_id=request.material_id,
            provider_id=request.provider_id,
            quantity=request.quantity
            # unit_price_override is not passed here; sim will use provider's catalogue price
        )
        # sim.state is saved within place_purchase_order if successful
        return po
    except ValueError as ve: # Catch insufficient funds or other validation errors from sim
        if "Insufficient funds" in str(ve):
            raise HTTPException(status_code=402, detail=str(ve)) # HTTP 402 Payment Required
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Error creating purchase order:")
        raise HTTPException(status_code=500, detail=f"Failed to create purchase order: {str(e)}")

@app.get("/purchase/orders", response_model=List[PurchaseOrder])
async def list_purchase_orders_api(status: Optional[str] = Query(None, description="Filter by status")):
    query = {"status": status} if status else {}
    items = await crud.get_all_items(crud.COLLECTIONS["purchase_orders"], query=query)
    items.sort(key=lambda x: x.get('order_date'), reverse=True)
    # total_cost should now be part of the PO item from DB
    return [PurchaseOrder(**item) for item in items]

# --- Inventory & Forecast Endpoints (no direct finance changes here) ---
@app.get("/inventory", response_model=InventoryStatusResponse)
async def get_inventory_api():
    sim = get_sim()
    # ... (existing inventory logic remains)
    inventory_items: Dict[str, InventoryDetail] = {}
    on_order_quantities: Dict[str, int] = {}
    pending_pos_raw = await crud.get_items(crud.COLLECTIONS["purchase_orders"], {"status": "Ordered"}, limit=None)
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
        committed = sim.state.committed_inventory.get(item_id, 0)
        on_order = on_order_quantities.get(item_id, 0) if item_type == "Material" else 0
        projected_available = physical + on_order - committed
        inventory_items[item_id] = InventoryDetail(
            item_id=item_id, name=item_name, type=item_type,
            physical=physical, committed=committed, on_order=on_order,
            projected_available=projected_available
        )
    return InventoryStatusResponse(items=inventory_items)


@app.get("/inventory/forecast/{item_id}", response_model=ItemForecastResponse)
async def get_item_forecast_api(item_id: str, days: int = Query(7, ge=1, le=90), historical_lookback_days: int = Query(0, ge=0, le=30)):
    sim = get_sim()
    try:
        if not (item_id in sim.materials or item_id in sim.products):
            raise HTTPException(status_code=404, detail=f"Item with ID '{item_id}' not found.")
        forecast_data = await sim.get_item_forecast(item_id, days, historical_lookback_days)
        return forecast_data
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.exception(f"Error generating forecast for item {item_id}:")
        raise HTTPException(status_code=500, detail=f"Failed to generate forecast: {str(e)}")

# --- New Finances Endpoint ---
@app.get("/finances", response_model=FinancialPageData)
async def get_financial_overview_and_forecast(forecast_days: int = Query(7, ge=1, le=30, description="Number of future days to forecast financially.")):
    sim = get_sim()
    try:
        financial_data = await sim.get_financial_data(forecast_days=forecast_days)
        return financial_data
    except Exception as e:
        logger.exception("Error generating financial overview and forecast:")
        raise HTTPException(status_code=500, detail=f"Failed to generate financial data: {str(e)}")


@app.get("/events", response_model=List[SimulationEvent])
async def list_events(limit: int = Query(100, description="Maximum number of events to return")):
    items = await crud.get_items(crud.COLLECTIONS["events"], limit=limit, sort_field="timestamp", sort_order=-1)
    return [SimulationEvent(**item) for item in items]

# --- Data Export/Import ---
@app.get("/data/export", response_model=DataExport)
async def export_data():
    global current_financial_config # Ensure we use the globally loaded one
    try:
        sim = get_sim() # To ensure sim is initialized for state export
        sim_state_raw = await crud.get_item_by_id(crud.COLLECTIONS["simulation_state"], "singleton_state")
        sim_state_dict = sim_state_raw if sim_state_raw else {}
        sim_state_dict.setdefault('committed_inventory', {})
        sim_state_dict.setdefault('current_balance', 0.0) # Ensure balance field
        sim_state = SimulationState(**sim_state_dict) if sim_state_raw else SimulationState(
            storage_capacity=0, daily_production_capacity=0, is_initialized=False, committed_inventory={}, current_balance=0.0
        )

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
            p_order.setdefault('revenue_collected', False)
            valid_prod_orders.append(ProductionOrder(**p_order))
        
        # Fetch financial_config from DB, or use current_financial_config if available
        financial_config_to_export_dict = await crud.get_config("financial_config")
        if not financial_config_to_export_dict and current_financial_config: # Fallback
             financial_config_to_export_dict = current_financial_config.model_dump()
        elif not financial_config_to_export_dict and not current_financial_config: # Critical error if neither
             raise HTTPException(status_code=500, detail="Financial configuration missing for export.")

        financial_config_to_export = FinancialConfig(**financial_config_to_export_dict)


        return DataExport(
            simulation_state=sim_state,
            events=[SimulationEvent(**e) for e in events_raw],
            production_orders=valid_prod_orders,
            purchase_orders=[PurchaseOrder(**po) for po in purch_orders_raw],
            products=[Product(**p) for p in products_raw],
            materials=[Material(**m) for m in materials_raw],
            providers=[Provider(**prov) for prov in providers_raw],
            financial_config=financial_config_to_export # Added financial_config
        )
    except Exception as e:
        logger.exception("Error during data export:")
        raise HTTPException(status_code=500, detail=f"Data export failed: {str(e)}")

@app.post("/data/import", response_model=StatusResponse)
async def import_data_api(data: DataExport = Body(...)): # Renamed
    global current_simulation, current_financial_config
    logger.warning("Received request to import data. This will overwrite existing simulation state and config.")
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
            p_order_dict.setdefault('revenue_collected', False)
            production_orders_to_import.append(p_order_dict)
        await crud.import_data_to_collection(crud.COLLECTIONS["production_orders"], production_orders_to_import)

        await crud.import_data_to_collection(crud.COLLECTIONS["purchase_orders"], [po.model_dump() for po in data.purchase_orders])
        await crud.import_data_to_collection(crud.COLLECTIONS["events"], [e.model_dump() for e in data.events])

        # Save configs from imported data
        if data.financial_config:
            await crud.save_config("financial_config", data.financial_config.model_dump())
            current_financial_config = data.financial_config # Update global
        else: # Should not happen if export is correct
            default_fc = FinancialConfig()
            await crud.save_config("financial_config", default_fc.model_dump())
            current_financial_config = default_fc
            logger.warning("Imported data did not contain financial_config. Using default.")

        # Assuming random_order_config is part of the general config in simulation or needs explicit handling
        # For now, let's assume InitialConditions (and thus DataExport via sim state) contains it or it's re-read.
        # Let's assume the simulation state in the export has a snapshot of the random_order_config used.
        # However, current DataExport doesn't explicitly have random_order_config.
        # The `config` for FactorySimulation currently takes random_order_config.
        # The original `InitialConditions` had it. The `DataExport` should probably include it too for full restoration.
        # For now, re-fetch from DB (or use imported if we add it to DataExport model).
        random_order_cfg_from_db = await crud.get_config("random_order_config", {}) # Default if not found

        imported_sim_state_dict = data.simulation_state.model_dump()
        imported_sim_state_dict.setdefault('committed_inventory', {})
        imported_sim_state_dict.setdefault('current_balance', data.financial_config.initial_balance if data.financial_config else 0.0)
        final_sim_state = SimulationState(**imported_sim_state_dict)
        await crud.save_simulation_state(final_sim_state)

        current_simulation = FactorySimulation(
             initial_state=final_sim_state,
             products=data.products,
             materials=data.materials,
             providers=data.providers,
             config={"random_order_config": random_order_cfg_from_db}, # Use a config dict
             financial_config=current_financial_config # Use the just imported/updated financial_config
         )

        logger.info("Data import completed successfully.")
        return StatusResponse(message="Data imported successfully.")
    except Exception as e:
        logger.exception("Error during data import:")
        await database.clear_database() # Attempt to clean up a failed import
        current_simulation = None
        current_financial_config = None
        raise HTTPException(status_code=500, detail=f"Data import failed: {str(e)}")

@app.get("/", response_model=StatusResponse)
async def read_root():
    return StatusResponse(message="MRP Factory Simulation API is running.")