from typing import Dict, List, Tuple, Optional
import simpy 
import random
from loguru import logger
from datetime import datetime, timedelta, timezone, date

from .models import (
    SimulationState, ProductionOrder, PurchaseOrder, SimulationEvent,
    Product, Material, Provider, InitialConditions
)
from . import crud, utils

SIMULATION_EPOCH_DATETIME = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

class FactorySimulation:
    def __init__(self, initial_state: SimulationState, products: List[Product], materials: List[Material], providers: List[Provider], config: Dict):
        self.env = simpy.Environment() 
        self.state = initial_state
        if not hasattr(self.state, 'committed_inventory') or self.state.committed_inventory is None:
            self.state.committed_inventory = {}

        self.products = {p.id: p for p in products}
        self.materials = {m.id: m for m in materials}
        self.providers = {p.id: p for p in providers}
        self.config = config

    async def log_sim_event(self, event_type: str, details: Dict):
        event = SimulationEvent(
            id=utils.generate_id(),
            day=self.state.current_day,
            event_type=event_type,
            details=details,
            timestamp=utils.get_current_utc_timestamp()
        )
        await crud.log_event(event)
        logger.info(f"[Day {self.state.current_day}] Event: {event_type} - Details: {details}")

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
            return ProductionOrder(**order_dict)
        return None
        
    async def get_purchase_order_async(self, po_id: str) -> Optional[PurchaseOrder]:
        po_dict = await crud.get_item_by_id(crud.COLLECTIONS["purchase_orders"], po_id)
        return PurchaseOrder(**po_dict) if po_dict else None

    async def accept_production_order(self, order_id: str) -> Tuple[bool, str]:
        order = await self.get_production_order_async(order_id)
        if not order: return False, "Production order not found."
        if order.status != "Pending": return False, f"Order status is '{order.status}', not 'Pending'."
        
        product_to_make = self.products.get(order.product_id)
        if not product_to_make: return False, f"Product definition for {order.product_id} not found."

        # Check if finished product is in stock
        physical_stock_of_product = self.state.inventory.get(order.product_id, 0)
        original_order_quantity = order.quantity
        fulfilled_from_stock_qty = 0

        if physical_stock_of_product > 0:
            if physical_stock_of_product >= order.quantity:
                # Fulfill entirely from stock
                fulfilled_from_stock_qty = order.quantity
                await self.update_inventory(order.product_id, -order.quantity, is_physical=True)
                order.status = "Fulfilled"
                order.completed_at = utils.get_current_utc_timestamp() # Using completed_at for fulfillment time
                # order.quantity remains original_order_quantity for this fulfilled order record
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, 
                                       order.model_dump(include={"status", "completed_at", "quantity"}))
                await self.log_sim_event("production_order_fulfilled_from_stock", {
                    "order_id": order.id, "product_id": order.product_id, 
                    "quantity_fulfilled": order.quantity
                })
                await crud.save_simulation_state(self.state)
                return True, f"Order {order.id} for {order.quantity}x {product_to_make.name} fulfilled directly from stock."
            else: # physical_stock_of_product < order.quantity (partial fulfillment)
                fulfilled_from_stock_qty = physical_stock_of_product
                await self.update_inventory(order.product_id, -physical_stock_of_product, is_physical=True)
                
                new_quantity_for_production = order.quantity - physical_stock_of_product
                
                await self.log_sim_event("production_order_partially_fulfilled_from_stock", {
                    "order_id": order.id, "product_id": order.product_id, 
                    "quantity_from_stock": physical_stock_of_product,
                    "remaining_quantity_for_production": new_quantity_for_production
                })
                
                order.quantity = new_quantity_for_production # Update order to remaining quantity
                # Recalculate required_materials for the reduced quantity
                new_required_materials = {}
                for bom_item in product_to_make.bom:
                    new_required_materials[bom_item.material_id] = \
                        new_required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * new_quantity_for_production)
                order.required_materials = new_required_materials
                
                # Update the original order to reflect partial fulfillment and new pending quantity for production
                # This can be complex: either create a new order for remaining, or update this one.
                # For simplicity, we update this order and it proceeds to "Accepted".
                # A separate "Fulfilled" record could be made for the part taken from stock if desired.
                # Let's assume the original order is now for the smaller, pending amount.
                # No, the problem asks to fulfill what is possible and then the rest continues as an "Accepted" request.
                # This means we should split the order or handle this more gracefully.
                # For now, let's log the partial fulfillment, and the *original* order (with original quantity) moves to Accepted.
                # The UI will then show material shortage for the full original amount.
                # This is simpler than splitting orders. The user sees they got *some* from stock via event log.
                # The "Accept" action means "Yes, we acknowledge this full demand."
                # Correction to align with user "if we have the item ... I should be able to accept them":
                # If any part is fulfilled from stock, this is a separate event. The production order itself (if remaining qty > 0)
                # becomes "Accepted" for the *remaining* quantity. If full qty fulfilled, order becomes "Fulfilled".

                # Let's re-evaluate the partial fulfillment:
                # If product is partially in stock:
                # 1. Fulfill that part.
                # 2. The existing ProductionOrder should now be for the *remaining* quantity.
                # This means `order.quantity` and `order.required_materials` must be updated BEFORE it's marked "Accepted".

                # Re-implementing partial fulfillment logic:
                order.quantity = new_quantity_for_production # quantity to be produced
                order.required_materials = new_required_materials
                # The order object 'order' now represents the part that needs production.
                # Update this order in DB with new quantity and materials BEFORE setting to Accepted.
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, 
                                       {"quantity": order.quantity, "required_materials": order.required_materials})
                # Now this modified order (for remaining qty) proceeds to be "Accepted"
                logger.info(f"Order {order_id} partially fulfilled. {fulfilled_from_stock_qty} from stock. Remaining {order.quantity} to be produced.")
        
        # If not fully fulfilled from stock (either partially or not at all), mark as "Accepted"
        # This part executes if:
        #   - No stock was available (fulfilled_from_stock_qty == 0)
        #   - Stock was partially available (and order.quantity was updated above)
        if order.status == "Pending": # Ensure it's still pending if it wasn't fully fulfilled
            order.status = "Accepted"
            # No material commitment here. No update to order.committed_materials.
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(include={"status"}))
            await self.log_sim_event("production_order_accepted", {
                "order_id": order.id, 
                "original_requested_quantity": original_order_quantity,
                "quantity_now_accepted_for_production": order.quantity,
                "fulfilled_directly_from_stock_qty": fulfilled_from_stock_qty
            })
            await crud.save_simulation_state(self.state) # Save state after inventory changes or status update
            message = f"Order {order.id} for {product_to_make.name} "
            if fulfilled_from_stock_qty > 0 and order.quantity > 0 : # partially fulfilled, rest accepted
                message += f"({fulfilled_from_stock_qty} fulfilled from stock, {order.quantity} accepted for production)."
            elif fulfilled_from_stock_qty == 0: # fully accepted for production
                 message += f"({order.quantity} accepted for production, no materials committed yet)."
            return True, message
        elif order.status == "Fulfilled": # Already handled above
            return True, f"Order {order.id} was fully fulfilled from stock." # Should have returned already
        
        return False, "Order status was not Pending initially or an issue occurred." # Should not happen

    async def place_purchase_order_for_shortages(self, production_order_id: str) -> Dict[str, str]:
        # This method remains largely the same; it checks physical stock vs required_materials
        # and orders the difference. "On Order" quantity is for UI display, not for this calculation directly.
        order = await self.get_production_order_async(production_order_id)
        if not order: return {"error": "Production order not found."}
        # Can be called for Pending or Accepted orders
        if order.status not in ["Pending", "Accepted"]: return {"error": f"Cannot order materials for order with status '{order.status}'."}
        product = self.products.get(order.product_id)
        if not product: return {"error": f"Product definition for {order.product_id} not found."}
        
        if not order.required_materials: # Should be populated by accept_production_order or _generate_random_orders
            calculated_req_materials = {}
            for bom_item in product.bom:
                calculated_req_materials[bom_item.material_id] = calculated_req_materials.get(bom_item.material_id, 0) + (bom_item.quantity * order.quantity)
            order.required_materials = calculated_req_materials
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, {"required_materials": order.required_materials})

        results = {}; materials_ordered_summary = []
        for mat_id, qty_needed_total in order.required_materials.items():
            current_physical_stock = self.state.inventory.get(mat_id, 0)
            shortage_quantity = qty_needed_total - current_physical_stock

            if shortage_quantity > 0:
                best_provider_id = None; min_price = float('inf')
                for prov_id, provider_obj in self.providers.items():
                    for offering in provider_obj.catalogue:
                        if offering.material_id == mat_id and offering.price_per_unit < min_price:
                            min_price = offering.price_per_unit; best_provider_id = prov_id
                if best_provider_id:
                    try:
                        po = await self.place_purchase_order(mat_id, best_provider_id, shortage_quantity)
                        results[mat_id] = f"Ordered {shortage_quantity} from {self.providers[best_provider_id].name} (PO: {po.id})."
                        materials_ordered_summary.append(f"{mat_id}: {shortage_quantity}")
                    except ValueError as e: results[mat_id] = f"Error: {str(e)}"
                else:
                    results[mat_id] = f"No provider for {mat_id}."
                    await self.log_sim_event("material_shortage_no_provider", {"order_id":order.id, "mat_id":mat_id, "needed":shortage_quantity})
            else:
                results[mat_id] = f"Sufficient physical stock ({current_physical_stock} for need of {qty_needed_total})."
        if materials_ordered_summary:
             await self.log_sim_event("auto_ordered_materials_for_prod_order", {"order_id":order.id, "ordered":materials_ordered_summary})
        await crud.save_simulation_state(self.state)
        return results

    async def run_day(self):
        self.state.current_day += 1
        current_day_offset = self.state.current_day
        logger.info(f"--- Starting Simulation Day {current_day_offset} ---")
        await self.log_sim_event("day_start", {"day": current_day_offset})
        await self._generate_random_orders(current_day_offset)

        pending_po_ids = self.state.pending_purchase_orders[:]
        current_sim_processing_datetime = SIMULATION_EPOCH_DATETIME + timedelta(days=current_day_offset)
        current_sim_processing_date = current_sim_processing_datetime.date()

        for po_id in pending_po_ids: # PO Arrivals
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
                    await self.log_sim_event("material_arrival", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered})
                 else:
                     await self.log_sim_event("arrival_delayed_storage", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered})

        completed_production_today = 0
        active_order_ids_today = self.state.active_production_orders[:]
        for order_id in active_order_ids_today: # Production Completion
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
                        await self.update_inventory(order.product_id, order.quantity, is_physical=True) # Product to physical
                        
                        # Consume materials from committed inventory
                        if order.committed_materials:
                            for mat_id, qty_consumed in order.committed_materials.items():
                                await self.update_inventory(mat_id, -qty_consumed, is_physical=False) # Decrease committed
                            # order.committed_materials can be cleared or kept for history. Let's keep for now.
                        
                        order.status = "Completed"; order.completed_at = current_sim_processing_datetime
                        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(exclude_none=True, include={"status", "completed_at"}))
                        if order_id in self.state.active_production_orders: self.state.active_production_orders.remove(order_id)
                        await self.log_sim_event("production_completed", {"order_id":order.id, "prod_id":order.product_id, "qty":order.quantity})
                        completed_production_today += 1
                    else:
                        await self.log_sim_event("production_delayed_capacity", {"order_id": order.id})
        
        await crud.save_simulation_state(self.state)
        logger.info(f"--- Ending Simulation Day {current_day_offset} ---")
        await self.log_sim_event("day_end", {"day": current_day_offset})
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
            logger.info(f"New demand: {requested_quantity}x {product_obj.name} (ID: {product_id}).")

            physical_stock = self.state.inventory.get(product_id, 0)
            fulfilled_from_stock = 0
            quantity_to_produce_if_needed = requested_quantity

            if physical_stock > 0:
                if physical_stock >= requested_quantity:
                    fulfilled_from_stock = requested_quantity
                    quantity_to_produce_if_needed = 0
                    await self.update_inventory(product_id, -requested_quantity, is_physical=True)
                    await self.log_sim_event("product_shipped_from_stock", {"product_id":product_id, "qty_shipped":requested_quantity, "demand_qty":requested_quantity, "source":"direct_demand"})
                    logger.info(f"Fulfilled demand for {requested_quantity}x {product_obj.name} from stock.")
                else: # physical_stock < requested_quantity
                    fulfilled_from_stock = physical_stock
                    quantity_to_produce_if_needed = requested_quantity - physical_stock
                    await self.update_inventory(product_id, -physical_stock, is_physical=True)
                    await self.log_sim_event("product_shipped_from_stock", {"product_id":product_id, "qty_shipped":physical_stock, "demand_qty":requested_quantity, "source":"direct_demand_partial"})
                    logger.info(f"Fulfilled {physical_stock}x {product_obj.name} from stock. Remainder {quantity_to_produce_if_needed} for production.")
            
            if quantity_to_produce_if_needed > 0:
                required_materials = {}
                for bom_item in product_obj.bom:
                    required_materials[bom_item.material_id] = required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * quantity_to_produce_if_needed)
                new_order = ProductionOrder(
                    id=utils.generate_id(), product_id=product_id, quantity=quantity_to_produce_if_needed,
                    requested_date=current_sim_datetime_for_request, status="Pending",
                    required_materials=required_materials, created_at=utils.get_current_utc_timestamp()
                )
                await crud.create_item(crud.COLLECTIONS["production_orders"], new_order.model_dump())
                await self.log_sim_event("order_received_for_production", {
                    "order_id": new_order.id, "product_id": product_id, "qty_for_prod": quantity_to_produce_if_needed,
                    "original_demand": requested_quantity, "fulfilled_stock": fulfilled_from_stock
                })
                logger.info(f"Prod Order {new_order.id} for {quantity_to_produce_if_needed}x {product_obj.name} created.")

    async def start_production(self, order_ids: List[str]) -> Dict[str, str]:
        # This is triggered when "Send to Production" is clicked for "Accepted" orders.
        # Material check and commit happens here.
        results = {}
        current_sim_datetime_for_start = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)
        for order_id in order_ids:
            order = await self.get_production_order_async(order_id)
            if not order: results[order_id] = "Order not found."; continue
            if order.status != "Accepted": results[order_id] = f"Order status is '{order.status}', must be 'Accepted'."; continue
            
            product = self.products.get(order.product_id)
            if not product: results[order_id] = f"Product {order.product_id} not found."; continue

            # Check physical material availability for order.required_materials
            can_start = True
            materials_to_commit_now = {}
            if not order.required_materials: # Should have been calculated
                 results[order_id] = f"Order {order_id} has no required materials defined. Please check order details."
                 continue

            for mat_id, qty_needed in order.required_materials.items():
                physical_qty_available = self.state.inventory.get(mat_id, 0)
                if physical_qty_available < qty_needed:
                    can_start = False
                    results[order_id] = f"Insufficient material {mat_id} for order {order_id}. Need: {qty_needed}, Have (Phys): {physical_qty_available}."
                    await self.log_sim_event("production_start_failed_material_shortage", {"order_id": order.id, "material_id": mat_id, "needed": qty_needed, "available": physical_qty_available})
                    break # Stop checking for this order
            
            if not can_start:
                continue # Move to the next order_id

            # If all materials available, commit them from physical to committed inventory
            for mat_id, qty_needed in order.required_materials.items():
                await self.update_inventory(mat_id, -qty_needed, is_physical=True)  # Decrease physical
                await self.update_inventory(mat_id, qty_needed, is_physical=False) # Increase committed
                materials_to_commit_now[mat_id] = qty_needed
            
            order.committed_materials = materials_to_commit_now # Store what was just committed
            order.status = "In Progress"
            order.started_at = current_sim_datetime_for_start
            
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, 
                                   order.model_dump(include={"status", "started_at", "committed_materials"}))
            
            if order.id not in self.state.active_production_orders: 
                self.state.active_production_orders.append(order.id)
            
            results[order_id] = "Production started successfully. Materials committed."
            await self.log_sim_event("production_started_and_materials_committed", {
                "order_id": order.id, "product_id": order.product_id, 
                "committed_materials": order.committed_materials
            })

        await crud.save_simulation_state(self.state) # Save state after processing all selected orders
        return results

    async def place_purchase_order(self, material_id: str, provider_id: str, quantity: int) -> PurchaseOrder:
        provider = self.providers.get(provider_id); material = self.materials.get(material_id)
        if not provider or not material: raise ValueError("Invalid provider or material ID.")
        offering = next((o for o in provider.catalogue if o.material_id == material_id), None)
        if not offering: raise ValueError(f"Provider {provider.name} does not offer {material.name}.")
        
        order_timestamp = utils.get_current_utc_timestamp()
        sim_day_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()
        expected_arrival_dt = datetime.combine(sim_day_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=offering.lead_time_days)

        po = PurchaseOrder(id=utils.generate_id(), material_id=material_id, provider_id=provider_id, 
                           quantity_ordered=quantity, order_date=order_timestamp, 
                           expected_arrival_date=expected_arrival_dt, status="Ordered", created_at=order_timestamp)
        po_dict = await crud.create_item(crud.COLLECTIONS["purchase_orders"], po.model_dump())
        if po.id not in self.state.pending_purchase_orders: self.state.pending_purchase_orders.append(po.id)
        await self.log_sim_event("purchase_order_placed", {"po_id": po.id, "mat_id": material_id, "prov_id": provider_id, "qty": quantity, "eta": expected_arrival_dt.isoformat()})
        logger.info(f"Placed PO {po.id} for {quantity}x {material.name}. ETA: {expected_arrival_dt.isoformat()}")
        return PurchaseOrder(**po_dict)