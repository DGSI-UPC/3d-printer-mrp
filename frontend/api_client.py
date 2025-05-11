import requests
import streamlit as st
import os
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json
from datetime import datetime

load_dotenv()

API_URL = os.getenv("API_URL", "http://backend:8000")

def handle_api_error(response: requests.Response, context: str):
    try:
        detail = response.json().get("detail", "No detail provided.")
    except json.JSONDecodeError:
        detail = response.text
    
    if response.status_code == 402: # Payment Required
        st.error(f"Financial Error in {context}: {response.status_code} - {detail}")
    elif response.status_code == 409 and "Simulation not initialized" in detail: # Specific handling for 409 not initialized
        st.warning(f"{detail} (Info from: {context})") # Less alarming for "not initialized"
    else:
        st.error(f"API Error in {context}: {response.status_code} - {detail}")
    return None

def get_simulation_status() -> Optional[Dict]:
    try:
        response = requests.get(f"{API_URL}/simulation/status")
        if response.status_code == 200:
            return response.json()
        # Simulation not initialized is a common case, handle less like an "error"
        elif response.status_code == 409 and response.json().get("detail", "").startswith("Simulation not initialized"):
            # st.info("Simulation not initialized yet. Go to 'Setup & Data'. (From: fetching simulation status)")
            return None # Return None, page will handle message
        else:
            handle_api_error(response, "fetching simulation status")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching simulation status: {e}")
        return None

def get_full_simulation_state() -> Optional[Dict]:
    try:
        response = requests.get(f"{API_URL}/simulation/state")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409 and response.json().get("detail", "").startswith("Simulation not initialized"):
             # st.info("Simulation not initialized. (From: fetching simulation state)")
             return None
        else:
            handle_api_error(response, "fetching simulation state")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching simulation state: {e}")
        return None

def initialize_simulation(initial_data: Dict) -> bool:
    try:
        response = requests.post(f"{API_URL}/simulation/initialize", json=initial_data)
        if response.status_code == 201:
            st.success("Simulation initialized successfully!")
            return True
        else:
            handle_api_error(response, "initializing simulation")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error initializing simulation: {e}")
        return False

def advance_day() -> Optional[Dict]:
    try:
        response = requests.post(f"{API_URL}/simulation/advance_day")
        if response.status_code == 200:
            st.success(f"Advanced to Day {response.json().get('current_day')}. Balance: {response.json().get('current_balance', 0.0):.2f} EUR")
            return response.json()
        else:
            handle_api_error(response, "advancing simulation day")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error advancing simulation day: {e}")
        return None

def get_materials() -> List[Dict]:
    try:
        response = requests.get(f"{API_URL}/materials")
        if response.status_code == 200:
            return response.json()
        else:
            handle_api_error(response, "fetching materials")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching materials: {e}")
        return []

def get_products() -> List[Dict]:
    try:
        response = requests.get(f"{API_URL}/products")
        if response.status_code == 200:
            return response.json()
        else:
            handle_api_error(response, "fetching products")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching products: {e}")
        return []

def get_providers() -> List[Dict]:
    try:
        response = requests.get(f"{API_URL}/providers")
        if response.status_code == 200:
            return response.json()
        else:
            handle_api_error(response, "fetching providers")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching providers: {e}")
        return []

def get_production_orders(status: Optional[str] = None) -> List[Dict]:
    params = {"status": status} if status else {}
    try:
        response = requests.get(f"{API_URL}/production/orders", params=params)
        if response.status_code == 200:
            orders = response.json()
            for order in orders:
                order.setdefault('required_materials', {})
                order.setdefault('committed_materials', {})
                order.setdefault('revenue_collected', False)
            return orders
        else:
            handle_api_error(response, f"fetching production orders (status: {status})")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching production orders: {e}")
        return []

def accept_production_order(order_id: str) -> bool:
    try:
        response = requests.post(f"{API_URL}/production/orders/{order_id}/accept")
        if response.status_code == 200:
            st.success(response.json().get("message", f"Order {order_id} processed for acceptance."))
            return True
        else:
            handle_api_error(response, f"accepting production order {order_id}")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error accepting production order {order_id}: {e}")
        return False

def fulfill_accepted_production_order_from_stock(order_id: str) -> bool:
    try:
        response = requests.post(f"{API_URL}/production/orders/{order_id}/fulfill_accepted_from_stock")
        if response.status_code == 200:
            st.success(response.json().get("message", f"Order {order_id} fulfillment from stock processed."))
            return True
        else:
            handle_api_error(response, f"fulfilling accepted order {order_id} from stock")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fulfilling accepted order {order_id} from stock: {e}")
        return False

def order_missing_materials_for_production_order(order_id: str) -> Optional[Dict]:
    try:
        response = requests.post(f"{API_URL}/production/orders/{order_id}/order_missing_materials")
        if response.status_code == 200:
            results = response.json()
            st.success(f"Material ordering process for order {order_id} initiated.")
            all_sufficient_or_ordered = True
            contains_error_msg = False
            for material_id, message in results.items():
                if "Ordered" in message:
                    st.info(f"🧾 {material_id}: {message}")
                elif "Sufficient" in message or "No additional sourcing needed" in message :
                     st.write(f"✔️ {material_id}: {message}")
                elif "Error placing PO" in message or "Insufficient funds" in message: # Specific check for PO error
                    st.error(f"⚠️ {material_id}: {message}") # Use error for this
                    all_sufficient_or_ordered = False
                    contains_error_msg = True
                else: # Other warnings or non-critical messages
                    st.warning(f"ℹ️ {material_id}: {message}")
                    all_sufficient_or_ordered = False # Treat other messages as needing attention

            if all_sufficient_or_ordered and not any("Ordered" in msg for msg in results.values()) and not contains_error_msg:
                 st.info("All materials for this order are already sufficiently stocked or covered.")
            elif not all_sufficient_or_ordered and not any("Ordered" in msg for msg in results.values()) and not contains_error_msg :
                st.warning("Review material status; some items may need attention or alternative sourcing.")
            return results
        else: # Handles non-200 responses, including 402 from the endpoint itself
            handle_api_error(response, f"ordering materials for production order {order_id}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error ordering materials for production order {order_id}: {e}")
        return None

