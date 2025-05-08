import streamlit as st
import pandas as pd
import plotly.express as px
import json
from datetime import datetime

from api_client import (
    get_simulation_status, initialize_simulation, advance_day,
    get_materials, get_products, get_providers, get_inventory,
    get_production_orders, start_production, accept_production_order, # Added accept_production_order
    order_missing_materials_for_production_order, # Added
    get_purchase_orders, create_purchase_order,
    get_events, export_data, import_data
)

st.set_page_config(
    page_title="MRP Factory Simulation",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Cache ---
@st.cache_data(ttl=60) # Reduced TTL for more frequent updates if needed, adjust as necessary
def load_base_data():
    materials = get_materials()
    products = get_products()
    providers = get_providers()
    return materials, products, providers

@st.cache_data(ttl=10) # Cache inventory for a short period
def load_inventory_data():
    return get_inventory()

# --- Helper Functions ---
def format_bom(bom_list, materials_dict, header=""):
    if not bom_list: return f"{header}No BOM defined" if header else "No BOM defined"
    lines = [header] if header else []
    for item in bom_list:
        mat_name = materials_dict.get(item.get('material_id'), {}).get('name', item.get('material_id'))
        lines.append(f"- {mat_name}: {item.get('quantity', 'N/A')}")
    return "\n".join(lines)

def format_material_list_with_stock(materials_needed_dict, physical_inventory_dict, materials_dict):
    if not materials_needed_dict:
        return "N/A"
    
    lines = []
    all_available = True
    for mat_id, qty_needed in materials_needed_dict.items():
        mat_name = materials_dict.get(mat_id, {}).get('name', mat_id)
        available_qty = physical_inventory_dict.get(mat_id, 0)
        color = "green" if available_qty >= qty_needed else "red"
        if available_qty < qty_needed:
            all_available = False
        lines.append(f"<span style='color:{color};'>- {mat_name}: Need {qty_needed}, Have {available_qty}</span>")
    # status_icon = "✅" if all_available else "⚠️"
    # return f"{status_icon}\n" + "\n".join(lines)
    return "<br>".join(lines) # Using <br> for Streamlit markdown line breaks

def format_catalogue(catalogue_list, materials_dict):
    if not catalogue_list: return "No offerings defined"
    lines = []
    for item in catalogue_list:
        mat_name = materials_dict.get(item['material_id'], {}).get('name', item['material_id'])
        lines.append(f"- {mat_name}: €{item['price_per_unit']:.2f}/unit (Lead: {item['lead_time_days']} days)")
    return "\n".join(lines)

# --- Load Initial Data & Session State ---
if 'simulation_status' not in st.session_state:
    st.session_state.simulation_status = None

materials, products, providers = load_base_data()
materials_dict = {m['id']: m for m in materials if m} if materials else {}
products_dict = {p['id']: p for p in products if p} if products else {}
providers_dict = {p['id']: p for p in providers if p} if providers else {}

current_inventory_data = load_inventory_data()
physical_inventory = current_inventory_data.get('physical', {}) if current_inventory_data else {}
committed_inventory = current_inventory_data.get('committed', {}) if current_inventory_data else {}

# --- Sidebar ---
st.sidebar.title("🏭 MRP Factory Simulation")
st.session_state.simulation_status = get_simulation_status() # Refresh status

if st.session_state.simulation_status:
    status = st.session_state.simulation_status
    st.sidebar.metric("Current Day", status.get('current_day', 'N/A'))
    st.sidebar.metric("Pending Production", status.get('pending_production_orders', 'N/A'))
    st.sidebar.metric("Accepted Production", status.get('accepted_production_orders', 'N/A')) # New
    st.sidebar.metric("In Progress Production", status.get('in_progress_production_orders', 'N/A'))
    st.sidebar.metric("Pending Purchases", status.get('pending_purchase_orders', 'N/A'))

    inv_units = status.get('total_inventory_units', 0) # This is physical for capacity
    capacity = status.get('storage_capacity', 1)
    util = status.get('storage_utilization', 0)

    st.sidebar.progress(util / 100 if capacity > 0 else 0, text=f"Storage: {inv_units}/{capacity} ({util:.1f}%)")

    if st.sidebar.button("Advance 1 Day", use_container_width=True, type="primary"):
        if advance_day():
            load_inventory_data.clear() # Clear cache
            st.rerun()

else:
    st.sidebar.warning("Simulation not running or API unreachable. Initialize first.")

st.sidebar.divider()
st.sidebar.header("Navigation")
page = st.sidebar.radio("Go to",
                        ["Dashboard", "Production", "Purchasing", "Inventory", "History", "Setup & Data"],
                        label_visibility="collapsed")
st.sidebar.divider()
st.sidebar.info("Manage your 3D printer factory day by day.")


# --- Page Content ---
if page == "Dashboard":
    st.header("🏭 Dashboard Overview")
    if st.session_state.simulation_status:
        status = st.session_state.simulation_status
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Current Day", status.get('current_day', 'N/A'))
        col2.metric("Pending Orders", status.get('pending_production_orders', 'N/A'))
        col3.metric("Accepted Orders", status.get('accepted_production_orders', 'N/A'))
        col4.metric("Pending Purchases", status.get('pending_purchase_orders', 'N/A'))

        st.subheader("Recent Events")
        events = get_events(limit=10)
        if events:
            events_df = pd.DataFrame(events)
            events_df['timestamp'] = pd.to_datetime(events_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            events_df_display = events_df[['day', 'timestamp', 'event_type', 'details']].copy()
            # events_df_display['details'] = events_df_display['details'].apply(lambda x: json.dumps(x, indent=2)) # Can be too verbose
            st.dataframe(events_df_display, use_container_width=True, height=300)
        else:
            st.info("No simulation events recorded yet.")

        st.subheader("Current Inventory Snapshot (Physical Stock)")
        if physical_inventory:
            inv_items = []
            for item_id, qty in physical_inventory.items():
                item_name = "Unknown Item"
                item_type = "Unknown"
                if item_id in materials_dict:
                    item_name = materials_dict[item_id]['name']
                    item_type = "Material"
                elif item_id in products_dict:
                     item_name = products_dict[item_id]['name']
                     item_type = "Product"
                inv_items.append({"ID": item_id, "Name": item_name, "Type": item_type, "Quantity": qty})
            
            if inv_items:
                inv_df = pd.DataFrame(inv_items)
                if not inv_df.empty:
                    fig = px.bar(inv_df.sort_values("Quantity", ascending=False).head(15), # Show top 15
                                 x="Name", y="Quantity", color="Type",
                                 title="Top 15 Items - Physical Stock Levels", labels={'Name':'Item Name'})
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Physical inventory is currently empty.")
            else:
                st.info("Physical inventory is currently empty.")
        else:
            st.info("Could not fetch inventory data or inventory is empty.")
    else:
        st.warning("Simulation not initialized. Go to 'Setup & Data' to start.")


elif page == "Production":
    st.header("🛠️ Production Management")

    if not st.session_state.simulation_status:
        st.warning("Simulation not initialized.")
    else:
        tab1, tab2, tab3, tab4 = st.tabs(["Pending Requests", "Accepted Orders", "In Progress Orders", "Completed Orders"])

        with tab1: # Pending Requests
            st.subheader("Pending Production Requests")
            pending_orders_data = get_production_orders(status="Pending")
            if pending_orders_data:
                # Create a list of dictionaries for st.data_editor or custom display
                display_data = []
                for order in pending_orders_data:
                    product_name = products_dict.get(order['product_id'], {}).get('name', order['product_id'])
                    material_status_html = format_material_list_with_stock(
                        order.get('required_materials', {}),
                        physical_inventory,
                        materials_dict
                    )
                    shortage_exists = "<span style='color:red;'>" in material_status_html
                    display_data.append({
                        "Order ID": order['id'],
                        "Product": product_name,
                        "Qty": order['quantity'],
                        "Requested": pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d'),
                        "Required Materials (Need, Have)": material_status_html,
                        "_shortage": shortage_exists, # Internal flag
                        "_order_obj": order # To access full order for actions
                    })
                
                if display_data:
                    cols = st.columns((1, 2, 1, 1, 3, 1, 2)) # Adjust column widths
                    headers = ["Order ID", "Product", "Qty", "Requested", "Material Availability", "Order Materials", "Accept Order"]
                    for col, header in zip(cols, headers):
                        col.markdown(f"**{header}**")
                    
                    st.markdown("---") # Separator

                    for item in display_data:
                        cols = st.columns((1, 2, 1, 1, 3, 1, 2))
                        cols[0].markdown(item["Order ID"])
                        cols[1].markdown(item["Product"])
                        cols[2].markdown(str(item["Qty"]))
                        cols[3].markdown(item["Requested"])
                        cols[4].markdown(item["Required Materials (Need, Have)"], unsafe_allow_html=True)

                        if item["_shortage"]:
                            if cols[5].button("Order Missing", key=f"order_missing_{item['Order ID']}", help="Order all missing materials for this request from cheapest providers."):
                                if order_missing_materials_for_production_order(item['Order ID']):
                                    load_inventory_data.clear()
                                    st.rerun()
                        else:
                            cols[5].markdown("-") # Placeholder if no shortage

                        if cols[6].button("Accept", key=f"accept_{item['Order ID']}", help="Accept this request and commit materials from physical stock."):
                            if accept_production_order(item['Order ID']):
                                load_inventory_data.clear()
                                st.rerun()
                        st.markdown("---") # Separator for each order
                else:
                    st.info("No pending production requests.")

            else:
                st.info("No pending production requests.")

        with tab2: # Accepted Orders
            st.subheader("Accepted Production Orders (Materials Committed)")
            accepted_orders_data = get_production_orders(status="Accepted")
            if accepted_orders_data:
                df_data = []
                for order in accepted_orders_data:
                    product_name = products_dict.get(order['product_id'], {}).get('name', order['product_id'])
                    committed_mats_display = format_bom(
                        [{'material_id': mid, 'quantity': qty} for mid, qty in order.get('committed_materials', {}).items()],
                        materials_dict
                    )
                    df_data.append({
                        "Order ID": order['id'],
                        "Product": product_name,
                        "Qty": order['quantity'],
                        "Requested": pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d'),
                        "Committed Materials": committed_mats_display,
                        "_order_id_internal": order['id'] # for selection
                    })

                if df_data:
                    orders_df = pd.DataFrame(df_data)
                    orders_df.insert(0, 'Select', False)
                    
                    edited_df = st.data_editor(
                        orders_df[['Select', 'Order ID', 'Product', 'Qty', 'Requested', 'Committed Materials']],
                        column_config={
                            "Select": st.column_config.CheckboxColumn(default=False),
                            "Order ID": st.column_config.TextColumn(disabled=True),
                            "Committed Materials": st.column_config.TextColumn(width="large", disabled=True)
                        },
                        use_container_width=True,
                        hide_index=True,
                        key="accepted_order_selector"
                    )
                    selected_mask = edited_df['Select'].fillna(False)
                    selected_order_ids = orders_df.loc[selected_mask.tolist(), '_order_id_internal'].tolist()

                    if st.button(f"Send Selected ({len(selected_order_ids)}) Accepted Orders to Production", disabled=not selected_order_ids):
                        if start_production(selected_order_ids):
                            load_inventory_data.clear()
                            st.rerun()
                else:
                    st.info("No production orders currently in 'Accepted' state.")
            else:
                st.info("No production orders currently in 'Accepted' state.")

        with tab3: # In Progress
            st.subheader("Production Orders In Progress")
            in_progress_orders = get_production_orders(status="In Progress")
            if in_progress_orders:
                 orders_df = pd.DataFrame(in_progress_orders)
                 orders_df['product_name'] = orders_df['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df['started_at_display'] = orders_df['started_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_display = orders_df[['id', 'product_name', 'quantity', 'started_at_display']].rename(
                     columns={'id':'Order ID', 'product_name':'Product', 'quantity':'Qty', 'started_at_display':'Started At'}
                 )
                 st.dataframe(orders_df_display, use_container_width=True, hide_index=True)
            else:
                 st.info("No production orders currently in progress.")

        with tab4: # Completed
             st.subheader("Completed Production Orders")
             completed_orders = get_production_orders(status="Completed")
             if completed_orders:
                 orders_df = pd.DataFrame(completed_orders)
                 orders_df['product_name'] = orders_df['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df['completed_at_display'] = orders_df['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_display = orders_df[['id', 'product_name', 'quantity', 'completed_at_display']].rename(
                     columns={'id':'Order ID', 'product_name':'Product', 'quantity':'Qty', 'completed_at_display':'Completed At'}
                 )
                 st.dataframe(orders_df_display, use_container_width=True, hide_index=True)
             else:
                 st.info("No production orders have been completed yet.")


elif page == "Purchasing":
    st.header("🛒 Material Purchasing")

    if not st.session_state.simulation_status:
         st.warning("Simulation not initialized.")
    elif not materials or not providers:
         st.warning("No materials or providers defined. Initialize simulation with data first.")
    else:
        col1, col2 = st.columns([2, 1])

        with col1:
            st.subheader("Create Purchase Order")
            with st.form("purchase_order_form"):
                material_options = {m['id']: f"{m['name']} (ID: {m['id']})" for m in materials}
                selected_material_id = st.selectbox("Select Material", options=list(material_options.keys()), format_func=lambda x: material_options[x])

                # Filter providers who offer the selected material
                available_providers_for_material = []
                if selected_material_id:
                    for p_id, p_data in providers_dict.items():
                        if any(offer['material_id'] == selected_material_id for offer in p_data.get('catalogue', [])):
                            available_providers_for_material.append(p_data)
                
                if not available_providers_for_material:
                    st.warning(f"No provider offers the selected material: {materials_dict.get(selected_material_id, {}).get('name', selected_material_id)}")
                    selected_provider_id = None
                    st.selectbox("Select Provider", [], disabled=True, help="No provider for this material.")
                    st.number_input("Quantity", min_value=1, value=1, step=1, disabled=True)
                    submit_disabled = True
                else:
                    provider_options = {p['id']: f"{p['name']} (ID: {p['id']})" for p in available_providers_for_material}
                    selected_provider_id = st.selectbox("Select Provider", options=list(provider_options.keys()), format_func=lambda x: provider_options[x])

                    if selected_provider_id:
                         prov = providers_dict.get(selected_provider_id)
                         offering = next((o for o in prov.get('catalogue', []) if o['material_id'] == selected_material_id), None)
                         if offering:
                              st.info(f"Provider Offering: Price: €{offering['price_per_unit']:.2f}/unit, Lead Time: {offering['lead_time_days']} days")
                         else: # Should not happen if filtering worked
                              st.error("Error: Selected provider does not seem to offer this material (data inconsistency?).")
                    
                    quantity = st.number_input("Quantity (units)", min_value=1, value=10, step=1)
                    submit_disabled = not selected_provider_id


                submitted = st.form_submit_button("Place Purchase Order", disabled=submit_disabled)
                if submitted and selected_material_id and selected_provider_id and quantity > 0:
                    if create_purchase_order(selected_material_id, selected_provider_id, quantity):
                        load_inventory_data.clear() # PO affects future inventory state
                        st.rerun()
        with col2:
            st.subheader("Providers & Offerings")
            if providers:
                for provider in providers:
                    with st.expander(f"{provider['name']}"):
                        st.write(f"**ID:** {provider['id']}")
                        st.write("**Catalogue:**")
                        catalogue_str = format_catalogue(provider.get('catalogue', []), materials_dict)
                        st.markdown(catalogue_str)
            else:
                 st.info("No providers defined.")

        st.divider()
        st.subheader("Pending Purchase Orders")
        pending_pos = get_purchase_orders(status="Ordered")
        if pending_pos:
            pos_df_data = []
            for po in pending_pos:
                pos_df_data.append({
                    "PO ID": po['id'],
                    "Material": materials_dict.get(po['material_id'], {}).get('name', po['material_id']),
                    "Qty": po['quantity_ordered'],
                    "Provider": providers_dict.get(po['provider_id'], {}).get('name', po['provider_id']),
                    "Ordered": pd.to_datetime(po['order_date']).strftime('%Y-%m-%d %H:%M'),
                    "ETA": pd.to_datetime(po['expected_arrival_date']).strftime('%Y-%m-%d')
                })
            pos_df = pd.DataFrame(pos_df_data)
            st.dataframe(pos_df, use_container_width=True, hide_index=True)
        else:
            st.info("No pending purchase orders.")


elif page == "Inventory":
    st.header("📦 Inventory Status")

    if not st.session_state.simulation_status:
         st.warning("Simulation not initialized.")
    else:
        # current_inventory_data already loaded globally
        if current_inventory_data and ('physical' in current_inventory_data or 'committed' in current_inventory_data):
            all_item_ids = set(physical_inventory.keys()) | set(committed_inventory.keys())
            
            inv_display_data = []
            if not all_item_ids:
                st.info("Inventory is currently empty (no physical or committed stock).")
            else:
                for item_id in sorted(list(all_item_ids)):
                    item_name = "Unknown Item"
                    item_type = "Unknown"
                    description = ""
                    
                    if item_id in materials_dict:
                        item_name = materials_dict[item_id]['name']
                        item_type = "Material"
                        description = materials_dict[item_id].get('description', '')
                    elif item_id in products_dict:
                        item_name = products_dict[item_id]['name']
                        item_type = "Product"
                        # Products don't have description in model, could add if needed
                    
                    physical_qty = physical_inventory.get(item_id, 0)
                    committed_qty = committed_inventory.get(item_id, 0)
                    available_qty = physical_qty - committed_qty # Can be negative if committed > physical (error state)

                    inv_display_data.append({
                        "Name": item_name,
                        "Type": item_type,
                        "Physical Stock": physical_qty,
                        "Committed Stock": committed_qty,
                        "Available Stock": available_qty,
                        "ID": item_id,
                        # "Description": description # Can add back if needed
                    })

                inv_df = pd.DataFrame(inv_display_data)
                st.dataframe(inv_df[['Name', 'Type', 'Physical Stock', 'Committed Stock', 'Available Stock', 'ID']],
                             use_container_width=True, hide_index=True)

                # Charting - focus on Physical or Available, or let user choose
                st.subheader("Inventory Charts")
                chart_type = st.selectbox("Chart Data:", ["Physical Stock", "Committed Stock", "Available Stock"])

                if not inv_df.empty:
                    fig_data = inv_df[inv_df[chart_type] > 0] # Only plot items with quantity > 0 for the selected type
                    if not fig_data.empty:
                        fig = px.bar(fig_data.sort_values(chart_type, ascending=False).head(20),
                                     x="Name", y=chart_type, color="Type",
                                     title=f"{chart_type} Levels (Top 20)", labels={'Name':'Item Name'})
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info(f"No items with {chart_type} > 0 to display in chart.")
                else:
                    st.info("No inventory data to chart.")
        else:
             st.info("Could not retrieve inventory data or inventory is empty.")


elif page == "History":
    st.header("📜 Simulation Event Log")
    if not st.session_state.simulation_status:
        st.warning("Simulation not initialized.")
    else:
        event_limit = st.slider("Number of recent events to display", 50, 500, 100, step=50)
        events = get_events(limit=event_limit)
        if events:
            events_df = pd.DataFrame(events)
            events_df['timestamp'] = pd.to_datetime(events_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            
            # Truncate details for better display in main table
            events_df['details_short'] = events_df['details'].apply(lambda x: json.dumps(x)[:100] + '...' if len(json.dumps(x)) > 100 else json.dumps(x))
            
            st.dataframe(events_df[['day', 'timestamp', 'event_type', 'details_short']], 
                         height=500, use_container_width=True, hide_index=True,
                         column_config={
                             "details_short": st.column_config.TextColumn("Details (Preview)")
                         })

            with st.expander("View Full Details for an Event"):
                event_id_to_show = st.selectbox("Select Event ID to see full details:", options=events_df['id'].tolist(), index=None)
                if event_id_to_show:
                    full_details = events_df[events_df['id'] == event_id_to_show]['details'].iloc[0]
                    st.json(full_details)


            order_events = events_df[events_df['event_type'] == 'order_received'].copy()
            if not order_events.empty:
                 order_events['day'] = order_events['day'].astype(int)
                 # Ensure 'details' is a dict and 'quantity' exists
                 order_events['quantity'] = order_events['details'].apply(
                     lambda x: x.get('quantity', 0) if isinstance(x, dict) else 0
                 )
                 orders_per_day = order_events.groupby('day')['quantity'].sum().reset_index(name='total_quantity_ordered')
                 if not orders_per_day.empty:
                    fig_orders = px.line(orders_per_day, x='day', y='total_quantity_ordered', title='Total Product Units Requested Per Day', markers=True)
                    st.plotly_chart(fig_orders, use_container_width=True)
        else:
            st.info("No simulation events recorded yet.")


elif page == "Setup & Data":
    st.header("⚙️ Setup & Data Management")
    st.subheader("Initial Conditions")
    st.info("Define the starting state of your factory simulation here. This will reset any current simulation.")

    default_initial_conditions = {
        "materials": [
            {"id": "mat-001", "name": "Plastic Filament Spool", "description": "Standard PLA 1kg"},
            {"id": "mat-002", "name": "Frame Component A"},
            {"id": "mat-003", "name": "Frame Component B"},
            {"id": "mat-004", "name": "Electronics Board v1"},
            {"id": "mat-005", "name": "Power Supply Unit"},
            {"id": "mat-006", "name": "Fasteners Pack (100pcs)"}
        ],
        "products": [
            {
                "id": "prod-001", "name": "Basic 3D Printer",
                "bom": [
                    {"material_id": "mat-001", "quantity": 1}, {"material_id": "mat-002", "quantity": 2},
                    {"material_id": "mat-003", "quantity": 2}, {"material_id": "mat-004", "quantity": 1},
                    {"material_id": "mat-005", "quantity": 1}, {"material_id": "mat-006", "quantity": 1}
                ], "production_time": 3 # days
            },
             {
                "id": "prod-002", "name": "Advanced 3D Printer",
                "bom": [
                    {"material_id": "mat-001", "quantity": 2}, {"material_id": "mat-002", "quantity": 4},
                    {"material_id": "mat-003", "quantity": 4}, {"material_id": "mat-004", "quantity": 2},
                    {"material_id": "mat-005", "quantity": 1}, {"material_id": "mat-006", "quantity": 2}
                ], "production_time": 5 # days
            }
        ],
        "providers": [
            {"id": "prov-001", "name": "Filament Inc.", "catalogue": [{"material_id": "mat-001", "price_per_unit": 20.0, "offered_unit_size": 1, "lead_time_days": 2}]},
            {"id": "prov-002", "name": "Frame Parts Co.", "catalogue": [
                {"material_id": "mat-002", "price_per_unit": 5.0, "offered_unit_size": 1, "lead_time_days": 5},
                {"material_id": "mat-003", "price_per_unit": 6.0, "offered_unit_size": 1, "lead_time_days": 5}
            ]},
            {"id": "prov-003", "name": "Electronics Hub", "catalogue": [
                {"material_id": "mat-004", "price_per_unit": 50.0, "offered_unit_size": 1, "lead_time_days": 7},
                {"material_id": "mat-005", "price_per_unit": 30.0, "offered_unit_size": 1, "lead_time_days": 4}
            ]},
            {"id": "prov-004", "name": "Hardware Supplies Ltd.", "catalogue": [{"material_id": "mat-006", "price_per_unit": 10.0, "offered_unit_size": 1, "lead_time_days": 3}]}
        ],
        "initial_inventory": { # Physical stock
            "mat-001": 50, "mat-002": 100, "mat-003": 100, "mat-004": 20, "mat-005": 30, "mat-006": 50, "prod-001": 5
        },
        "storage_capacity": 5000,
        "daily_production_capacity": 5, # units per day
        "random_order_config": {"min_orders_per_day": 0, "max_orders_per_day": 2, "min_qty_per_order": 1, "max_qty_per_order": 3}
    }

    edited_conditions_str = st.text_area(
        "Initial Conditions JSON",
        value=json.dumps(default_initial_conditions, indent=2),
        height=400,
        key="initial_cond_json"
    )

    if st.button("Initialize Simulation with Above Data", type="primary"):
        try:
            conditions_data = json.loads(edited_conditions_str)
            if initialize_simulation(conditions_data):
                 load_base_data.clear()
                 load_inventory_data.clear()
                 st.rerun()
        except json.JSONDecodeError:
            st.error("Invalid JSON format in Initial Conditions.")
        except Exception as e:
            st.error(f"Error initializing simulation: {e}")

    st.divider()
    st.subheader("Data Export / Import")
    col_exp, col_imp = st.columns(2)

    with col_exp:
        st.write("Export the current simulation state, events, and definitions to a JSON file.")
        if st.session_state.simulation_status: # Only show if sim is running
            if st.button("Prepare Export Data"):
                exported_data_content = export_data()
                if exported_data_content:
                    current_day_val = st.session_state.simulation_status.get('current_day', 0)
                    st.download_button(
                        label="Download Exported Data (JSON)",
                        data=json.dumps(exported_data_content, indent=2),
                        file_name=f"mrp_simulation_export_day_{current_day_val}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                        mime="application/json",
                    )
        else:
            st.info("Initialize simulation to enable data export.")


    with col_imp:
        st.write("Import a previously exported JSON file. This will **overwrite** the current simulation.")
        uploaded_file = st.file_uploader("Choose a JSON file to import", type="json")
        if uploaded_file is not None:
            try:
                import_file_content = uploaded_file.getvalue().decode("utf-8")
                import_json_data = json.loads(import_file_content)
                # Basic validation of the imported structure
                if "simulation_state" in import_json_data and "products" in import_json_data and "materials" in import_json_data:
                     if st.button("Confirm Import Data", type="danger"):
                         if import_data(import_json_data): # API client's import_data now handles rerun
                             load_base_data.clear()
                             load_inventory_data.clear()
                             # st.rerun() # Rerun is now handled by api_client on success
                else:
                    st.error("Uploaded file does not appear to be a valid simulation export (missing key fields).")
            except json.JSONDecodeError:
                st.error("Invalid JSON file.")
            except Exception as e:
                st.error(f"Error processing import file: {e}")