from typing import Dict, List, Tuple, Optional, Any
import simpy
import random
from loguru import logger
from datetime import datetime, timedelta, timezone, date

from .models import (
    SimulationState, ProductionOrder, PurchaseOrder, SimulationEvent,
    Product, Material, Provider, InitialConditions, FinancialConfig,
    ItemForecastResponse, DailyForecast,
    FinancialSummary, FinancialTimeseriesDataPoint, FinancialForecastDataPoint, FinancialPageData # New financial models
)
from . import crud, utils

SIMULATION_EPOCH_DATETIME = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
MIN_OPERATIONAL_COST = 1.0 # Absolute minimum daily operational cost if other calculations result in <=0

class FactorySimulation:
    def __init__(self, initial_state: SimulationState, products: List[Product],
                 materials: List[Material], providers: List[Provider],
                 config: Dict, financial_config: FinancialConfig): # Added financial_config
        self.env = simpy.Environment()
        self.state = initial_state
        if not hasattr(self.state, 'committed_inventory') or self.state.committed_inventory is None:
            self.state.committed_inventory = {}
        
        # Initialize balance if it's not already set (e.g. from loaded state vs new init)
        if not self.state.is_initialized: # Only for a brand new simulation from InitialConditions
             self.state.current_balance = financial_config.initial_balance

        self.products = {p.id: p for p in products}
        self.materials = {m.id: m for m in materials}
        self.providers = {p.id: p for p in providers}
        self.config = config # General sim config like random_order_config
        self.financial_config = financial_config # Specific financial parameters

    async def log_sim_event(self, event_type: str, details: Dict, is_financial: bool = False, amount: Optional[float] = None):
        if is_financial and amount is not None:
            details["amount_EUR"] = amount
            details["balance_before_EUR"] = self.state.current_balance - amount # Calculate what it was before this event
            details["balance_after_EUR"] = self.state.current_balance
            # Ensure amount reflects expense (negative) or income (positive) in details
            logger.info(f"[Day {self.state.current_day}] Financial Event: {event_type} - Amount: {amount:.2f} EUR, New Balance: {self.state.current_balance:.2f} EUR - Details: {details}")
        else:
            logger.info(f"[Day {self.state.current_day}] Event: {event_type} - Details: {details}")

        event = SimulationEvent(
            id=utils.generate_id(),
            day=self.state.current_day,
            event_type=event_type,
            details=details,
            timestamp=utils.get_current_utc_timestamp()
        )
        await crud.log_event(event)


    def get_total_inventory_units(self) -> int:
        return sum(self.state.inventory.values())

    async def check_storage_capacity(self, adding_quantity: int) -> bool:
        return (self.get_total_inventory_units() + adding_quantity) <= self.state.storage_capacity

    async def update_inventory(self, item_id: str, quantity_change: int, is_physical: bool = True):
        target_inventory = self.state.inventory if is_physical else self.state.committed_inventory
        current_qty = target_inventory.get(item_id, 0)
        new_qty = max(0, current_qty + quantity_change)
        target_inventory[item_id] = new_qty
        log_details = {
            "item_id": item_id, "change": quantity_change, "new_quantity": new_qty,
            "inventory_type": "physical" if is_physical else "committed"
        }
        await self.log_sim_event("inventory_change", log_details)

    async def get_production_order_async(self, order_id: str) -> Optional[ProductionOrder]:
        order_dict = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
        if order_dict:
            order_dict.setdefault('required_materials', {})
            order_dict.setdefault('committed_materials', {})
            order_dict.setdefault('revenue_collected', False) # Ensure field exists
            return ProductionOrder(**order_dict)
        return None

    async def get_purchase_order_async(self, po_id: str) -> Optional[PurchaseOrder]:
        po_dict = await crud.get_item_by_id(crud.COLLECTIONS["purchase_orders"], po_id)
        return PurchaseOrder(**po_dict) if po_dict else None

    async def _collect_revenue_for_order(self, order: ProductionOrder):
        if order.revenue_collected:
            return # Already collected

        product_price = self.financial_config.product_prices.get(order.product_id)
        if product_price is None:
            await self.log_sim_event("revenue_collection_failed_no_price", {
                "order_id": order.id, "product_id": order.product_id,
                "reason": f"Product ID {order.product_id} has no defined price in financial_config."
            })
            return

        revenue = product_price * order.quantity
        self.state.current_balance += revenue
        order.revenue_collected = True # Mark as collected
        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, {"revenue_collected": True})
        
        await self.log_sim_event("product_sale_revenue_collected", {
            "order_id": order.id, "product_id": order.product_id, "quantity": order.quantity,
            "unit_price_EUR": product_price, "total_revenue_EUR": revenue
        }, is_financial=True, amount=revenue)


    async def accept_production_order(self, order_id: str) -> Tuple[bool, str]:
        order = await self.get_production_order_async(order_id)
        if not order: return False, "Production order not found."
        if order.status != "Pending": return False, f"Order status is '{order.status}', not 'Pending'."

        product_to_make = self.products.get(order.product_id)
        if not product_to_make: return False, f"Product definition for {order.product_id} not found."

        original_order_quantity = order.quantity
        fulfilled_from_stock_qty = 0
        quantity_to_produce = original_order_quantity

        physical_stock_of_product = self.state.inventory.get(order.product_id, 0)

        if physical_stock_of_product > 0:
            can_fulfill_now = min(physical_stock_of_product, quantity_to_produce)
            if can_fulfill_now > 0 :
                await self.update_inventory(order.product_id, -can_fulfill_now, is_physical=True)
                fulfilled_from_stock_qty = can_fulfill_now
                quantity_to_produce -= can_fulfill_now
                
                # Create a temporary "fulfilled part" order representation for revenue collection
                temp_fulfilled_order_part = ProductionOrder(
                    id=f"{order.id}-stockpart", product_id=order.product_id, quantity=fulfilled_from_stock_qty,
                    requested_date=order.requested_date, status="Fulfilled", revenue_collected=False # revenue_collected will be set by _collect_revenue_for_order
                )
                await self._collect_revenue_for_order(temp_fulfilled_order_part) # Collect revenue for this part

                await self.log_sim_event("production_order_partially_fulfilled_from_stock", {
                    "order_id": order.id, "product_id": order.product_id,
                    "quantity_from_stock": fulfilled_from_stock_qty,
                    "remaining_quantity_for_production": quantity_to_produce
                })

                if quantity_to_produce == 0: # Fully fulfilled from stock
                    order.status = "Fulfilled"
                    order.completed_at = utils.get_current_utc_timestamp()
                    # order.revenue_collected will be handled if this was a sales order by _collect_revenue_for_order
                    # For the main order, we will mark revenue collected if all parts are.
                    # For now, let's assume the main order is fulfilled and revenue for the WHOLE order is collected IF it was a sales order.
                    # This needs to be clearer if a single ProdOrder can be part sales/part internal.
                    # Re-evaluating: _collect_revenue_for_order takes an order object.
                    # If it's fully fulfilled, we mark the original order.
                    if not order.revenue_collected: # check original hasn't been collected somehow
                         await self._collect_revenue_for_order(order)


                    await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                           order.model_dump(include={"status", "completed_at", "revenue_collected"}))
                    await crud.save_simulation_state(self.state)
                    return True, f"Order {order.id} for {original_order_quantity}x {product_to_make.name} fulfilled directly from stock. Revenue collected if applicable."

                # If partially fulfilled, update the original order's quantity and required materials
                order.quantity = quantity_to_produce
                new_required_materials = {}
                for bom_item in product_to_make.bom:
                    new_required_materials[bom_item.material_id] = \
                        new_required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * quantity_to_produce)
                order.required_materials = new_required_materials
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                       {"quantity": order.quantity, "required_materials": order.required_materials})
                logger.info(f"Order {order_id} partially fulfilled. {fulfilled_from_stock_qty} from stock. Remaining {quantity_to_produce} for production.")


        if quantity_to_produce > 0: # If there's still something to produce
            if not order.required_materials: # Recalculate if not already set for the (potentially reduced) quantity
                temp_req_mats = {}
                for bom_item in product_to_make.bom:
                    temp_req_mats[bom_item.material_id] = temp_req_mats.get(bom_item.material_id, 0) + (bom_item.quantity * quantity_to_produce)
                order.required_materials = temp_req_mats

            materials_ok_for_acceptance = True
            insufficient_material_details = ""
            materials_to_commit_upon_acceptance = {}

            for mat_id, qty_needed in order.required_materials.items():
                physical_mat_stock = self.state.inventory.get(mat_id, 0)
                already_committed_qty_to_others = self.state.committed_inventory.get(mat_id, 0)
                available_uncommitted_stock_for_acceptance = physical_mat_stock - already_committed_qty_to_others
                
                if available_uncommitted_stock_for_acceptance < qty_needed:
                    materials_ok_for_acceptance = False
                    material_name = self.materials.get(mat_id, Material(id=mat_id, name=mat_id)).name
                    insufficient_material_details = f"Insufficient uncommitted material: {material_name}. Need: {qty_needed}, Effectively Available: {available_uncommitted_stock_for_acceptance} (Physical: {physical_mat_stock} - Committed to others: {already_committed_qty_to_others})."
                    await self.log_sim_event("acceptance_failed_material_shortage", {
                        "order_id": order.id, "material_id": mat_id, "needed": qty_needed,
                        "available_effective": available_uncommitted_stock_for_acceptance,
                        "physical_stock": physical_mat_stock,
                        "committed_to_others": already_committed_qty_to_others
                    })
                    break
                else:
                    materials_to_commit_upon_acceptance[mat_id] = qty_needed

            if not materials_ok_for_acceptance:
                message = f"Production part of Order {order.id} for {product_to_make.name} ({quantity_to_produce} units) cannot be accepted. {insufficient_material_details}"
                if fulfilled_from_stock_qty > 0:
                    message = f"{fulfilled_from_stock_qty} units of {product_to_make.name} fulfilled from stock. Remaining {quantity_to_produce} units for Order {order.id} cannot be accepted. {insufficient_material_details}"
                return False, message

            order.committed_materials.clear() 
            for mat_id, qty_to_commit in materials_to_commit_upon_acceptance.items():
                await self.update_inventory(mat_id, -qty_to_commit, is_physical=True) # Reduce physical
                await self.update_inventory(mat_id, qty_to_commit, is_physical=False) # Increase committed
                order.committed_materials[mat_id] = qty_to_commit

            order.status = "Accepted"
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                   order.model_dump(include={"status", "committed_materials", "quantity", "required_materials"}))
            await self.log_sim_event("production_order_accepted_materials_committed", {
                "order_id": order.id,
                "original_requested_quantity": original_order_quantity,
                "quantity_accepted_for_production": quantity_to_produce,
                "fulfilled_directly_from_stock_qty": fulfilled_from_stock_qty,
                "committed_materials": order.committed_materials
            })
            await crud.save_simulation_state(self.state)
            message = f"Production part of Order {order.id} for {product_to_make.name} ({quantity_to_produce} units) accepted. Materials committed."
            if fulfilled_from_stock_qty > 0:
                 message = f"{fulfilled_from_stock_qty} units of {product_to_make.name} fulfilled from stock (revenue collected if applicable). Remaining {quantity_to_produce} units for Order {order.id} accepted for production and materials committed."
            return True, message

        # This part should not be reached if logic above is correct.
        # If quantity_to_produce became 0 and it was fully handled.
        return False, "Order processing error or no action taken."


    async def place_purchase_order_for_shortages(self, production_order_id: str) -> Dict[str, str]:
        order = await self.get_production_order_async(production_order_id)
        if not order: return {"error": "Production order not found."}
        if order.status not in ["Pending", "Accepted"]: # Can order even if accepted if more is needed due to other consumption
            return {"error": f"Cannot order materials for order with status '{order.status}' if not Pending/Accepted."}
        
        product = self.products.get(order.product_id)
        if not product: return {"error": f"Product definition for {order.product_id} not found."}

        # Use the current quantity of the order, which might have been reduced if partially fulfilled from stock
        current_order_quantity = order.quantity 
        if not order.required_materials or any(val == 0 for val in order.required_materials.values()): # Recalculate if empty or seems off for current quantity
            calculated_req_materials = {}
            for bom_item in product.bom:
                calculated_req_materials[bom_item.material_id] = \
                    calculated_req_materials.get(bom_item.material_id, 0) + (bom_item.quantity * current_order_quantity)
            order.required_materials = calculated_req_materials


        results = {}
        materials_ordered_summary = []
        for mat_id, qty_needed_for_order_line in order.required_materials.items():
            if qty_needed_for_order_line == 0: continue

            current_physical_stock = self.state.inventory.get(mat_id, 0)
            total_committed_globally = self.state.committed_inventory.get(mat_id, 0)
            
            # How much of this material is already committed TO THIS SPECIFIC production order?
            committed_for_this_order_already = 0
            if order.status == "Accepted" and order.committed_materials:
                committed_for_this_order_already = order.committed_materials.get(mat_id, 0)
            
            # What's the net additional need for this order for this material line?
            # It's the total requirement for the order line minus what's already committed to it.
            net_additional_need_for_this_order_this_mat = qty_needed_for_order_line - committed_for_this_order_already
            if net_additional_need_for_this_order_this_mat <= 0:
                results[mat_id] = f"No additional sourcing needed for {self.materials.get(mat_id, Material(id=mat_id, name=mat_id)).name} (already committed or requirement met)."
                continue

            # Effective physical stock available for *new* commitments (physical - committed to *other* orders)
            committed_elsewhere = total_committed_globally - committed_for_this_order_already
            effective_available_physical_for_new_commit = current_physical_stock - committed_elsewhere
            
            shortage_quantity = net_additional_need_for_this_order_this_mat - effective_available_physical_for_new_commit

            if shortage_quantity > 0:
                best_provider_id = None
                min_price = float('inf')
                selected_offering = None
                material_obj = self.materials.get(mat_id)
                if not material_obj:
                    results[mat_id] = f"Material definition for {mat_id} not found."
                    continue

                for prov_id, provider_obj in self.providers.items():
                    for offering in provider_obj.catalogue:
                        if offering.material_id == mat_id and offering.price_per_unit < min_price:
                            min_price = offering.price_per_unit
                            best_provider_id = prov_id
                            selected_offering = offering # Keep the offering for cost calculation
                
                if best_provider_id and selected_offering:
                    try:
                        # Order the calculated shortage_quantity
                        po = await self.place_purchase_order(mat_id, best_provider_id, shortage_quantity, selected_offering.price_per_unit)
                        results[mat_id] = f"Ordered {shortage_quantity} of {material_obj.name} from {self.providers[best_provider_id].name} (PO: {po.id}). Cost: {po.total_cost:.2f} EUR."
                        materials_ordered_summary.append(f"{material_obj.name}: {shortage_quantity}")
                    except ValueError as e: # Catch insufficient funds or other PO errors
                        results[mat_id] = f"Error placing PO for {material_obj.name}: {str(e)}"
                        # No PO created, so don't add to summary
                    except Exception as e_gen: # Catch other unexpected errors
                        results[mat_id] = f"Unexpected error placing PO for {material_obj.name}: {str(e_gen)}"

                else:
                    results[mat_id] = f"No provider found for {material_obj.name}."
                    await self.log_sim_event("material_shortage_no_provider", {
                        "order_id": order.id, "mat_id": mat_id, "needed": shortage_quantity
                    })
            else: # shortage_quantity <= 0
                results[mat_id] = f"Sufficient effective stock for {self.materials.get(mat_id, Material(id=mat_id, name=mat_id)).name}. Need to source: {net_additional_need_for_this_order_this_mat}, Effective Available: {effective_available_physical_for_new_commit}."
        
        if materials_ordered_summary:
             await self.log_sim_event("auto_ordered_materials_for_prod_order", {
                 "order_id": order.id, "ordered_summary": materials_ordered_summary
            })
        return results

    async def run_day(self):
        self.state.current_day += 1
        current_day_offset = self.state.current_day
        logger.info(f"--- Starting Simulation Day {current_day_offset} ---")
        await self.log_sim_event("day_start", {"day": current_day_offset, "balance_at_start_of_day_EUR": self.state.current_balance})
        
        # 1. Generate Random Customer Orders
        await self._generate_random_orders(current_day_offset)

        # 2. Process Purchase Order Arrivals
        pending_po_ids = self.state.pending_purchase_orders[:]
        current_sim_processing_datetime = SIMULATION_EPOCH_DATETIME + timedelta(days=current_day_offset)
        current_sim_processing_date = current_sim_processing_datetime.date()

        for po_id in pending_po_ids: 
            po = await self.get_purchase_order_async(po_id)
            if not po:
                if po_id in self.state.pending_purchase_orders: self.state.pending_purchase_orders.remove(po_id)
                continue
            expected_arrival_date_val = po.expected_arrival_date.date()
            if po.status == "Ordered" and expected_arrival_date_val <= current_sim_processing_date:
                 if await self.check_storage_capacity(po.quantity_ordered):
                    await self.update_inventory(po.material_id, po.quantity_ordered, is_physical=True)
                    po.status = "Arrived"; po.actual_arrival_date = current_sim_processing_datetime
                    po.units_received = po.quantity_ordered
                    await crud.update_item(crud.COLLECTIONS["purchase_orders"], po.id, po.model_dump(exclude_none=True))
                    if po_id in self.state.pending_purchase_orders: self.state.pending_purchase_orders.remove(po_id)
                    await self.log_sim_event("material_arrival", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered, "cost_EUR": po.total_cost})
                 else:
                     await self.log_sim_event("arrival_delayed_storage", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered})

        # 3. Process Production Completions
        completed_production_today = 0
        active_order_ids_today = self.state.active_production_orders[:]
        for order_id in active_order_ids_today: 
            order = await self.get_production_order_async(order_id)
            if not order or order.status != "In Progress":
                if order_id in self.state.active_production_orders: self.state.active_production_orders.remove(order_id)
                continue
            product = self.products.get(order.product_id);
            if not product: continue

            if order.started_at:
                started_at_aware = order.started_at.replace(tzinfo=timezone.utc) if order.started_at.tzinfo is None else order.started_at
                days_in_production = (current_sim_processing_datetime.date() - started_at_aware.date()).days
                if days_in_production >= product.production_time:
                    if completed_production_today < self.state.daily_production_capacity:
                        if order.committed_materials: # Consume committed materials
                            for mat_id, qty_consumed in order.committed_materials.items():
                                await self.update_inventory(mat_id, -qty_consumed, is_physical=False) 
                        
                        # Add finished product to inventory
                        await self.update_inventory(order.product_id, order.quantity, is_physical=True)

                        order.status = "Completed"; order.completed_at = current_sim_processing_datetime
                        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(exclude_none=True, include={"status", "completed_at"}))
                        if order_id in self.state.active_production_orders:
                            self.state.active_production_orders.remove(order_id)
                        
                        await self.log_sim_event("production_completed", {"order_id":order.id, "prod_id":order.product_id, "qty":order.quantity})
                        
                        # Collect revenue if this completed order was a sales order and not yet collected
                        # Assuming orders from _generate_random_orders are sales orders
                        # This check should ideally be more robust based on order source/type
                        if not order.revenue_collected: # Check again, though accept might have handled stock fulfillment
                            await self._collect_revenue_for_order(order)

                        completed_production_today += 1
                    else:
                        await self.log_sim_event("production_delayed_capacity", {"order_id": order.id})
        
        # 4. Apply Daily Operational Costs
        base_cost = self.financial_config.daily_operational_cost_base
        per_item_cost = self.financial_config.daily_operational_cost_per_item_in_production
        num_items_in_prod = len(self.state.active_production_orders) # Orders still active *after* today's completions
        
        daily_cost = base_cost + (num_items_in_prod * per_item_cost)
        daily_cost = max(MIN_OPERATIONAL_COST, daily_cost) # Ensure it's not zero or negative if config allows
        
        self.state.current_balance -= daily_cost
        await self.log_sim_event("operational_cost_deducted", {
            "base_cost_EUR": base_cost, "cost_per_item_in_prod_EUR": per_item_cost,
            "items_in_production": num_items_in_prod, "total_daily_cost_EUR": daily_cost
        }, is_financial=True, amount=-daily_cost)


        # 5. Save State
        await crud.save_simulation_state(self.state)
        logger.info(f"--- Ending Simulation Day {current_day_offset} --- Balance: {self.state.current_balance:.2f} EUR")
        await self.log_sim_event("day_end", {"day": current_day_offset, "balance_at_end_of_day_EUR": self.state.current_balance})
        return self.state

    async def _generate_random_orders(self, current_day_offset: int):
        cfg = self.config.get("random_order_config", {})
        num_demands = random.randint(cfg.get("min_orders_per_day", 0), cfg.get("max_orders_per_day", 2))
        product_ids_available_for_order = list(self.products.keys())
        if not product_ids_available_for_order: return

        current_sim_datetime_for_request = SIMULATION_EPOCH_DATETIME + timedelta(days=current_day_offset)
        for _ in range(num_demands):
            product_id = random.choice(product_ids_available_for_order)
            requested_quantity = random.randint(cfg.get("min_qty_per_order", 1), cfg.get("max_qty_per_order", 5))
            product_obj = self.products[product_id]
            
            # Check if price exists for this product, log warning if not but still create order
            if product_id not in self.financial_config.product_prices:
                 logger.warning(f"New demand for product {product_id} ({product_obj.name}) which has no price defined in financial_config. Revenue will not be collected.")
                 await self.log_sim_event("demand_generated_no_price", {
                     "product_id": product_id, "quantity": requested_quantity,
                     "warning": "No price defined, revenue will be zero for this order."
                 })


            required_materials = {}
            for bom_item in product_obj.bom:
                required_materials[bom_item.material_id] = required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * requested_quantity)

            new_order = ProductionOrder(
                id=utils.generate_id(), product_id=product_id, quantity=requested_quantity,
                requested_date=current_sim_datetime_for_request, status="Pending",
                required_materials=required_materials, created_at=utils.get_current_utc_timestamp(),
                revenue_collected=False # Explicitly false for new sales orders
            )
            await crud.create_item(crud.COLLECTIONS["production_orders"], new_order.model_dump())
            await self.log_sim_event("order_received_for_production", { # This event implies a customer order
                "order_id": new_order.id, "product_id": product_id, "qty_for_prod": requested_quantity,
                "original_demand": requested_quantity # Assuming all random orders are for direct sale
            })
            logger.info(f"Prod Order {new_order.id} (customer demand) for {requested_quantity}x {product_obj.name} created as 'Pending'.")


    async def start_production(self, order_ids: List[str]) -> Dict[str, str]:
        results = {}
        current_sim_datetime_for_start = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)
        for order_id in order_ids:
            order = await self.get_production_order_async(order_id)
            if not order: results[order_id] = "Order not found."; continue
            if order.status != "Accepted":
                results[order_id] = f"Order status is '{order.status}', must be 'Accepted'."
                continue

            order.status = "In Progress"
            order.started_at = current_sim_datetime_for_start

            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                   order.model_dump(include={"status", "started_at"}))

            if order.id not in self.state.active_production_orders:
                self.state.active_production_orders.append(order.id)

            results[order_id] = "Production started successfully."
            await self.log_sim_event("production_started", { 
                "order_id": order.id, "product_id": order.product_id,
                "previously_committed_materials": order.committed_materials 
            })

        await crud.save_simulation_state(self.state)
        return results

    async def fulfill_accepted_order_from_stock(self, order_id: str) -> Tuple[bool, str]:
        order = await self.get_production_order_async(order_id)
        if not order: return False, "Production order not found."
        if order.status != "Accepted":
            return False, f"Order status is '{order.status}', must be 'Accepted' to fulfill this way."

        product_to_fulfill = self.products.get(order.product_id)
        if not product_to_fulfill: return False, f"Product definition for {order.product_id} not found."

        physical_stock_of_product = self.state.inventory.get(order.product_id, 0)
        if physical_stock_of_product < order.quantity:
            return False, f"Insufficient finished product '{product_to_fulfill.name}' in stock. Need: {order.quantity}, Have: {physical_stock_of_product}."

        await self.update_inventory(order.product_id, -order.quantity, is_physical=True) # Reduce stock

        # Un-commit materials that were reserved for this order
        if order.committed_materials:
            for mat_id, qty_committed in order.committed_materials.items():
                await self.update_inventory(mat_id, qty_committed, is_physical=True) # Return to physical
                await self.update_inventory(mat_id, -qty_committed, is_physical=False) # Reduce committed
            await self.log_sim_event("materials_uncommitted_for_fulfillment", {
                "order_id": order.id, "uncommitted_materials": order.committed_materials
            })
        order.committed_materials.clear() 

        order.status = "Fulfilled"
        order.completed_at = utils.get_current_utc_timestamp()
        
        # Collect revenue for this fulfilled order if not already collected
        if not order.revenue_collected:
            await self._collect_revenue_for_order(order)

        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                               order.model_dump(include={"status", "completed_at", "committed_materials", "revenue_collected"}))

        await self.log_sim_event("accepted_order_fulfilled_from_stock", {
            "order_id": order.id, "product_id": order.product_id,
            "quantity_fulfilled": order.quantity
        })
        await crud.save_simulation_state(self.state)
        return True, f"Accepted order {order.id} for {order.quantity}x {product_to_fulfill.name} fulfilled from stock. Materials uncommitted. Revenue collected if applicable."

    async def place_purchase_order(self, material_id: str, provider_id: str, quantity: int, unit_price_override: Optional[float] = None) -> PurchaseOrder:
        provider = self.providers.get(provider_id); material = self.materials.get(material_id)
        if not provider or not material: raise ValueError("Invalid provider or material ID.")
        
        offering = next((o for o in provider.catalogue if o.material_id == material_id), None)
        if not offering: raise ValueError(f"Provider {provider.name} does not offer {material.name}.")

        if quantity <= 0:
            raise ValueError(f"Purchase order quantity must be positive. Attempted to order {quantity} of {material.name}.")

        material_cost_per_unit = unit_price_override if unit_price_override is not None else offering.price_per_unit
        total_po_cost = material_cost_per_unit * quantity

        if self.state.current_balance < total_po_cost:
            raise ValueError(f"Insufficient funds to purchase {quantity} of {material.name}. Need {total_po_cost:.2f} EUR, have {self.state.current_balance:.2f} EUR.")

        self.state.current_balance -= total_po_cost # Deduct cost

        order_timestamp = utils.get_current_utc_timestamp()
        sim_day_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()
        expected_arrival_dt = datetime.combine(sim_day_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=offering.lead_time_days)

        po = PurchaseOrder(id=utils.generate_id(), material_id=material_id, provider_id=provider_id,
                           quantity_ordered=quantity, order_date=order_timestamp,
                           expected_arrival_date=expected_arrival_dt, status="Ordered", created_at=order_timestamp,
                           total_cost=total_po_cost) # Store total cost
        
        if po.id not in self.state.pending_purchase_orders:
            self.state.pending_purchase_orders.append(po.id)
        
        await crud.save_simulation_state(self.state) # Save state *after* balance change and adding to pending list
        
        po_dict = await crud.create_item(crud.COLLECTIONS["purchase_orders"], po.model_dump())

        await self.log_sim_event("purchase_order_placed", {
            "po_id": po.id, "mat_id": material_id, "prov_id": provider_id, "qty": quantity, 
            "unit_cost_EUR": material_cost_per_unit, "total_cost_EUR": total_po_cost, 
            "eta": expected_arrival_dt.isoformat()
        }, is_financial=True, amount=-total_po_cost) # Amount is negative for expense
        
        logger.info(f"Placed PO {po.id} for {quantity}x {material.name}. Cost: {total_po_cost:.2f} EUR. ETA: {expected_arrival_dt.isoformat()}")
        return PurchaseOrder(**po_dict) if po_dict else po


    async def get_item_forecast(self, item_id: str, num_days: int, historical_lookback_days: int = 0) -> ItemForecastResponse:
        # (Existing forecast logic - no direct financial impact here, but financial forecast will be separate)
        if item_id in self.materials:
            item_type = "Material"; item_name = self.materials[item_id].name
        elif item_id in self.products:
            item_type = "Product"; item_name = self.products[item_id].name
        else:
            raise ValueError(f"Item ID {item_id} not found as a material or product.")

        current_sim_datetime = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)
        current_sim_date = current_sim_datetime.date()
        physical_stock = self.state.inventory.get(item_id, 0)
        daily_deltas = [0.0] * num_days 
        forecast_list: List[DailyForecast] = []

        # ... (rest of the existing item forecast logic remains unchanged) ...
        # This logic projects physical stock, not financial values.
        # For brevity, I'm not repeating the entire existing get_item_forecast logic.
        # Assume it works as before.
        
        # --- Placeholder for the rest of the original get_item_forecast logic ---
        # This part calculates physical stock forecast.
        # The financial forecast will be a new, separate function.
        # --- End Placeholder ---

        # Reconstruct the original logic for physical forecast here:
        if historical_lookback_days > 0:
            for i in range(historical_lookback_days):
                day_offset_val = -historical_lookback_days + i 
                actual_day_number_eod = self.state.current_day + day_offset_val 
                forecast_dt = current_sim_date + timedelta(days=day_offset_val)
                qty_for_this_hist_day = 0.0 
                if actual_day_number_eod == self.state.current_day -1 : # Yesterday's EOD physical stock
                     physical_events_yesterday = await crud.get_items(
                        crud.COLLECTIONS["events"],
                        query={ "event_type": "inventory_change", "details.item_id": item_id, "details.inventory_type": "physical", "day": self.state.current_day -1 },
                        sort_field="timestamp", sort_order=-1, limit=1
                    )
                     if physical_events_yesterday:
                         qty_for_this_hist_day = float(physical_events_yesterday[0].get("details",{}).get("new_quantity", 0.0))
                     else: # if no event yesterday, try to get latest before that
                          latest_event_raw = await crud.get_items(
                            crud.COLLECTIONS["events"],
                            query={ "event_type": "inventory_change", "details.item_id": item_id, "details.inventory_type": "physical", "day": {"$lt": self.state.current_day -1} },
                            sort_field="timestamp", sort_order=-1, limit=1
                          )
                          if latest_event_raw: qty_for_this_hist_day = float(latest_event_raw[0].get("details",{}).get("new_quantity", 0.0))

                elif actual_day_number_eod < self.state.current_day -1: # Days before yesterday
                    latest_event_raw = await crud.get_items(
                        crud.COLLECTIONS["events"],
                        query={ "event_type": "inventory_change", "details.item_id": item_id, "details.inventory_type": "physical",  "day": {"$lte": actual_day_number_eod} },
                        sort_field="timestamp", sort_order=-1, limit=1
                    )
                    if latest_event_raw: qty_for_this_hist_day = float(latest_event_raw[0].get("details",{}).get("new_quantity", 0.0))
                forecast_list.append(DailyForecast(day_offset=day_offset_val, date=forecast_dt, quantity=qty_for_this_hist_day))

        if item_type == "Material":
            pending_pos_dicts = await crud.get_items(crud.COLLECTIONS["purchase_orders"], {"status": "Ordered", "material_id": item_id}, limit=None)
            for po_dict in pending_pos_dicts:
                po = PurchaseOrder(**po_dict)
                arrival_offset = (po.expected_arrival_date.date() - current_sim_date).days
                if 0 <= arrival_offset < num_days: daily_deltas[arrival_offset] += po.quantity_ordered
            
            # Consumption from In Progress Production Orders (simplified: materials consumed on start day of production cycle)
            # More accurate would be to spread consumption over production_time if needed.
            # For this forecast, we assume materials are "gone" from available when production starts.
            # Committed materials were already removed from physical when order was accepted.
            # This forecast should show *projected physical stock*.
            # Let's consider consumption when production *completes* and committed are cleared for simplicity.
            # No, committed are used. Physical forecast should reflect this.
            # The `committed_inventory` tracks this. The forecast should show *available to promise* effectively.
            # The provided forecast logic in the original code seems to focus on this.
            # This item forecast should represent the *end-of-day physical quantity*.
            # So, when a production order starts, materials are committed. When it finishes, committed are cleared.
            # The current physical stock *already reflects* committed materials being moved out.
            # So, the `daily_deltas` should reflect arrivals (for materials) and production completions (for products).
            pass # Material consumption is complex for *future* forecast of physical.
                 # The current `projected_available` in inventory view is better for immediate decisions.
                 # For a true physical stock forecast:
                 # - Add incoming POs.
                 # - Subtract for planned production starts (if we can predict them). This is hard.
                 # The existing `get_item_forecast` in user's code was already quite detailed. Let's try to use that.
            production_orders_consuming_material = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress"}, limit=None)
            for prod_o_dict in production_orders_consuming_material:
                prod_o = ProductionOrder(**prod_o_dict)
                product_def = self.products.get(prod_o.product_id)
                if not product_def or not prod_o.started_at: continue
                
                # When does consumption of *committed* materials effectively happen against *physical*?
                # It already happened when order was accepted (physical reduced, committed increased).
                # When production completes, committed is reduced.
                # This physical forecast is about the *actual pile of stuff*.
                # So no subtractions here for in-progress orders, as physical was already reduced.
                pass


        elif item_type == "Product":
            in_progress_orders_dicts = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "In Progress", "product_id": item_id}, limit=None)
            for prod_o_dict in in_progress_orders_dicts:
                prod_o = ProductionOrder(**prod_o_dict)
                product_def = self.products.get(prod_o.product_id)
                if product_def and prod_o.started_at:
                    started_at_date = prod_o.started_at.date()
                    completion_date = started_at_date + timedelta(days=product_def.production_time)
                    completion_offset = (completion_date - current_sim_date).days
                    if 0 <= completion_offset < num_days: daily_deltas[completion_offset] += prod_o.quantity
            
            # Consider "Accepted" orders that might get fulfilled from stock or start production
            accepted_orders_dicts = await crud.get_items(crud.COLLECTIONS["production_orders"], {"status": "Accepted", "product_id": item_id}, limit=None)
            # This gets complex: do we predict they start production or get fulfilled?
            # For now, this forecast focuses on *already in-progress* completions.
            # Predicting future starts/fulfillments is more advanced MRP.

        running_balance = float(physical_stock)
        for d_offset in range(num_days): 
            running_balance += daily_deltas[d_offset]
            forecast_dt = current_sim_date + timedelta(days=d_offset)
            forecast_list.append(DailyForecast(day_offset=d_offset, date=forecast_dt, quantity=running_balance))
            
        return ItemForecastResponse(item_id=item_id, item_name=item_name, item_type=item_type, forecast=forecast_list)

    async def get_financial_data(self, forecast_days: int = 7) -> FinancialPageData:
        """
        Gathers historical financial data and projects a short-term financial forecast.
        """
        # 1. Current Summary (simple version for now)
        # These would ideally be built from historical transaction events for accuracy.
        all_events = await crud.get_all_items(crud.COLLECTIONS["events"], sort_field="timestamp", sort_order=1)
        
        total_revenue = 0
        total_material_costs = 0
        total_operational_costs = 0

        daily_financials_map: Dict[int, Dict[str, float]] = {} # day -> {revenue, material_costs, operational_costs, balance_eod}

        for event_dict in all_events:
            event = SimulationEvent(**event_dict)
            day = event.day
            if day not in daily_financials_map:
                daily_financials_map[day] = {"revenue": 0, "material_costs": 0, "operational_costs": 0, "balance_eod": 0}

            if event.event_type == "product_sale_revenue_collected":
                rev = event.details.get("total_revenue_EUR", 0)
                total_revenue += rev
                daily_financials_map[day]["revenue"] += rev
            elif event.event_type == "purchase_order_placed": # Cost incurred when placed
                cost = event.details.get("total_cost_EUR", 0) # This is positive in details, represents expense
                total_material_costs += cost
                daily_financials_map[day]["material_costs"] += cost
            elif event.event_type == "operational_cost_deducted":
                cost = event.details.get("total_daily_cost_EUR", 0) # Positive in details
                total_operational_costs += cost
                daily_financials_map[day]["operational_costs"] += cost
            
            if event.event_type == "day_end": # Capture end of day balance
                balance = event.details.get("balance_at_end_of_day_EUR")
                if balance is not None:
                     daily_financials_map[day]["balance_eod"] = balance


        historical_performance: List[FinancialTimeseriesDataPoint] = []
        # Ensure days are sorted if iterating through map keys
        sorted_days = sorted(daily_financials_map.keys())
        for day_num in sorted_days:
            if day_num == 0 and self.state.current_day == 0 and not daily_financials_map[day_num]: # Skip empty day 0 if current day is 0
                # Use initial balance for day 0 if it's the current day and no transactions happened yet
                if self.state.current_day == 0:
                     day_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=day_num)).date()
                     initial_bal = self.financial_config.initial_balance # or self.state.current_balance if it was loaded
                     historical_performance.append(FinancialTimeseriesDataPoint(
                        day=day_num, date=day_date, balance=initial_bal,
                        revenue=0, material_costs=0, operational_costs=0,
                        profit=0
                    ))
                continue

            day_data = daily_financials_map[day_num]
            day_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=day_num)).date()
            day_profit = day_data["revenue"] - day_data["material_costs"] - day_data["operational_costs"]
            
            # If balance_eod was not captured by a day_end event (e.g. for day 0 startup), try to infer
            balance_to_use = day_data.get("balance_eod")
            if balance_to_use is None: # Fallback for days without explicit EOD balance event (e.g. Day 0 before first day_end)
                if day_num == 0: balance_to_use = self.financial_config.initial_balance
                elif day_num > 0 and (day_num -1) in daily_financials_map and daily_financials_map[day_num-1].get("balance_eod") is not None:
                    balance_to_use = daily_financials_map[day_num-1]["balance_eod"] + day_profit
                else: # Very rough estimate if prior EOD not found
                    balance_to_use = self.state.current_balance if day_num == self.state.current_day else 0


            historical_performance.append(FinancialTimeseriesDataPoint(
                day=day_num, date=day_date, balance=balance_to_use,
                revenue=day_data["revenue"], material_costs=day_data["material_costs"],
                operational_costs=day_data["operational_costs"], profit=day_profit
            ))

        # Correct Day 0 balance if it was missed and it's the current day (sim just initialized)
        if self.state.current_day == 0 and not any(hp.day == 0 for hp in historical_performance):
             historical_performance.insert(0, FinancialTimeseriesDataPoint(
                day=0, date=SIMULATION_EPOCH_DATETIME.date(), balance=self.state.current_balance,
                revenue=0, material_costs=0, operational_costs=0, profit=0
            ))


        profit_to_date = total_revenue - total_material_costs - total_operational_costs
        summary = FinancialSummary(
            current_balance=self.state.current_balance,
            total_revenue_to_date=total_revenue,
            total_expenses_to_date=total_material_costs + total_operational_costs,
            profit_to_date=profit_to_date
        )

        # 2. Forecast
        forecast: List[FinancialForecastDataPoint] = []
        projected_balance = self.state.current_balance
        current_sim_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()

        # Get all pending POs for material cost projection
        pending_pos_raw = await crud.get_items(crud.COLLECTIONS["purchase_orders"], {"status": "Ordered"}, limit=None)
        pending_pos = [PurchaseOrder(**po) for po in pending_pos_raw]

        # Get all in-progress and accepted production orders for revenue projection
        # For simplicity, assume 'Accepted' orders will start soon and complete based on their product's production time.
        # This is a rough forecast.
        prod_orders_for_revenue_raw = await crud.get_items(
            crud.COLLECTIONS["production_orders"],
            {"status": {"$in": ["In Progress", "Accepted"]}, "revenue_collected": False}, # Only those not yet paid
            limit=None
        )
        potential_revenue_orders = [ProductionOrder(**o) for o in prod_orders_for_revenue_raw]


        for i in range(forecast_days):
            day_offset = i 
            forecast_date = current_sim_date + timedelta(days=day_offset)
            sim_day_for_forecast = self.state.current_day + day_offset

            # Projected Operational Costs for this future day
            # Estimate active orders: current active + accepted that might start
            # This is a simplification; actual active orders might change.
            estimated_active_orders = len(self.state.active_production_orders) + \
                                      len([o for o in potential_revenue_orders if o.status == "Accepted"])
            
            proj_op_cost = self.financial_config.daily_operational_cost_base + \
                           (estimated_active_orders * self.financial_config.daily_operational_cost_per_item_in_production)
            proj_op_cost = max(MIN_OPERATIONAL_COST, proj_op_cost)
            projected_balance -= proj_op_cost

            # Projected Material Costs (from POs arriving - already paid, so no balance change here)
            # If POs were paid on arrival, this would be different. We assume paid on order.
            # So, daily material cost for forecast is 0 unless we change payment terms.
            proj_mat_cost_today = 0 # Costs are incurred when PO is placed.

            # Projected Revenue
            proj_rev_today = 0
            for order in potential_revenue_orders:
                product = self.products.get(order.product_id)
                if not product: continue
                
                price = self.financial_config.product_prices.get(order.product_id)
                if not price: continue

                completion_sim_day = -1
                if order.status == "In Progress" and order.started_at:
                    started_at_sim_day = (order.started_at.date() - SIMULATION_EPOCH_DATETIME.date()).days
                    completion_sim_day = started_at_sim_day + product.production_time
                elif order.status == "Accepted":
                    # Assume it starts "today" relative to the forecast day if not started
                    # This is a big assumption. A better forecast would use an MRP plan.
                    # For this simple forecast, assume accepted orders start on the forecast day 'i'
                    # and complete 'production_time' days after that.
                    # Let's assume it starts on current_day and project completion from there.
                    # If we are forecasting for day i, assume it could complete on day i.
                    assumed_start_day_for_accepted = self.state.current_day # Assume they start immediately from current_day
                    completion_sim_day = assumed_start_day_for_accepted + product.production_time


                if completion_sim_day == sim_day_for_forecast: # If completes on this forecasted day
                    proj_rev_today += price * order.quantity
                    projected_balance += price * order.quantity
            
            forecast.append(FinancialForecastDataPoint(
                day_offset=day_offset,
                date=forecast_date,
                projected_balance=projected_balance,
                projected_revenue=proj_rev_today,
                projected_material_costs=proj_mat_cost_today, # Already paid
                projected_operational_costs=proj_op_cost,
                projected_profit=proj_rev_today - proj_op_cost - proj_mat_cost_today
            ))

        return FinancialPageData(summary=summary, historical_performance=historical_performance, forecast=forecast)