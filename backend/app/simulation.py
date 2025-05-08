from typing import Dict, List
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
        self.products = {p.id: p for p in products}
        self.materials = {m.id: m for m in materials}
        self.providers = {p.id: p for p in providers}
        self.config = config
        self.production_capacity_resource = simpy.Resource(self.env, capacity=self.state.daily_production_capacity)
        self.production_processes = {}
        self.arrival_processes = {}

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

    async def update_inventory(self, item_id: str, quantity_change: int):
        current_qty = self.state.inventory.get(item_id, 0)
        new_qty = max(0, current_qty + quantity_change)
        self.state.inventory[item_id] = new_qty
        await self.log_sim_event("inventory_change", {"item_id": item_id, "change": quantity_change, "new_quantity": new_qty})
        await crud.save_simulation_state(self.state)

    def production_process(self, order_id: str):
        logger.debug(f"[Day {self.state.current_day}] Production process started for order {order_id} unit.")
        order_data = yield self.env.process(self.get_production_order_async(order_id))
        if not order_data:
            logger.error(f"Could not retrieve order {order_id} during production process.")
            return

        product = self.products.get(order_data.product_id)
        if not product:
             logger.error(f"Product {order_data.product_id} not found for order {order_id}.")
             return

        yield self.env.timeout(product.production_time)
        logger.info(f"[Sim Time {self.env.now}] One unit for production order {order_id} completed.")
        return {"order_id": order_id, "product_id": product.id}

    def material_arrival_process(self, po_id: str):
        po_data = yield self.env.process(self.get_purchase_order_async(po_id))
        if not po_data:
            logger.error(f"Could not retrieve purchase order {po_id} during arrival process.")
            return

        order_date_val = po_data.order_date
        if order_date_val.tzinfo is None:
            order_date_val = order_date_val.replace(tzinfo=timezone.utc)

        expected_arrival_date_val = po_data.expected_arrival_date
        if expected_arrival_date_val.tzinfo is None:
            expected_arrival_date_val = expected_arrival_date_val.replace(tzinfo=timezone.utc)
        
        lead_time = (expected_arrival_date_val.date() - order_date_val.date()).days
        
        logger.debug(f"[Day {self.state.current_day}] Material arrival process started for PO {po_id}. Lead time: {lead_time} days.")
        yield self.env.timeout(lead_time)
        
        actual_arrival_day_offset = (expected_arrival_date_val.date() - SIMULATION_EPOCH_DATETIME.date()).days
        logger.info(f"[Sim Time {self.env.now}] Materials for PO {po_id} arriving. Expected Day (offset): {actual_arrival_day_offset}")

        return {
            "po_id": po_id,
            "material_id": po_data.material_id,
            "quantity": po_data.quantity_ordered,
            "arrival_day_offset": actual_arrival_day_offset
        }

    async def get_production_order_async(self, order_id: str):
        order_dict = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
        return ProductionOrder(**order_dict) if order_dict else None

    async def get_purchase_order_async(self, po_id: str):
        po_dict = await crud.get_item_by_id(crud.COLLECTIONS["purchase_orders"], po_id)
        return PurchaseOrder(**po_dict) if po_dict else None

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
                logger.warning(f"Pending PO {po_id} not found in DB, removing from state.")
                if po_id in self.state.pending_purchase_orders:
                    self.state.pending_purchase_orders.remove(po_id)
                continue
            
            expected_arrival_date_val = po.expected_arrival_date.date()

            if po.status == "Ordered" and expected_arrival_date_val <= current_sim_processing_date:
                 if await self.check_storage_capacity(po.quantity_ordered):
                    logger.info(f"[Day {current_day_offset}] Processing arrival for PO {po_id} (Material: {po.material_id}, Qty: {po.quantity_ordered}).")
                    await self.update_inventory(po.material_id, po.quantity_ordered)
                    po.status = "Arrived"
                    po.actual_arrival_date = current_sim_processing_datetime
                    po.units_received = po.quantity_ordered
                    po_dump = po.model_dump()
                    await crud.update_item(crud.COLLECTIONS["purchase_orders"], po.id, po_dump)
                    if po_id in self.state.pending_purchase_orders:
                         self.state.pending_purchase_orders.remove(po_id)
                    await self.log_sim_event("material_arrival", {"po_id": po.id, "material_id": po.material_id, "quantity": po.quantity_ordered})
                 else:
                     logger.warning(f"[Day {current_day_offset}] PO {po_id} arrival delayed - insufficient storage capacity.")
                     await self.log_sim_event("arrival_delayed_storage", {"po_id": po.id, "material_id": po.material_id, "quantity": po.quantity_ordered})

        completed_production_today = 0
        active_order_ids = self.state.active_production_orders[:]

        for order_id in active_order_ids:
            order = await self.get_production_order_async(order_id)
            if not order or order.status != "In Progress":
                logger.warning(f"Active order {order_id} not found or not 'In Progress'. Removing from active list.")
                if order_id in self.state.active_production_orders: self.state.active_production_orders.remove(order_id)
                continue

            product = self.products.get(order.product_id)
            if not product:
                 logger.error(f"Product {order.product_id} for active order {order_id} not found.")
                 continue

            if order.started_at:
                # Ensure started_at is timezone-aware for correct calculation
                started_at_aware = order.started_at
                if started_at_aware.tzinfo is None:
                    started_at_aware = started_at_aware.replace(tzinfo=timezone.utc)

                days_in_production = (current_sim_processing_datetime.date() - started_at_aware.date()).days

                if days_in_production >= product.production_time:
                    if completed_production_today < self.state.daily_production_capacity:
                        logger.info(f"[Day {current_day_offset}] Production completed for order {order_id} (Product: {order.product_id}, Qty: {order.quantity}).")
                        await self.update_inventory(order.product_id, order.quantity)
                        order.status = "Completed"
                        order.completed_at = current_sim_processing_datetime
                        await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump())
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

            required_materials = {}
            for bom_item in product.bom:
                required_materials[bom_item.material_id] = required_materials.get(bom_item.material_id, 0) + (bom_item.quantity * quantity)

            new_order = ProductionOrder(
                id=utils.generate_id(),
                product_id=product_id,
                quantity=quantity,
                requested_date=current_sim_datetime_for_request,
                status="Pending",
                required_materials=required_materials,
                created_at=utils.get_current_utc_timestamp()
            )
            await crud.create_item(crud.COLLECTIONS["production_orders"], new_order.model_dump())
            await self.log_sim_event("order_received", {"order_id": new_order.id, "product_id": product_id, "quantity": quantity, "requested_date_iso": new_order.requested_date.isoformat()})
            logger.info(f"[Day {current_day_offset}] Generated random order {new_order.id} for {quantity}x {product.name}")

    async def start_production(self, order_ids: List[str]) -> Dict[str, str]:
        results = {}
        current_sim_datetime_for_start = SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)

        for order_id in order_ids:
            order_dict = await crud.get_item_by_id(crud.COLLECTIONS["production_orders"], order_id)
            if not order_dict:
                results[order_id] = "Order not found."
                continue

            order = ProductionOrder(**order_dict)
            if order.status != "Pending":
                results[order_id] = f"Order status is not 'Pending' (Status: {order.status})."
                continue

            product = self.products.get(order.product_id)
            if not product:
                 results[order_id] = f"Product {order.product_id} definition not found."
                 continue

            can_produce = True
            materials_to_consume = {}
            if not order.required_materials:
                 for bom_item in product.bom:
                     materials_to_consume[bom_item.material_id] = materials_to_consume.get(bom_item.material_id, 0) + (bom_item.quantity * order.quantity)
                 order.required_materials = materials_to_consume
            else:
                 materials_to_consume = order.required_materials

            for mat_id, qty_needed in materials_to_consume.items():
                if self.state.inventory.get(mat_id, 0) < qty_needed:
                    can_produce = False
                    results[order_id] = f"Insufficient material {mat_id} (Need: {qty_needed}, Have: {self.state.inventory.get(mat_id, 0)})."
                    await self.log_sim_event("production_start_failed_material", {"order_id": order.id, "material_id": mat_id, "needed": qty_needed})
                    break

            if can_produce:
                for mat_id, qty_needed in materials_to_consume.items():
                    await self.update_inventory(mat_id, -qty_needed)

                order.status = "In Progress"
                order.started_at = current_sim_datetime_for_start # Mark start with simulated day's timestamp
                await crud.update_item(crud.COLLECTIONS["production_orders"], order.id, order.model_dump())
                self.state.active_production_orders.append(order.id)
                results[order_id] = "Production started successfully."
                await self.log_sim_event("production_started", {"order_id": order.id, "product_id": order.product_id})

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

        order_timestamp = utils.get_current_utc_timestamp()
        
        current_sim_date_for_order = (SIMULATION_EPOCH_DATETIME + timedelta(days=self.state.current_day)).date()
        order_date_start_of_sim_day = datetime.combine(current_sim_date_for_order, datetime.min.time(), tzinfo=timezone.utc)
        
        expected_arrival_datetime = order_date_start_of_sim_day + timedelta(days=offering.lead_time_days)

        po = PurchaseOrder(
            id=utils.generate_id(),
            material_id=material_id,
            provider_id=provider_id,
            quantity_ordered=quantity,
            order_date=order_timestamp,
            expected_arrival_date=expected_arrival_datetime,
            status="Ordered",
            created_at=order_timestamp
        )
        po_dict = await crud.create_item(crud.COLLECTIONS["purchase_orders"], po.model_dump())

        self.state.pending_purchase_orders.append(po.id)
        await crud.save_simulation_state(self.state)

        await self.log_sim_event("purchase_order_placed", {
            "po_id": po.id,
            "material_id": material_id,
            "provider_id": provider_id,
            "quantity": quantity,
            "expected_arrival": expected_arrival_datetime.isoformat()
        })

        logger.info(f"[Day {self.state.current_day}] Placed Purchase Order {po.id} for {quantity}x {material.name} from {provider.name}. Expected arrival: {expected_arrival_datetime.isoformat()}")
        return PurchaseOrder(**po_dict)