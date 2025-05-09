from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any
from datetime import datetime, date

class Material(BaseModel):
    id: str = Field(..., description="Unique material ID")
    name: str
    description: Optional[str] = None

class ProductBOM(BaseModel):
    material_id: str
    quantity: int

class Product(BaseModel):
    id: str = Field(..., description="Unique product ID")
    name: str
    bom: List[ProductBOM] = Field(..., description="Bill of Materials")
    production_time: int = Field(..., description="Time in days to produce one unit")

class ProviderOffering(BaseModel):
    material_id: str
    price_per_unit: float
    offered_unit_size: int = Field(1, description="e.g., 1 for single units, 100 for a pallet")
    lead_time_days: int

class Provider(BaseModel):
    id: str = Field(..., description="Unique provider ID")
    name: str
    catalogue: List[ProviderOffering]

class InventoryItem(BaseModel): # This seems like a DTO, might not be actively used by core logic
    item_id: str
    item_type: str
    quantity: int

class ProductionOrder(BaseModel):
    id: str = Field(..., description="Unique production order ID")
    product_id: str
    quantity: int
    requested_date: datetime
    status: str = Field("Pending", description="Pending, Accepted, In Progress, Completed, Cancelled, Fulfilled") # Added Fulfilled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    required_materials: Dict[str, int] = Field({}, description="Calculated total materials needed for the current quantity")
    committed_materials: Dict[str, int] = Field({}, description="Materials committed from inventory when order is accepted or production starts")

class PurchaseOrder(BaseModel):
    id: str = Field(..., description="Unique purchase order ID")
    material_id: str
    provider_id: str
    quantity_ordered: int
    units_received: int = 0
    order_date: datetime
    expected_arrival_date: datetime
    actual_arrival_date: Optional[datetime] = None
    status: str = Field("Ordered", description="Ordered, Arrived, Partially Arrived, Cancelled")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class SimulationEvent(BaseModel):
    id: str = Field(..., description="Unique event ID")
    day: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: str
    details: Dict[str, Any]

class SimulationState(BaseModel):
    id: str = Field("singleton_state", description="Unique identifier for the simulation state document")
    current_day: int = 0
    inventory: Dict[str, int] = Field({}, description="Maps material_id/product_id to quantity (physical stock)")
    committed_inventory: Dict[str, int] = Field({}, description="Tracks materials committed to accepted/in-progress orders but not yet consumed by production")
    storage_capacity: int
    daily_production_capacity: int
    active_production_orders: List[str] = Field([], description="List of ProductionOrder IDs currently in progress")
    pending_purchase_orders: List[str] = Field([], description="List of PurchaseOrder IDs not yet arrived")
    is_initialized: bool = False

class InitialConditions(BaseModel):
    products: List[Product]
    providers: List[Provider]
    materials: List[Material]
    initial_inventory: Dict[str, int] = Field({}, description="Maps material_id/product_id to quantity")
    storage_capacity: int = 10000
    daily_production_capacity: int = 10
    random_order_config: Dict[str, int] = Field({
        "min_orders_per_day": 0,
        "max_orders_per_day": 3,
        "min_qty_per_order": 1,
        "max_qty_per_order": 5
    })

class ProductionStartRequest(BaseModel):
    order_ids: List[str]

class PurchaseOrderRequest(BaseModel):
    material_id: str
    provider_id: str
    quantity: int

class StatusResponse(BaseModel):
    message: str
    details: Optional[Dict[str, Any]] = None

class SimulationStatus(BaseModel):
    current_day: int
    total_inventory_units: int # Physical units for storage calculation
    storage_capacity: int
    storage_utilization: float
    pending_production_orders: int
    accepted_production_orders: int
    in_progress_production_orders: int
    pending_purchase_orders: int # Count of POs, not material units

class DataExport(BaseModel):
    simulation_state: SimulationState
    events: List[SimulationEvent]
    production_orders: List[ProductionOrder]
    purchase_orders: List[PurchaseOrder]
    products: List[Product]
    providers: List[Provider]
    materials: List[Material]

# New Models for Enhanced Inventory View
class InventoryDetail(BaseModel):
    item_id: str # For reference, though dict key will be item_id
    name: str
    type: str # "Material" or "Product"
    physical: int = 0
    committed: int = 0 # Total committed across all orders (accepted & in-progress)
    on_order: int = 0 # Only applicable to materials
    projected_available: int = 0

class InventoryStatusResponse(BaseModel):
    items: Dict[str, InventoryDetail] = Field({}, description="Maps item_id to its detailed inventory status")

# New Models for Item Forecast
class DailyForecast(BaseModel):
    day_offset: int  # 0 for current day's end / next day's start, 1 for day after, etc.
    date: date
    quantity: float

class ItemForecastResponse(BaseModel):
    item_id: str
    item_name: str
    item_type: str # "Material" or "Product"
    forecast: List[DailyForecast]