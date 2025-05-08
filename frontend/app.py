import streamlit as st
import pandas as pd
import plotly.express as px
import json
from datetime import datetime

from api_client import (
    get_simulation_status, initialize_simulation, advance_day,
    get_materials, get_products, get_providers, get_inventory,
    get_production_orders, start_production,
    get_purchase_orders, create_purchase_order,
    get_events, export_data, import_data
)

st.set_page_config(
    page_title="MRP Factory Simulation",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded"
)

@st.cache_data(ttl=300)
def load_base_data():
    materials = get_materials()
    products = get_products()
    providers = get_providers()
    return materials, products, providers

def format_bom(bom_list, materials_dict):
    if not bom_list: return "No BOM defined"
    lines = []
    for item in bom_list:
        mat_name = materials_dict.get(item['material_id'], {}).get('name', item['material_id'])
        lines.append(f"- {mat_name}: {item['quantity']}")
    return "\n".join(lines)

def format_catalogue(catalogue_list, materials_dict):
    if not catalogue_list: return "No offerings defined"
    lines = []
    for item in catalogue_list:
        mat_name = materials_dict.get(item['material_id'], {}).get('name', item['material_id'])
        lines.append(f"- {mat_name}: €{item['price_per_unit']:.2f}/unit (Lead: {item['lead_time_days']} days)")
    return "\n".join(lines)

if 'simulation_status' not in st.session_state:
    st.session_state.simulation_status = None
if 'selected_prod_orders' not in st.session_state:
    st.session_state.selected_prod_orders = []

materials, products, providers = load_base_data()
materials_dict = {m['id']: m for m in materials}
products_dict = {p['id']: p for p in products}
providers_dict = {p['id']: p for p in providers}

st.sidebar.title("🏭 MRP Factory Simulation")
st.session_state.simulation_status = get_simulation_status()

if st.session_state.simulation_status:
    status = st.session_state.simulation_status
    st.sidebar.metric("Current Day", status.get('current_day', 'N/A'))
    st.sidebar.metric("Pending Production", status.get('pending_production_orders', 'N/A'))
    st.sidebar.metric("In Progress Production", status.get('in_progress_production_orders', 'N/A'))
    st.sidebar.metric("Pending Purchases", status.get('pending_purchase_orders', 'N/A'))

    inv_units = status.get('total_inventory_units', 0)
    capacity = status.get('storage_capacity', 1)
    util = status.get('storage_utilization', 0)

    st.sidebar.progress(util / 100, text=f"Storage: {inv_units}/{capacity} ({util:.1f}%)")

    if st.sidebar.button("Advance 1 Day", use_container_width=True, type="primary"):
        advance_day()
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