def start_production(order_ids: List[str]) -> Optional[Dict]:
    if not order_ids:
        st.warning("No production orders selected to start.")
        return None
    try:
        response = requests.post(f"{API_URL}/production/orders/start", json={"order_ids": order_ids})
        if response.status_code == 200:
            results = response.json()
            st.success("Attempted to start production for selected 'Accepted' orders.")
            for order_id, msg in results.items():
                if "success" in msg.lower() or "started" in msg.lower():
                    st.info(f"Order {order_id}: {msg}")
                else:
                    st.warning(f"Order {order_id}: {msg}")
            return results
        else:
            handle_api_error(response, "starting production")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error starting production: {e}")
        return None

def get_purchase_orders(status: Optional[str] = None) -> List[Dict]:
    params = {"status": status} if status else {}
    try:
        response = requests.get(f"{API_URL}/purchase/orders", params=params)
        if response.status_code == 200:
            return response.json()
        else:
            handle_api_error(response, f"fetching purchase orders (status: {status})")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching purchase orders: {e}")
        return []

def create_purchase_order(material_id: str, provider_id: str, quantity: int) -> bool:
    payload = {
        "material_id": material_id,
        "provider_id": provider_id,
        "quantity": quantity
    }
    try:
        response = requests.post(f"{API_URL}/purchase/orders", json=payload)
        if response.status_code == 201:
            po = response.json()
            arrival_date = po.get('expected_arrival_date')
            arrival_date_str = "N/A"
            if arrival_date:
                try:
                    parsed_date = datetime.fromisoformat(arrival_date.replace("Z", "+00:00")) if isinstance(arrival_date, str) else datetime.fromtimestamp(arrival_date) if isinstance(arrival_date, (int, float)) else None
                    if parsed_date: arrival_date_str = parsed_date.strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    arrival_date_str = str(arrival_date)
            
            total_cost_str = f"{po.get('total_cost', 0.0):.2f} EUR" if po.get('total_cost') is not None else "N/A"
            st.success(f"Purchase Order {po.get('id')} created. Cost: {total_cost_str}. Expected arrival: {arrival_date_str}")
            return True
        else: # Handles non-201, including 402 from the endpoint
            handle_api_error(response, "creating purchase order")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error creating purchase order: {e}")
        return False

def get_inventory() -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(f"{API_URL}/inventory")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409 and response.json().get("detail","").startswith("Simulation not initialized"):
            return {"items": {}} 
        else:
            handle_api_error(response, "fetching inventory")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching inventory: {e}")
        return None

def get_item_forecast(item_id: str, days: int, historical_lookback_days: int = 0) -> Optional[Dict]:
    if not item_id: return None
    try:
        params = {"days": days, "historical_lookback_days": historical_lookback_days}
        response = requests.get(f"{API_URL}/inventory/forecast/{item_id}", params=params)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            st.warning(f"Item '{item_id}' not found for forecasting.")
            return None
        elif response.status_code == 409 and response.json().get("detail","").startswith("Simulation not initialized"):
            st.warning("Simulation not initialized. Cannot fetch forecast.")
            return None
        else:
            handle_api_error(response, f"fetching forecast for item {item_id}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching item forecast: {e}")
        return None

def get_events(limit: int = 100) -> List[Dict]:
    params = {"limit": limit}
    try:
        response = requests.get(f"{API_URL}/events", params=params)
        if response.status_code == 200:
            return response.json()
        else:
            handle_api_error(response, "fetching events")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching events: {e}")
        return []

def export_data() -> Optional[Dict]:
    try:
        response = requests.get(f"{API_URL}/data/export")
        if response.status_code == 200:
            st.success("Data exported successfully.")
            return response.json()
        else:
            handle_api_error(response, "exporting data")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error exporting data: {e}")
        return None

def import_data(data: Dict) -> bool:
    try:
        response = requests.post(f"{API_URL}/data/import", json=data)
        if response.status_code == 200:
            st.success("Data imported successfully! Refreshing data...")
            # st.rerun() # Re-run should be handled by the calling page if needed
            return True
        else:
            handle_api_error(response, "importing data")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error importing data: {e}")
        return False

# --- New function for Finances Page ---
def get_financial_data(forecast_days: int = 7) -> Optional[Dict]:
    """
    Fetches financial summary, historical performance, and forecast.
    Returns a dictionary corresponding to the FinancialPageData model.
    """
    try:
        params = {"forecast_days": forecast_days}
        response = requests.get(f"{API_URL}/finances", params=params)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409 and response.json().get("detail","").startswith("Simulation not initialized"):
            st.warning("Simulation not initialized. Cannot fetch financial data.")
            return None
        else:
            handle_api_error(response, "fetching financial data")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching financial data: {e}")
        return None