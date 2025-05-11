from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any
from datetime import datetime, date

# --- Existing Enum-like constants (if any, or can be added) ---
# e.g. TransactionType = Literal["material_purchase", "product_sale", "operational_cost"]

# --- Financial Configuration ---
class FinancialConfig(BaseModel):
    initial_balance: float = Field(10000.0, description="Initial monetary balance in EURO")
    product_prices: Dict[str, float] = Field({}, description="Maps product_id to its selling price")
    daily_operational_cost_base: float = Field(50.0, description="Fixed daily operational cost in EURO")
    daily_operational_cost_per_item_in_production: float = Field(5.0, description="Additional daily cost per item actively in production")
    # Material costs are derived from ProviderOffering.price_per_unit

class Material(BaseModel):
    id: str = Field(..., description="Unique material ID")
    name: str
    description: Optional[str] = None
    # Cost is per provider, so not directly here.

class ProductBOM(BaseModel):
    material_id: str
    quantity: int

class Product(BaseModel):
    id: str = Field(..., description="Unique product ID")
    name: str
    bom: List[ProductBOM] = Field(..., description="Bill of Materials")
    production_time: int = Field(..., description="Time in days to produce one unit")
    # price: float = Field(..., description="Selling price of the product") # Moved to FinancialConfig for central management

class ProviderOffering(BaseModel):
    material_id: str
    price_per_unit: float # This is the cost of the material from this provider
    offered_unit_size: int = Field(1, description="e.g., 1 for single units, 100 for a pallet")
    lead_time_days: int

class Provider(BaseModel):
    id: str = Field(..., description="Unique provider ID")
    name: str
    catalogue: List[ProviderOffering]

class ProductionOrder(BaseModel):
    id: str = Field(..., description="Unique production order ID")
    product_id: str
    quantity: int
    requested_date: datetime
    status: str = Field("Pending", description="Pending, Accepted, In Progress, Completed, Cancelled, Fulfilled")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    required_materials: Dict[str, int] = Field({}, description="Calculated total materials needed for the current quantity")
    committed_materials: Dict[str, int] = Field({}, description="Materials committed from inventory when order is accepted or production starts")
    # Add a flag to know if revenue has been collected for this order if it's a customer order
    revenue_collected: bool = Field(False, description="True if revenue for this order has been added to balance")


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
    total_cost: Optional[float] = Field(None, description="Total cost of this purchase order when it was placed")


class SimulationEvent(BaseModel):
    id: str = Field(..., description="Unique event ID")
    day: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: str # e.g., "financial_transaction", "inventory_change"
    details: Dict[str, Any]

# --- Financial Transaction Log (optional, but good for auditing) ---
class FinancialTransaction(BaseModel):
    id: str = Field(default_factory=lambda: utils.generate_id(), description="Unique transaction ID")
    day: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    transaction_type: str # e.g., "material_purchase", "product_sale", "operational_cost"
    description: str
    amount: float # Positive for income, negative for expenses
    related_item_id: Optional[str] = None # e.g., PO ID, Production Order ID
    balance_after_transaction: float

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
    # Financials
    current_balance: float = Field(0.0, description="Current monetary balance in EURO")
    # financial_log: List[FinancialTransaction] = Field([], description="Log of all financial transactions") # Decided to make this a separate collection for performance if it grows large. Events can log key financial moments.

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
    financial_config: FinancialConfig = Field(default_factory=FinancialConfig, description="Initial financial settings for the factory")


class ProductionStartRequest(BaseModel):
    order_ids: List[str]

class PurchaseOrderRequest(BaseModel):
    material_id: str
    provider_id: str
    quantity: int

class StatusResponse(BaseModel):
    message: str
    details: Optional[Dict[str, Any]] = None

class SimulationStatus(BaseModel): # For the main dashboard and sidebar
    current_day: int
    total_inventory_units: int
    storage_capacity: int
    storage_utilization: float
    pending_production_orders: int
    accepted_production_orders: int
    in_progress_production_orders: int
    pending_purchase_orders: int
    # Financial Summary
    current_balance: float
    # Potentially add a very brief financial health indicator or trend here later


class DataExport(BaseModel):
    simulation_state: SimulationState
    events: List[SimulationEvent]
    production_orders: List[ProductionOrder]
    purchase_orders: List[PurchaseOrder]
    products: List[Product]
    providers: List[Provider]
    materials: List[Material]
    financial_config: FinancialConfig # To ensure financial settings are exported
    # financial_transactions: List[FinancialTransaction] # If using a separate transactions collection

class InventoryDetail(BaseModel):
    item_id: str
    name: str
    type: str
    physical: int = 0
    committed: int = 0
    on_order: int = 0
    projected_available: int = 0

class InventoryStatusResponse(BaseModel):
    items: Dict[str, InventoryDetail] = Field({}, description="Maps item_id to its detailed inventory status")

class DailyForecast(BaseModel):
    day_offset: int
    date: date
    quantity: float

class ItemForecastResponse(BaseModel):
    item_id: str
    item_name: str
    item_type: str
    forecast: List[DailyForecast]

# --- New Models for Finances Page ---
class FinancialSummary(BaseModel):
    current_balance: float
    total_revenue_to_date: float # Could be calculated from events or a running total
    total_expenses_to_date: float # Could be calculated or a running total
    profit_to_date: float
    # Add more as needed, e.g., avg daily cost, avg daily revenue

class FinancialTimeseriesDataPoint(BaseModel):
    day: int
    date: date # Representing the end of this day
    balance: float
    revenue: float # Revenue for this day
    material_costs: float # Material costs for this day
    operational_costs: float # Operational costs for this day
    profit: float # Profit for this day

class FinancialForecastDataPoint(BaseModel):
    day_offset: int # Relative to current day (0 is today, 1 is tomorrow)
    date: date
    projected_balance: float
    projected_revenue: float
    projected_material_costs: float
    projected_operational_costs: float
    projected_profit: float

class FinancialPageData(BaseModel):
    summary: FinancialSummary
    historical_performance: List[FinancialTimeseriesDataPoint]
    forecast: List[FinancialForecastDataPoint]