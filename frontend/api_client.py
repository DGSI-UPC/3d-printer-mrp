import requests
import streamlit as st
import os
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json # For handling export/import data

load_dotenv()

API_URL = os.getenv("API_URL", "http://backend:8000")

def handle_api_error(response: requests.Response, context: str):
    """Handles common API errors and displays messages in Streamlit."""
    try:
        detail = response.json().get("detail", "No detail provided.")
    except json.JSONDecodeError:
        detail = response.text 
    st.error(f"API Error in {context}: {response.status_code} - {detail}")
    return None 

def get_simulation_status() -> Optional[Dict]:
    try:
        response = requests.get(f"{API_URL}/simulation/status")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409: 
            # This state is now handled more directly in the UI by checking if status is None
            # st.warning("Simulation not initialized. Please go to 'Setup' to initialize.")
            return None 
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
        elif response.status_code == 409:
             # st.warning("Simulation not initialized.")
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
            st.success(f"Advanced to Day {response.json().get('current_day')}.")
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
            # Ensure required_materials and committed_materials are dicts
            orders = response.json()
            for order in orders:
                order.setdefault('required_materials', {})
                order.setdefault('committed_materials', {})
            return orders
        else:
            handle_api_error(response, f"fetching production orders (status: {status})")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching production orders: {e}")
        return []

def accept_production_order(order_id: str) -> bool:
    """Attempts to accept a production order."""
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

def order_missing_materials_for_production_order(order_id: str) -> Optional[Dict]:
    """Orders missing materials for a specific production order."""
    try:
        response = requests.post(f"{API_URL}/production/orders/{order_id}/order_missing_materials")
        if response.status_code == 200:
            results = response.json()
            st.success(f"Material ordering process for order {order_id} initiated.")
            for material_id, message in results.items():
                if "Ordered" in message:
                    st.info(f"{material_id}: {message}")
                elif "Sufficient" in message:
                     st.info(f"{material_id}: {message}")
                else:
                    st.warning(f"{material_id}: {message}")
            return results
        else:
            handle_api_error(response, f"ordering materials for production order {order_id}")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error ordering materials for production order {order_id}: {e}")
        return None

def start_production(order_ids: List[str]) -> Optional[Dict]:
    """Attempts to start production for 'Accepted' orders."""
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
            if arrival_date:
                try:
                    # Attempt to parse and reformat the date for better readability
                    parsed_date = datetime.fromisoformat(arrival_date.replace("Z", "+00:00"))
                    arrival_date_str = parsed_date.strftime('%Y-%m-%d')
                except ValueError:
                    arrival_date_str = arrival_date # Fallback to original string if parsing fails
            else:
                arrival_date_str = "N/A"

            st.success(f"Purchase Order {po.get('id')} created successfully. Expected arrival: {arrival_date_str}")
            return True
        else:
            handle_api_error(response, "creating purchase order")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error creating purchase order: {e}")
        return False

def get_inventory() -> Optional[Dict[str, Dict[str, int]]]:
    """
    Fetches inventory data.
    Returns a dictionary: {"physical": {item_id: qty}, "committed": {item_id: qty}}
    or None if an error occurs or not initialized.
    """
    try:
        response = requests.get(f"{API_URL}/inventory")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409: # Simulation not initialized
            # st.warning("Simulation not initialized. Inventory data unavailable.")
            return {"physical": {}, "committed": {}} # Return empty structure
        else:
            handle_api_error(response, "fetching inventory")
            return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching inventory: {e}")
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
            st.rerun() # Trigger a rerun to reflect changes immediately
            return True
        else:
            handle_api_error(response, "importing data")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error importing data: {e}")
        return False