if page == "Dashboard":
    st.header("🏭 Dashboard Overview")
    if st.session_state.simulation_status:
        status = st.session_state.simulation_status
        col1, col2, col3 = st.columns(3)
        col1.metric("Current Day", status.get('current_day', 'N/A'))
        col2.metric("Pending Production Orders", status.get('pending_production_orders', 'N/A'))
        col3.metric("Pending Purchase Orders", status.get('pending_purchase_orders', 'N/A'))

        st.subheader("Recent Events")
        events = get_events(limit=10)
        if events:
            events_df = pd.DataFrame(events)
            events_df['timestamp'] = pd.to_datetime(events_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            events_df_display = events_df[['day', 'timestamp', 'event_type', 'details']].copy()
            events_df_display['details'] = events_df_display['details'].apply(lambda x: json.dumps(x, indent=2))
            st.dataframe(events_df_display, use_container_width=True, height=300)
        else:
            st.info("No simulation events recorded yet.")

        st.subheader("Current Inventory")
        inventory_data = get_inventory()
        if inventory_data:
            inv_items = []
            for item_id, qty in inventory_data.items():
                item_name = "Unknown Item"
                item_type = "Unknown"
                if item_id in materials_dict:
                    item_name = materials_dict[item_id]['name']
                    item_type = "Material"
                elif item_id in products_dict:
                     item_name = products_dict[item_id]['name']
                     item_type = "Product"
                inv_items.append({"ID": item_id, "Name": item_name, "Type": item_type, "Quantity": qty})

            inv_df = pd.DataFrame(inv_items)
            if not inv_df.empty:
                fig = px.bar(inv_df.sort_values("Quantity", ascending=False),
                             x="Name", y="Quantity", color="Type",
                             title="Inventory Levels", labels={'Name':'Item Name'})
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Inventory is currently empty.")
        else:
            st.info("Could not fetch inventory data.")

    else:
        st.warning("Simulation not initialized. Go to 'Setup & Data' to start.")


elif page == "Production":
    st.header("🛠️ Production Management")

    if not st.session_state.simulation_status:
        st.warning("Simulation not initialized.")
    else:
        tab1, tab2, tab3 = st.tabs(["Pending Orders", "In Progress Orders", "Completed Orders"])

        with tab1:
            st.subheader("Pending Production Orders")
            pending_orders = get_production_orders(status="Pending")
            if pending_orders:
                orders_df = pd.DataFrame(pending_orders)
                orders_df['product_name'] = orders_df['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                orders_df['required_materials_display'] = orders_df.apply(
                     lambda row: format_bom([{'material_id': mid, 'quantity': qty} for mid, qty in row['required_materials'].items()], materials_dict) if row['required_materials'] else "N/A",
                     axis=1
                 )
                orders_df['requested_date'] = pd.to_datetime(orders_df['requested_date']).dt.strftime('%Y-%m-%d')

                orders_df_display = orders_df[['id', 'product_name', 'quantity', 'requested_date', 'required_materials_display']].rename(
                    columns={'id':'Order ID', 'product_name':'Product', 'quantity':'Qty', 'requested_date':'Requested', 'required_materials_display':'Materials Needed'}
                )
                orders_df_display.insert(0, 'Select', False)

                edited_df = st.data_editor(
                    orders_df_display,
                    column_config={
                        "Select": st.column_config.CheckboxColumn(default=False),
                        "Order ID": st.column_config.TextColumn(disabled=True),
                        "Product": st.column_config.TextColumn(disabled=True),
                        "Qty": st.column_config.NumberColumn(disabled=True),
                        "Requested": st.column_config.TextColumn(disabled=True),
                        "Materials Needed": st.column_config.TextColumn(width="large", disabled=True)
                    },
                    hide_index=True,
                    key="prod_order_selector"
                )

                selected_mask = edited_df['Select'].fillna(False)
                selected_order_ids = orders_df.loc[selected_mask.tolist(), 'id'].tolist()

                if st.button(f"Start Production for Selected ({len(selected_order_ids)}) Orders", disabled=not selected_order_ids):
                     results = start_production(selected_order_ids)
                     if results:
                         st.rerun()
            else:
                st.info("No pending production orders.")

        with tab2:
            st.subheader("Production Orders In Progress")
            in_progress_orders = get_production_orders(status="In Progress")
            if in_progress_orders:
                 orders_df = pd.DataFrame(in_progress_orders)
                 orders_df['product_name'] = orders_df['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df['started_at'] = pd.to_datetime(orders_df['started_at']).dt.strftime('%Y-%m-%d %H:%M')
                 orders_df_display = orders_df[['id', 'product_name', 'quantity', 'started_at']].rename(
                     columns={'id':'Order ID', 'product_name':'Product', 'quantity':'Qty', 'started_at':'Started At'}
                 )
                 st.dataframe(orders_df_display, use_container_width=True, hide_index=True)
            else:
                 st.info("No production orders currently in progress.")

        with tab3:
             st.subheader("Completed Production Orders")
             completed_orders = get_production_orders(status="Completed")
             if completed_orders:
                 orders_df = pd.DataFrame(completed_orders)
                 orders_df['product_name'] = orders_df['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df['completed_at'] = pd.to_datetime(orders_df['completed_at']).dt.strftime('%Y-%m-%d %H:%M')
                 orders_df_display = orders_df[['id', 'product_name', 'quantity', 'completed_at']].rename(
                     columns={'id':'Order ID', 'product_name':'Product', 'quantity':'Qty', 'completed_at':'Completed At'}
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
                material_options = {m['id']: f"{m['name']} ({m['id']})" for m in materials}
                selected_material_id = st.selectbox("Select Material", options=list(material_options.keys()), format_func=lambda x: material_options[x])

                available_providers = []
                for p in providers:
                    for offer in p.get('catalogue', []):
                        if offer.get('material_id') == selected_material_id:
                            available_providers.append(p)
                            break

                if not available_providers:
                    st.warning(f"No provider offers the selected material: {materials_dict.get(selected_material_id, {}).get('name', selected_material_id)}")
                    provider_options = {}
                    selected_provider_id = None
                    st.selectbox("Select Provider", [], disabled=True)
                    st.number_input("Quantity", min_value=1, value=1, step=1, disabled=True)
                    submit_disabled = True

                else:
                    provider_options = {p['id']: f"{p['name']} ({p['id']})" for p in available_providers}
                    selected_provider_id = st.selectbox("Select Provider", options=list(provider_options.keys()), format_func=lambda x: provider_options[x])

                    if selected_provider_id:
                         prov = providers_dict.get(selected_provider_id)
                         offering = next((o for o in prov.get('catalogue', []) if o['material_id'] == selected_material_id), None)
                         if offering:
                              st.info(f"Provider Offering: Price: €{offering['price_per_unit']:.2f}/unit, Lead Time: {offering['lead_time_days']} days")
                         else:
                              st.error("Error: Selected provider does not seem to offer this material (data inconsistency?).")

                    quantity = st.number_input("Quantity (units)", min_value=1, value=10, step=1)
                    submit_disabled = False


                submitted = st.form_submit_button("Place Purchase Order", disabled=submit_disabled)
                if submitted and selected_material_id and selected_provider_id and quantity > 0:
                    if create_purchase_order(selected_material_id, selected_provider_id, quantity):
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
            pos_df = pd.DataFrame(pending_pos)
            pos_df['material_name'] = pos_df['material_id'].apply(lambda x: materials_dict.get(x, {}).get('name', x))
            pos_df['provider_name'] = pos_df['provider_id'].apply(lambda x: providers_dict.get(x, {}).get('name', x))
            pos_df['order_date'] = pd.to_datetime(pos_df['order_date']).dt.strftime('%Y-%m-%d %H:%M')
            pos_df['expected_arrival_date'] = pd.to_datetime(pos_df['expected_arrival_date']).dt.strftime('%Y-%m-%d')

            pos_df_display = pos_df[['id', 'material_name', 'quantity_ordered', 'provider_name', 'order_date', 'expected_arrival_date']].rename(
                 columns={'id':'PO ID', 'material_name':'Material', 'quantity_ordered':'Qty', 'provider_name':'Provider', 'order_date':'Ordered', 'expected_arrival_date':'ETA'}
             )
            st.dataframe(pos_df_display, use_container_width=True, hide_index=True)
        else:
            st.info("No pending purchase orders.")


elif page == "Inventory":
    st.header("📦 Inventory Status")

    if not st.session_state.simulation_status:
         st.warning("Simulation not initialized.")
    else:
        inventory_data = get_inventory()
        if inventory_data is not None:
            inv_items = []
            for item_id, qty in inventory_data.items():
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
                inv_items.append({"ID": item_id, "Name": item_name, "Type": item_type, "Quantity": qty, "Description": description})

            if inv_items:
                 inv_df = pd.DataFrame(inv_items)
                 st.dataframe(inv_df[['Name', 'Type', 'Quantity', 'ID', 'Description']], use_container_width=True, hide_index=True)

                 fig = px.pie(inv_df, values='Quantity', names='Name', title='Inventory Composition by Quantity',
                             hover_data=['Type'])
                 st.plotly_chart(fig, use_container_width=True)

            else:
                 st.info("Inventory is currently empty.")
        else:
             st.info("Could not retrieve inventory data.")


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
            events_df_display = events_df[['day', 'timestamp', 'event_type', 'details']].copy()
            events_df_display['details'] = events_df_display['details'].apply(lambda x: json.dumps(x))
            st.dataframe(events_df_display, height=500, use_container_width=True, hide_index=True)

            order_events = events_df[events_df['event_type'] == 'order_received'].copy()
            if not order_events.empty:
                 order_events['day'] = order_events['day'].astype(int)
                 order_events['quantity'] = order_events['details'].apply(lambda x: x.get('quantity', 0) if isinstance(x, dict) else 0)
                 orders_per_day = order_events.groupby('day')['quantity'].sum().reset_index(name='total_quantity_ordered')
                 fig_orders = px.line(orders_per_day, x='day', y='total_quantity_ordered', title='Total Product Units Ordered Per Day', markers=True)
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
                    {"material_id": "mat-001", "quantity": 1},
                    {"material_id": "mat-002", "quantity": 2},
                    {"material_id": "mat-003", "quantity": 2},
                    {"material_id": "mat-004", "quantity": 1},
                    {"material_id": "mat-005", "quantity": 1},
                    {"material_id": "mat-006", "quantity": 1}
                ],
                "production_time": 3
            },
             {
                "id": "prod-002", "name": "Advanced 3D Printer",
                "bom": [
                    {"material_id": "mat-001", "quantity": 2},
                    {"material_id": "mat-002", "quantity": 4},
                    {"material_id": "mat-003", "quantity": 4},
                    {"material_id": "mat-004", "quantity": 2},
                    {"material_id": "mat-005", "quantity": 1},
                    {"material_id": "mat-006", "quantity": 2}
                ],
                "production_time": 5
            }
        ],
        "providers": [
            {
                "id": "prov-001", "name": "Filament Inc.",
                "catalogue": [
                    {"material_id": "mat-001", "price_per_unit": 20.0, "offered_unit_size": 1, "lead_time_days": 2}
                ]
            },
            {
                "id": "prov-002", "name": "Frame Parts Co.",
                "catalogue": [
                    {"material_id": "mat-002", "price_per_unit": 5.0, "offered_unit_size": 1, "lead_time_days": 5},
                    {"material_id": "mat-003", "price_per_unit": 6.0, "offered_unit_size": 1, "lead_time_days": 5}
                ]
            },
             {
                "id": "prov-003", "name": "Electronics Hub",
                "catalogue": [
                    {"material_id": "mat-004", "price_per_unit": 50.0, "offered_unit_size": 1, "lead_time_days": 7},
                     {"material_id": "mat-005", "price_per_unit": 30.0, "offered_unit_size": 1, "lead_time_days": 4}
                ]
            },
             {
                "id": "prov-004", "name": "Hardware Supplies Ltd.",
                "catalogue": [
                    {"material_id": "mat-006", "price_per_unit": 10.0, "offered_unit_size": 1, "lead_time_days": 3}
                ]
            }
        ],
        "initial_inventory": {
            "mat-001": 50, "mat-002": 100, "mat-003": 100,
            "mat-004": 20, "mat-005": 30, "mat-006": 50,
            "prod-001": 5
        },
        "storage_capacity": 5000,
        "daily_production_capacity": 5,
         "random_order_config": {
            "min_orders_per_day": 0, "max_orders_per_day": 2,
            "min_qty_per_order": 1, "max_qty_per_order": 3
        }
    }

    edited_conditions = st.text_area(
        "Initial Conditions JSON",
        value=json.dumps(default_initial_conditions, indent=2),
        height=400,
        key="initial_cond_json"
    )

    if st.button("Initialize Simulation with Above Data", type="primary"):
        try:
            conditions_data = json.loads(edited_conditions)
            if initialize_simulation(conditions_data):
                 load_base_data.clear()
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
        if st.button("Export Data"):
            exported_data = export_data()
            if exported_data:
                 current_day_val = st.session_state.simulation_status.get('current_day', 0) if st.session_state.simulation_status else 0
                 st.download_button(
                     label="Download Exported Data (JSON)",
                     data=json.dumps(exported_data, indent=2),
                     file_name=f"mrp_simulation_export_day_{current_day_val}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                     mime="application/json",
                 )

    with col_imp:
        st.write("Import a previously exported JSON file. This will **overwrite** the current simulation.")
        uploaded_file = st.file_uploader("Choose a JSON file to import", type="json")
        if uploaded_file is not None:
            try:
                import_file_content = uploaded_file.getvalue().decode("utf-8")
                import_json_data = json.loads(import_file_content)
                if "simulation_state" in import_json_data and "products" in import_json_data:
                     if st.button("Confirm Import Data", type="danger"):
                         if import_data(import_json_data):
                             load_base_data.clear()
                             st.rerun()
                else:
                    st.error("Uploaded file does not appear to be a valid simulation export.")
            except json.JSONDecodeError:
                st.error("Invalid JSON file.")
            except Exception as e:
                st.error(f"Error processing import file: {e}")