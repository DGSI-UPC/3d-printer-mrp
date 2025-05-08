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
        # Ensure committed_inventory is initialized
        if not hasattr(self.state, 'committed_inventory') or self.state.committed_inventory is None:
            self.state.committed_inventory = {}

        self.products = {p.id: p for p in products}
        self.materials = {m.id: m for m in materials}
        self.providers = {p.id: p for p in providers}
        self.config = config
        self.production_capacity_resource = simpy.Resource(self.env, capacity=self.state.daily_production_capacity)
        # self.production_processes = {} # These seem to be managed differently now, can be removed if not used by simpy processes
        # self.arrival_processes = {}   # Same as above

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
        # Considers only physical inventory for storage capacity
        return sum(self.state.inventory.values())

    async def check_storage_capacity(self, adding_quantity: int) -> bool:
        # Checks against physical inventory for storage capacity
        return (self.get_total_inventory_units() + adding_quantity) <= self.state.storage_capacity

    async def update_inventory(self, item_id: str, quantity_change: int, is_physical: bool = True):
        """Updates physical or committed inventory."""
        target_inventory = self.state.inventory if is_physical else self.state.committed_inventory
        
        current_qty = target_inventory.get(item_id, 0)
        new_qty = max(0, current_qty + quantity_change) # Ensure quantity doesn't go below zero
        target_inventory[item_id] = new_qty

        log_details = {
            "item_id": item_id,
            "change": quantity_change,
            "new_quantity": new_qty,
            "inventory_type": "physical" if is_physical else "committed"
        }
        await self.log_sim_event("inventory_change", log_details)
        # No need to save state here, should be saved by the calling method or at end of day
        # await crud.save_simulation_state(self.state) # Avoid multiple saves

    async def get_production_order_async(self, order_id: str) -> Optional[ProductionOrder]:
        order_dict = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
        return ProductionOrder(**order_dict) if order_dict else None

    async def get_purchase_order_async(self, po_id: str) -> Optional[PurchaseOrder]:
        po_dict = await crud.get_item_by_id(crud.COLLECTIONS["purchase_orders"], po_id)
        return PurchaseOrder(**po_dict) if po_dict else None

    async def accept_production_order(self, order_id: str) -> Tuple[bool, str]:
        """
        Attempts to move a 'Pending' production order to 'Accepted' status.
        This involves checking material availability and committing materials.
        """
        order = await self.get_production_order_async(order_id)
        if not order:
            return False, "Production order not found."
        if order.status != "Pending":
            return False, f"Order status is '{order.status}', not 'Pending'."

        product = self.products.get(order.product_id)
        if not product:
            return False, f"Product definition for {order.product_id} not found."

        # Ensure required_materials is populated
        if not order.required_materials:
            calculated_req_materials = {}
            for bom_item in product.bom:
                calculated_req_materials[bom_item.material_id] = calculated_req_materials.get(bom_item.material_id, 0) + (bom_item.quantity * order.quantity)
            order.required_materials = calculated_req_materials
            # Persist this calculation to the order if it was missing
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, {"required_materials": order.required_materials})


        materials_to_commit = order.required_materials
        can_commit_all = True
        shortages = {}

        for mat_id, qty_needed in materials_to_commit.items():
            physical_qty_available = self.state.inventory.get(mat_id, 0)
            if physical_qty_available < qty_needed:
                can_commit_all = False
                shortages[mat_id] = {"needed": qty_needed, "available": physical_qty_available}
        
        if not can_commit_all:
            await self.log_sim_event("order_acceptance_failed_material_shortage", {"order_id": order.id, "shortages": shortages})
            return False, f"Insufficient materials to accept order. Shortages: {shortages}"

        # If all materials available, commit them
        committed_for_this_order = {}
        for mat_id, qty_needed in materials_to_commit.items():
            await self.update_inventory(mat_id, -qty_needed, is_physical=True) # Decrease physical
            await self.update_inventory(mat_id, qty_needed, is_physical=False) # Increase committed
            committed_for_this_order[mat_id] = qty_needed
        
        order.status = "Accepted"
        order.committed_materials = committed_for_this_order
        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(include={"status", "committed_materials"}))
        
        await self.log_sim_event("production_order_accepted", {"order_id": order.id, "committed_materials": committed_for_this_order})
        await crud.save_simulation_state(self.state) # Save state after successful commit
        return True, "Production order accepted and materials committed."

    async def place_purchase_order_for_shortages(self, production_order_id: str) -> Dict[str, str]:
        """
        Identifies material shortages for a given production order (Pending or Accepted)
        and places purchase orders for them from the cheapest provider.
        """
        order = await self.get_production_order_async(production_order_id)
        if not order:
            return {"error": "Production order not found."}
        if order.status not in ["Pending", "Accepted"]:
            return {"error": f"Cannot order materials for order with status '{order.status}'."}

        product = self.products.get(order.product_id)
        if not product:
            return {"error": f"Product definition for {order.product_id} not found."}
        
        if not order.required_materials: # Ensure it's populated
            calculated_req_materials = {}
            for bom_item in product.bom:
                calculated_req_materials[bom_item.material_id] = calculated_req_materials.get(bom_item.material_id, 0) + (bom_item.quantity * order.quantity)
            order.required_materials = calculated_req_materials
            # Update order in DB if it was missing
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, {"required_materials": order.required_materials})


        results = {}
        materials_ordered_summary = []

        for mat_id, qty_needed_total in order.required_materials.items():
            # For "Accepted" orders, check against committed. For "Pending", check against physical.
            # However, the request implies checking against physical warehouse stock *before* accepting.
            # This function is for ordering if stock is insufficient *for this order's needs*.
            # So, we always check current physical inventory vs the order's total requirement.
            
            current_physical_stock = self.state.inventory.get(mat_id, 0)
            shortage_quantity = qty_needed_total - current_physical_stock

            if shortage_quantity > 0:
                # Find the cheapest provider for this material
                best_provider_id = None
                min_price = float('inf')
                
                for prov_id, provider_obj in self.providers.items():
                    for offering in provider_obj.catalogue:
                        if offering.material_id == mat_id and offering.price_per_unit < min_price:
                            min_price = offering.price_per_unit
                            best_provider_id = prov_id
                
                if best_provider_id:
                    try:
                        po = await self.place_purchase_order(mat_id, best_provider_id, shortage_quantity)
                        results[mat_id] = f"Ordered {shortage_quantity} units from {self.providers[best_provider_id].name} (PO: {po.id})."
                        materials_ordered_summary.append(f"{mat_id}: {shortage_quantity} units")
                    except ValueError as e:
                        results[mat_id] = f"Error ordering {mat_id}: {str(e)}"
                else:
                    results[mat_id] = f"No provider found for material {mat_id}."
                    await self.log_sim_event("material_shortage_no_provider", {"order_id": order.id, "material_id": mat_id, "quantity_needed": shortage_quantity})
            else:
                results[mat_id] = f"Sufficient stock available ({current_physical_stock} units)."

        if materials_ordered_summary:
             await self.log_sim_event("auto_ordered_materials_for_production", {"order_id": order.id, "materials_ordered": materials_ordered_summary})
        await crud.save_simulation_state(self.state) # Save state after POs
        return results

    async def run_day(self):
        self.state.current_day += 1
        current_day_offset = self.state.current_day
        logger.info(f"--- Starting Simulation Day {current_day_offset} ---")
        await self.log_sim_event("day_start", {"day": current_day_offset})

        await self._generate_random_orders(current_day_offset)

        # Process Purchase Order Arrivals
        pending_po_ids = self.state.pending_purchase_orders[:]
        current_sim_processing_datetime = SIMULATION_EPOCH_DATETIME + timedelta(days=current_day_offset)
        current_sim_processing_date = current_sim_processing_datetime.date()

        for po_id in pending_po_ids:
            po = await self.get_purchase_order_async(po_id)
            if not po:
                logger.warning(f"Pending PO {po_id} not found in DB, removing from state.")
                if po_id in self.state.pending_purchase_orders: self.state.pending_purchase_orders.remove(po_id)
                continue
            
            expected_arrival_date_val = po.expected_arrival_date.date()

            if po.status == "Ordered" and expected_arrival_date_val <= current_sim_processing_date:
                 if await self.check_storage_capacity(po.quantity_ordered): # Checks physical storage
                    logger.info(f"[Day {current_day_offset}] Processing arrival for PO {po_id} (Material: {po.material_id}, Qty: {po.quantity_ordered}).")
                    await self.update_inventory(po.material_id, po.quantity_ordered, is_physical=True) # Add to physical
                    po.status = "Arrived"
                    po.actual_arrival_date = current_sim_processing_datetime
                    po.units_received = po.quantity_ordered
                    await crud.update_item(crud.COLLECTIONS["purchase_orders"], po.id, po.model_dump(exclude_none=True))
                    if po_id in self.state.pending_purchase_orders: self.state.pending_purchase_orders.remove(po_id)
                    await self.log_sim_event("material_arrival", {"po_id": po.id, "material_id": po.material_id, "quantity": po.quantity_ordered})
                 else:
                     logger.warning(f"[Day {current_day_offset}] PO {po_id} arrival delayed - insufficient storage capacity.")
                     await self.log_sim_event("arrival_delayed_storage", {"po_id": po.id, "material_id": po.material_id, "quantity": po.quantity_ordered})

        # Process Production Completion
        completed_production_today = 0
        active_order_ids_today = self.state.active_production_orders[:] # Orders currently "In Progress"

        for order_id in active_order_ids_today:
            order = await self.get_production_order_async(order_id)
            if not order or order.status != "In Progress":
                logger.warning(f"Active order {order_id} issue. Status: {order.status if order else 'Not Found'}. Removing from active list.")
                if order_id in self.state.active_production_orders: self.state.active_production_orders.remove(order_id)
                continue

            product = self.products.get(order.product_id)
            if not product:
                 logger.error(f"Product {order.product_id} for active order {order_id} not found.")
                 continue

            if order.started_at:
                started_at_aware = order.started_at
                if started_at_aware.tzinfo is None: started_at_aware = started_at_aware.replace(tzinfo=timezone.utc)
                
                days_in_production = (current_sim_processing_datetime.date() - started_at_aware.date()).days

                if days_in_production >= product.production_time:
                    if completed_production_today < self.state.daily_production_capacity:
                        logger.info(f"[Day {current_day_offset}] Production completed for order {order_id} (Product: {order.product_id}, Qty: {order.quantity}).")
                        await self.update_inventory(order.product_id, order.quantity, is_physical=True) # Add product to physical inventory
                        order.status = "Completed"
                        order.completed_at = current_sim_processing_datetime
                        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(exclude_none=True))
                        if order_id in self.state.active_production_orders: self.state.active_production_orders.remove(order_id)
                        await self.log_sim_event("production_completed", {"order_id": order.id, "product_id": order.product_id, "quantity": order.quantity})
                        completed_production_today += 1
                    else:
                        logger.warning(f"[Day {current_day_offset}] Order {order_id} finished production but completion delayed due to daily capacity limit.")
                        await self.log_sim_event("production_delayed_capacity", {"order_id": order.id})

        await crud.save_simulation_state(self.state)
        logger.info(f"--- Ending Simulation Day {current_day_offset} ---")
        await self.log_sim_event("day_end", {"day": current_day_offset})
        return self.state

    async def _generate_random_orders(self, current_day_offset: int):
        cfg = self.config.get("random_order_config", {})
        num_orders = random.randint(cfg.get("min_orders_per_day", 0), cfg.get("max_orders_per_day", 2))
        product_ids = list(self.products.keys())

        if not product_ids:
            logger.warning("No products defined, cannot generate random orders.")
            return

        current_sim_datetime_for_request = SIMULATION_EPOCH_DATETIME + timedelta(days=current_day_offset)

        for _ in range(num_orders):
            product_id = random.choice(product_ids)
            quantity = random.randint(cfg.get("min_qty_per_order", 1), cfg.get("max_qty_per_order", 5))
            product = self.products[product_id]

            # Calculate required materials for the new order
            required_materials_for_order = {}
            for bom_item in product.bom:
                required_materials_for_order[bom_item.material_id] = \
                    required_materials_for_order.get(bom_item.material_id, 0) + (bom_item.quantity * quantity)

            new_order = ProductionOrder(
                id=utils.generate_id(),
                product_id=product_id,
                quantity=quantity,
                requested_date=current_sim_datetime_for_request,
                status="Pending", # New orders start as Pending
                required_materials=required_materials_for_order, # Store calculated materials
                created_at=utils.get_current_utc_timestamp()
            )
            await crud.create_item(crud.COLLECTIONS["production_orders"], new_order.model_dump())
            await self.log_sim_event("order_received", {"order_id": new_order.id, "product_id": product_id, "quantity": quantity, "requested_date_iso": new_order.requested_date.isoformat()})
            logger.info(f"[Day {current_day_offset}] Generated random order {new_order.id} for {quantity}x {product.name}")

    async def start_production(self, order_ids: List[str]) -> Dict[str, str]:
        """
        Attempts to start production for 'Accepted' orders.
        This now consumes materials from 'committed_inventory'.
        """
        results = {}
        current_sim_datetime_for_start = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)

        for order_id in order_ids:
            order = await self.get_production_order_async(order_id)
            if not order:
                results[order_id] = "Order not found."
                continue

            if order.status != "Accepted": # Now expects "Accepted" status
                results[order_id] = f"Order status is '{order.status}', not 'Accepted'."
                continue

            product = self.products.get(order.product_id)
            if not product:
                 results[order_id] = f"Product {order.product_id} definition not found."
                 continue

            # Materials should already be in order.committed_materials and state.committed_inventory
            if not order.committed_materials:
                results[order_id] = f"Order {order_id} has no committed materials. Internal error or order not properly accepted."
                await self.log_sim_event("production_start_failed_no_commit", {"order_id": order.id})
                continue

            # Verify committed materials in state match order's committed materials (sanity check)
            can_start = True
            for mat_id, qty_needed in order.committed_materials.items():
                if self.state.committed_inventory.get(mat_id, 0) < qty_needed:
                    can_start = False
                    results[order_id] = f"Mismatch or shortage in committed inventory for material {mat_id}. (Need: {qty_needed}, Committed: {self.state.committed_inventory.get(mat_id, 0)}). Critical error."
                    await self.log_sim_event("production_start_failed_commit_mismatch", {"order_id": order.id, "material_id": mat_id, "needed": qty_needed, "state_committed": self.state.committed_inventory.get(mat_id, 0)})
                    break
            
            if not can_start:
                continue

            # Consume from committed_inventory
            for mat_id, qty_consumed in order.committed_materials.items():
                await self.update_inventory(mat_id, -qty_consumed, is_physical=False) # Decrease committed

            order.status = "In Progress"
            order.started_at = current_sim_datetime_for_start
            # Clear committed_materials on the order as they are now consumed for this production
            # order.committed_materials = {} # Or keep for history? Let's keep for now.
            await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump(include={"status", "started_at"}))
            
            if order.id not in self.state.active_production_orders: # Ensure no duplicates
                 self.state.active_production_orders.append(order.id)

            results[order_id] = "Production started successfully (materials consumed from committed stock)."
            await self.log_sim_event("production_started", {"order_id": order.id, "product_id": order.product_id, "consumed_from_committed": order.committed_materials})

        await crud.save_simulation_state(self.state)
        return results

    async def place_purchase_order(self, material_id: str, provider_id: str, quantity: int) -> PurchaseOrder:
        provider = self.providers.get(provider_id)
        material = self.materials.get(material_id)
        if not provider or not material:
            raise ValueError("Invalid provider or material ID.")

        offering = next((o for o in provider.catalogue if o.material_id == material_id), None)
        if not offering:
            raise ValueError(f"Provider {provider.name} does not offer material {material.name}.")
        
        # Ensure quantity respects offered_unit_size if it's ever > 1 (not currently, but good practice)
        if quantity % offering.offered_unit_size != 0 :
            # For now, let's assume we round up or reject. Here, we'll implicitly handle it by ordering what's asked.
            # A real system might enforce multiples of offered_unit_size.
            pass


        order_timestamp = utils.get_current_utc_timestamp()
        current_sim_date_for_order = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()
        order_date_start_of_sim_day = datetime.combine(current_sim_date_for_order, datetime.min.time(), tzinfo=timezone.utc)
        expected_arrival_datetime = order_date_start_of_sim_day + timedelta(days=offering.lead_time_days)

        po = PurchaseOrder(
            id=utils.generate_id(),
            material_id=material_id,
            provider_id=provider_id,
            quantity_ordered=quantity,
            order_date=order_timestamp, # Actual time of PO creation via API
            expected_arrival_date=expected_arrival_datetime, # Based on sim day + lead time
            status="Ordered",
            created_at=order_timestamp
        )
        po_dict = await crud.create_item(crud.COLLECTIONS["purchase_orders"], po.model_dump())

        if po.id not in self.state.pending_purchase_orders: # Ensure no duplicates
            self.state.pending_purchase_orders.append(po.id)
        # No direct save_simulation_state here, usually called by the main function (e.g. create_purchase_order API or place_purchase_order_for_shortages)

        await self.log_sim_event("purchase_order_placed", {
            "po_id": po.id,
            "material_id": material_id,
            "provider_id": provider_id,
            "quantity": quantity,
            "expected_arrival": expected_arrival_datetime.isoformat()
        })
        logger.info(f"[Day {self.state.current_day}] Placed Purchase Order {po.id} for {quantity}x {material.name} from {provider.name}. Expected arrival: {expected_arrival_datetime.isoformat()}")
        return PurchaseOrder(**po_dict)