import requests
import streamlit as st
import os
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json # For handling export/import data

load_dotenv()

# Use environment variable or default for API URL
# Default assumes Docker Compose setup where 'backend' is the service name
API_URL = os.getenv("API_URL", "http://backend:8000")

def handle_api_error(response: requests.Response, context: str):
    """Handles common API errors and displays messages in Streamlit."""
    try:
        detail = response.json().get("detail", "No detail provided.")
    except json.JSONDecodeError:
        detail = response.text # Use raw text if not JSON
    st.error(f"API Error in {context}: {response.status_code} - {detail}")
    return None # Indicate failure

def get_simulation_status() -> Optional[Dict]:
    try:
        response = requests.get(f"{API_URL}/simulation/status")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409: # Handle "Not Initialized" state gracefully
            st.warning("Simulation not initialized. Please go to 'Setup' to initialize.")
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
             st.warning("Simulation not initialized.")
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
            return response.json()
        else:
            handle_api_error(response, f"fetching production orders (status: {status})")
            return []
    except requests.exceptions.RequestException as e:
        st.error(f"Network error fetching production orders: {e}")
        return []

def start_production(order_ids: List[str]) -> Optional[Dict]:
    if not order_ids:
        st.warning("No production orders selected to start.")
        return None
    try:
        response = requests.post(f"{API_URL}/production/orders/start", json={"order_ids": order_ids})
        if response.status_code == 200:
            results = response.json()
            st.success("Attempted to start production.")
            # Display detailed results
            for order_id, msg in results.items():
                if "success" in msg.lower():
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
            st.success(f"Purchase Order {po.get('id')} created successfully. Expected arrival: {po.get('expected_arrival_date')}")
            return True
        else:
            handle_api_error(response, "creating purchase order")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error creating purchase order: {e}")
        return False

def get_inventory() -> Optional[Dict[str, int]]:
    try:
        response = requests.get(f"{API_URL}/inventory")
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 409:
            st.warning("Simulation not initialized.")
            return None
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
            st.success("Data imported successfully! Refresh page may be needed.")
            # Trigger a rerun to reflect changes immediately
            st.rerun()
            return True
        else:
            handle_api_error(response, "importing data")
            return False
    except requests.exceptions.RequestException as e:
        st.error(f"Network error importing data: {e}")
        return False