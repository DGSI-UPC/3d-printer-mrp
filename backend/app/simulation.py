from typing import Dict, List, Tuple, Optional
import simpy
import random
from loguru import logger
from datetime import datetime, timedelta, timezone, date

from .models import (
    SimulationState, ProductionOrder, PurchaseOrder, SimulationEvent,
    Product, Material, Provider, InitialConditions,
    ItemForecastResponse, DailyForecast # Added new models
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

        original_order_quantity = order.quantity
        fulfilled_from_stock_qty = 0
        quantity_to_produce = original_order_quantity

        physical_stock_of_product = self.state.inventory.get(order.product_id, 0)

        if physical_stock_of_product > 0:
            if physical_stock_of_product >= quantity_to_produce:
                await self.update_inventory(order.product_id, -quantity_to_produce, is_physical=True)
                order.status = "Fulfilled"
                order.completed_at = utils.get_current_utc_timestamp()
                fulfilled_from_stock_qty = quantity_to_produce
                quantity_to_produce = 0
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                       order.model_dump(include={"status", "completed_at", "quantity"}))
                await self.log_sim_event("production_order_fulfilled_from_stock", {
                    "order_id": order.id, "product_id": order.product_id,
                    "quantity_fulfilled": fulfilled_from_stock_qty
                })
                await crud.save_simulation_state(self.state)
                return True, f"Order {order.id} for {original_order_quantity}x {product_to_make.name} fulfilled directly from stock."
            else: 
                await self.update_inventory(order.product_id, -physical_stock_of_product, is_physical=True)
                fulfilled_from_stock_qty = physical_stock_of_product
                quantity_to_produce = original_order_quantity - fulfilled_from_stock_qty
                order.quantity = quantity_to_produce 

                new_required_materials = {}
                for bom_item in product_to_make.bom:
                    new_required_materials[bom_item.material_id] = \
                        new_required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * quantity_to_produce)
                order.required_materials = new_required_materials
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                                       {"quantity": order.quantity, "required_materials": order.required_materials})
                await self.log_sim_event("production_order_partially_fulfilled_from_stock", {
                    "order_id": order.id, "product_id": order.product_id,
                    "quantity_from_stock": fulfilled_from_stock_qty,
                    "remaining_quantity_for_production": quantity_to_produce
                })
                logger.info(f"Order {order_id} partially fulfilled. {fulfilled_from_stock_qty} from stock. Remaining {quantity_to_produce} for production.")

        if quantity_to_produce > 0:
            if not order.required_materials:
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
                message = f"Order {order.id} for {product_to_make.name} ({quantity_to_produce} units) cannot be accepted. {insufficient_material_details}"
                if fulfilled_from_stock_qty > 0:
                    message = f"{fulfilled_from_stock_qty} units of {product_to_make.name} fulfilled from stock. Remaining {quantity_to_produce} units for Order {order.id} cannot be accepted. {insufficient_material_details}"
                return False, message

            order.committed_materials.clear() 
            for mat_id, qty_to_commit in materials_to_commit_upon_acceptance.items():
                await self.update_inventory(mat_id, -qty_to_commit, is_physical=True)
                await self.update_inventory(mat_id, qty_to_commit, is_physical=False) 
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
            message = f"Order {order.id} for {product_to_make.name} ({quantity_to_produce} units) accepted. Materials committed."
            if fulfilled_from_stock_qty > 0:
                 message = f"{fulfilled_from_stock_qty} units of {product_to_make.name} fulfilled from stock. Remaining {quantity_to_produce} units for Order {order.id} accepted and materials committed."
            return True, message

        return False, "Order processing error."

    async def place_purchase_order_for_shortages(self, production_order_id: str) -> Dict[str, str]:
        order = await self.get_production_order_async(production_order_id)
        if not order: return {"error": "Production order not found."}
        if order.status not in ["Pending", "Accepted"]:
            return {"error": f"Cannot order materials for order with status '{order.status}'."}
        
        product = self.products.get(order.product_id)
        if not product: return {"error": f"Product definition for {order.product_id} not found."}

        if not order.required_materials:
            calculated_req_materials = {}
            for bom_item in product.bom:
                calculated_req_materials[bom_item.material_id] = \
                    calculated_req_materials.get(bom_item.material_id, 0) + (bom_item.quantity * order.quantity)
            order.required_materials = calculated_req_materials
            # No need to save to DB here, it's an in-memory modification for this function's scope
            # await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, {"required_materials": order.required_materials})


        results = {}
        materials_ordered_summary = []
        for mat_id, qty_needed_for_order in order.required_materials.items():
            current_physical_stock = self.state.inventory.get(mat_id, 0)
            total_committed_globally = self.state.committed_inventory.get(mat_id, 0)
            
            actual_need_to_source_for_this_mat_line = qty_needed_for_order
            
            # If the order is "Accepted", we need to figure out how much *more* is needed beyond what was already committed for it.
            # And the available physical should be reduced by commitments to *other* orders.
            committed_for_this_order_already = 0
            if order.status == "Accepted":
                committed_for_this_order_already = order.committed_materials.get(mat_id, 0)
                
            # The portion of total_committed_globally that is *not* for the current order (if it's accepted)
            committed_elsewhere = total_committed_globally - committed_for_this_order_already
            
            # Effective physical stock available for this specific need (before committing this order or for this order's additional need)
            effective_available_physical = current_physical_stock - committed_elsewhere
            
            # If order is "Accepted", we're interested in the shortfall beyond what's already committed TO IT.
            if order.status == "Accepted":
                actual_need_to_source_for_this_mat_line = qty_needed_for_order - committed_for_this_order_already

            if actual_need_to_source_for_this_mat_line <= 0: # Already fully covered if accepted, or not needed
                results[mat_id] = f"No additional sourcing needed for {self.materials.get(mat_id, Material(id=mat_id, name=mat_id)).name} (Needed for this action: {actual_need_to_source_for_this_mat_line})."
                continue

            shortage_quantity = actual_need_to_source_for_this_mat_line - effective_available_physical

            if shortage_quantity > 0:
                best_provider_id = None
                min_price = float('inf')
                material_obj = self.materials.get(mat_id)
                if not material_obj:
                    results[mat_id] = f"Material definition for {mat_id} not found."
                    continue

                for prov_id, provider_obj in self.providers.items():
                    for offering in provider_obj.catalogue:
                        if offering.material_id == mat_id and offering.price_per_unit < min_price:
                            min_price = offering.price_per_unit
                            best_provider_id = prov_id
                
                if best_provider_id:
                    try:
                        # Order the calculated shortage_quantity
                        po = await self.place_purchase_order(mat_id, best_provider_id, shortage_quantity)
                        results[mat_id] = f"Ordered {shortage_quantity} of {material_obj.name} from {self.providers[best_provider_id].name} (PO: {po.id})."
                        materials_ordered_summary.append(f"{material_obj.name}: {shortage_quantity}")
                    except ValueError as e:
                        results[mat_id] = f"Error placing PO for {material_obj.name}: {str(e)}"
                else:
                    results[mat_id] = f"No provider found for {material_obj.name}."
                    await self.log_sim_event("material_shortage_no_provider", {
                        "order_id": order.id, "mat_id": mat_id, "needed": shortage_quantity
                    })
            else:
                results[mat_id] = f"Sufficient effective stock for {self.materials.get(mat_id, Material(id=mat_id, name=mat_id)).name}. Need to source: {actual_need_to_source_for_this_mat_line}, Effective Available: {effective_available_physical}."
        
        if materials_ordered_summary:
             await self.log_sim_event("auto_ordered_materials_for_prod_order", {
                 "order_id": order.id, "ordered_summary": materials_ordered_summary
            })
        # No explicit save_simulation_state here, as place_purchase_order saves it if a PO is made.
        # If no POs are made, state might not need saving from this specific action.
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
                    await self.log_sim_event("material_arrival", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered})
                 else:
                     await self.log_sim_event("arrival_delayed_storage", {"po_id":po.id, "mat_id":po.material_id, "qty":po.quantity_ordered})

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
                        if order.committed_materials:
                            for mat_id, qty_consumed in order.committed_materials.items():
                                await self.update_inventory(mat_id, -qty_consumed, is_physical=False)
                        await self.update_inventory(order.product_id, order.quantity, is_physical=True)

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

            required_materials = {}
            for bom_item in product_obj.bom:
                required_materials[bom_item.material_id] = required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * requested_quantity)

            new_order = ProductionOrder(
                id=utils.generate_id(), product_id=product_id, quantity=requested_quantity,
                requested_date=current_sim_datetime_for_request, status="Pending",
                required_materials=required_materials, created_at=utils.get_current_utc_timestamp()
            )
            await crud.create_item(crud.COLLECTIONS["production_orders"], new_order.model_dump())
            await self.log_sim_event("order_received_for_production", { 
                "order_id": new_order.id, "product_id": product_id, "qty_for_prod": requested_quantity,
                "original_demand": requested_quantity, "fulfilled_stock": 0
            })
            logger.info(f"Prod Order {new_order.id} for {requested_quantity}x {product_obj.name} created as 'Pending'.")


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

        await self.update_inventory(order.product_id, -order.quantity, is_physical=True)

        if order.committed_materials:
            for mat_id, qty_committed in order.committed_materials.items():
                await self.update_inventory(mat_id, qty_committed, is_physical=True) 
                await self.update_inventory(mat_id, -qty_committed, is_physical=False)
            await self.log_sim_event("materials_uncommitted_for_fulfillment", {
                "order_id": order.id, "uncommitted_materials": order.committed_materials
            })
        order.committed_materials.clear() 

        order.status = "Fulfilled"
        order.completed_at = utils.get_current_utc_timestamp()

        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id,
                               order.model_dump(include={"status", "completed_at", "committed_materials"}))

        await self.log_sim_event("accepted_order_fulfilled_from_stock", {
            "order_id": order.id, "product_id": order.product_id,
            "quantity_fulfilled": order.quantity
        })
        await crud.save_simulation_state(self.state)
        return True, f"Accepted order {order.id} for {order.quantity}x {product_to_fulfill.name} fulfilled from stock. Materials uncommitted."

    async def place_purchase_order(self, material_id: str, provider_id: str, quantity: int) -> PurchaseOrder:
        provider = self.providers.get(provider_id); material = self.materials.get(material_id)
        if not provider or not material: raise ValueError("Invalid provider or material ID.")
        offering = next((o for o in provider.catalogue if o.material_id == material_id), None)
        if not offering: raise ValueError(f"Provider {provider.name} does not offer {material.name}.")

        if quantity <= 0:
            raise ValueError(f"Purchase order quantity must be positive. Attempted to order {quantity} of {material.name}.")


        order_timestamp = utils.get_current_utc_timestamp()
        sim_day_date = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()
        expected_arrival_dt = datetime.combine(sim_day_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=offering.lead_time_days)

        po = PurchaseOrder(id=utils.generate_id(), material_id=material_id, provider_id=provider_id,
                           quantity_ordered=quantity, order_date=order_timestamp,
                           expected_arrival_date=expected_arrival_dt, status="Ordered", created_at=order_timestamp)
        
        # Ensure state is saved *after* PO is created and added to pending list
        if po.id not in self.state.pending_purchase_orders: # Should always be true for new PO
            self.state.pending_purchase_orders.append(po.id)
        
        # Save state before creating item in DB, or ensure created_item includes all necessary fields for PO obj
        # Let's save state first, then create the PO item in DB
        await crud.save_simulation_state(self.state) 
        
        po_dict = await crud.create_item(crud.COLLECTIONS["purchase_orders"], po.model_dump())


        await self.log_sim_event("purchase_order_placed", {"po_id": po.id, "mat_id": material_id, "prov_id": provider_id, "qty": quantity, "eta": expected_arrival_dt.isoformat()})
        logger.info(f"Placed PO {po.id} for {quantity}x {material.name}. ETA: {expected_arrival_dt.isoformat()}")
        
        # It's safer to reconstruct from the dict returned by CRUD to ensure all DB fields are there
        return PurchaseOrder(**po_dict) if po_dict else po


    async def get_item_forecast(self, item_id: str, num_days: int, historical_lookback_days: int = 0) -> ItemForecastResponse:
        if item_id in self.materials:
            item_type = "Material"
            item_name = self.materials[item_id].name
        elif item_id in self.products:
            item_type = "Product"
            item_name = self.products[item_id].name
        else:
            raise ValueError(f"Item ID {item_id} not found as a material or product.")

        current_sim_datetime = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)
        current_sim_date = current_sim_datetime.date()
        
        physical_stock = self.state.inventory.get(item_id, 0)
        daily_deltas = [0.0] * num_days 

        forecast_list: List[DailyForecast] = []

        if historical_lookback_days > 0:
            for i in range(historical_lookback_days):
                day_offset_val = -historical_lookback_days + i 
                actual_day_number_eod = self.state.current_day + day_offset_val 
                forecast_dt = current_sim_date + timedelta(days=day_offset_val)
                
                qty_for_this_hist_day = 0.0 

                if actual_day_number_eod == self.state.current_day - 1:
                    qty_for_this_hist_day = float(physical_stock)
                elif actual_day_number_eod < self.state.current_day - 1:
                    latest_event_raw = await crud.get_items(
                        crud.COLLECTIONS["events"],
                        query={
                            "event_type": "inventory_change",
                            "details.item_id": item_id,
                            "details.inventory_type": "physical", 
                            "day": {"$lte": actual_day_number_eod} 
                        },
                        sort_field="timestamp", 
                        sort_order=-1, 
                        limit=1
                    )
                    if latest_event_raw:
                        qty_for_this_hist_day = float(latest_event_raw[0].get("details",{}).get("new_quantity", 0.0))
                
                forecast_list.append(DailyForecast(day_offset=day_offset_val, date=forecast_dt, quantity=qty_for_this_hist_day))

        if item_type == "Material":
            pending_pos_dicts = await crud.get_items(
                crud.COLLECTIONS["purchase_orders"],
                {"status": "Ordered", "material_id": item_id},
                limit=None 
            )
            for po_dict in pending_pos_dicts:
                po = PurchaseOrder(**po_dict)
                arrival_offset = (po.expected_arrival_date.date() - current_sim_date).days
                if 0 <= arrival_offset < num_days:
                    daily_deltas[arrival_offset] += po.quantity_ordered
            
            production_orders_consuming_material = await crud.get_items(
                crud.COLLECTIONS["production_orders"],
                {"status": "In Progress"}, 
                limit=None
            )

            for prod_o_dict in production_orders_consuming_material:
                prod_o = ProductionOrder(**prod_o_dict)
                product_def = self.products.get(prod_o.product_id)

                if not product_def or not prod_o.started_at:
                    continue

                total_material_needed_for_order = prod_o.committed_materials.get(item_id, 0)
                if total_material_needed_for_order == 0:
                    continue

                prod_o_started_at_date = prod_o.started_at.date()
                production_duration_days = product_def.production_time
                base_start_offset_from_forecast_start = (prod_o_started_at_date - current_sim_date).days

                if production_duration_days <= 0: production_duration_days = 1 

                if production_duration_days == 1:
                    consumption_forecast_offset = base_start_offset_from_forecast_start
                    if 0 <= consumption_forecast_offset < num_days:
                        daily_deltas[consumption_forecast_offset] -= total_material_needed_for_order
                else:
                    if total_material_needed_for_order == 1:
                        middle_day_of_production_cycle = (production_duration_days - 1) // 2 
                        consumption_forecast_offset = base_start_offset_from_forecast_start + middle_day_of_production_cycle
                        if 0 <= consumption_forecast_offset < num_days:
                            daily_deltas[consumption_forecast_offset] -= 1.0
                    else:
                        daily_consumption_schedule = [0.0] * production_duration_days
                        for i in range(int(total_material_needed_for_order)): 
                            day_index_in_cycle = i % production_duration_days
                            daily_consumption_schedule[day_index_in_cycle] += 1.0
                        
                        for day_in_cycle_idx, qty_consumed_on_cycle_day in enumerate(daily_consumption_schedule):
                            if qty_consumed_on_cycle_day > 0:
                                consumption_forecast_offset = base_start_offset_from_forecast_start + day_in_cycle_idx
                                if 0 <= consumption_forecast_offset < num_days:
                                    daily_deltas[consumption_forecast_offset] -= qty_consumed_on_cycle_day
        
        elif item_type == "Product":
            in_progress_orders_dicts = await crud.get_items(
                crud.COLLECTIONS["production_orders"],
                {"status": "In Progress", "product_id": item_id},
                limit=None
            )
            for prod_o_dict in in_progress_orders_dicts:
                prod_o = ProductionOrder(**prod_o_dict)
                product_def = self.products.get(prod_o.product_id)
                if product_def and prod_o.started_at:
                    started_at_date = prod_o.started_at.date()
                    completion_date = started_at_date + timedelta(days=product_def.production_time)
                    completion_offset = (completion_date - current_sim_date).days
                    if 0 <= completion_offset < num_days:
                        daily_deltas[completion_offset] += prod_o.quantity

            accepted_orders_dicts = await crud.get_items(
                crud.COLLECTIONS["production_orders"],
                {"status": "Accepted", "product_id": item_id},
                limit=None,
                sort_field="requested_date", 
                sort_order=1 
            )
            
            simulated_physical_for_fulfillment = float(physical_stock)
            for acc_o_dict in accepted_orders_dicts:
                acc_o = ProductionOrder(**acc_o_dict)
                if simulated_physical_for_fulfillment >= acc_o.quantity:
                    daily_deltas[0] -= acc_o.quantity
                    simulated_physical_for_fulfillment -= acc_o.quantity
        
        running_balance = float(physical_stock)
        for d_offset in range(num_days): 
            running_balance += daily_deltas[d_offset]
            forecast_dt = current_sim_date + timedelta(days=d_offset)
            forecast_list.append(
                DailyForecast(day_offset=d_offset, date=forecast_dt, quantity=running_balance)
            )
            
        return ItemForecastResponse(
            item_id=item_id,
            item_name=item_name,
            item_type=item_type,
            forecast=forecast_list
        )