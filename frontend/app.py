import streamlit as st
import pandas as pd
import plotly.express as px
import json
from datetime import datetime

from api_client import (
    get_simulation_status, initialize_simulation, advance_day,
    get_materials, get_products, get_providers, get_inventory,
    get_production_orders, start_production, accept_production_order, 
    order_missing_materials_for_production_order, 
    get_purchase_orders, create_purchase_order,
    get_events, export_data, import_data
)

st.set_page_config(
    page_title="MRP Factory Simulation",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded"
)

@st.cache_data(ttl=60)
def load_base_data():
    materials = get_materials()
    products = get_products()
    providers = get_providers()
    return materials, products, providers

@st.cache_data(ttl=10) 
def load_inventory_data_cached():
    return get_inventory()

def format_bom(bom_list, materials_dict_local, header=""):
    if not bom_list: return f"{header}No BOM defined" if header else "No BOM defined"
    lines = [header] if header else []
    for item in bom_list: # bom_list is expected to be list of dicts like {'material_id': id, 'quantity': qty}
        mat_id = item.get('material_id')
        qty = item.get('quantity', 'N/A')
        mat_name = materials_dict_local.get(mat_id, {}).get('name', mat_id)
        lines.append(f"- {mat_name}: {qty}")
    return "\n".join(lines)

def format_material_list_with_stock_check(materials_needed_dict, physical_stock_levels, materials_dict_local):
    # materials_needed_dict: Dict[str, int] e.g. {'mat-001': 10}
    # physical_stock_levels: Dict[str, int] e.g. {'mat-001': 5} (actual physical stock)
    if not materials_needed_dict:
        return "N/A (No materials specified)"
    
    lines = []
    shortage_for_this_order = False
    for mat_id, qty_needed in materials_needed_dict.items():
        mat_name = materials_dict_local.get(mat_id, {}).get('name', mat_id)
        available_qty = physical_stock_levels.get(mat_id, 0) # Physical stock
        color = "green" if available_qty >= qty_needed else "red"
        if available_qty < qty_needed:
            shortage_for_this_order = True
        lines.append(f"<span style='color:{color};'>- {mat_name}: Need {qty_needed}, Have (Phys) {available_qty}</span>")
    
    # Prepend a status icon/message based on overall availability for this order
    # status_message = "<span style='color:red;'>Shortages Exist</span>" if shortage_for_this_order else "<span style='color:green;'>All Materials Physically Available</span>"
    # return status_message + "<br>" + "<br>".join(lines)
    return "<br>".join(lines), shortage_for_this_order


def format_catalogue(catalogue_list, materials_dict_local):
    if not catalogue_list: return "No offerings defined"
    lines = []
    for item in catalogue_list:
        mat_name = materials_dict_local.get(item['material_id'], {}).get('name', item['material_id'])
        lines.append(f"- {mat_name}: €{item['price_per_unit']:.2f}/unit (Lead: {item['lead_time_days']} days)")
    return "\n".join(lines)

# --- Load Initial Data & Session State ---
if 'simulation_status' not in st.session_state:
    st.session_state.simulation_status = None

materials_list_data, products_list_data, providers_list_data = load_base_data()
materials_dict = {m['id']: m for m in materials_list_data if m} if materials_list_data else {}
products_dict = {p['id']: p for p in products_list_data if p} if products_list_data else {}
providers_dict = {p['id']: p for p in providers_list_data if p} if providers_list_data else {}

current_inventory_status_response = load_inventory_data_cached() 
inventory_items_detailed = current_inventory_status_response.get('items', {}) if current_inventory_status_response else {}

# Extract physical stock for quick checks (e.g., on Pending orders page)
physical_stock_snapshot = {
    item_id: details.get('physical', 0) 
    for item_id, details in inventory_items_detailed.items()
}

# --- Sidebar ---
st.sidebar.title("🏭 MRP Factory Simulation")
st.session_state.simulation_status = get_simulation_status()

if st.session_state.simulation_status:
    status = st.session_state.simulation_status
    st.sidebar.metric("Current Day", status.get('current_day', 'N/A'))
    st.sidebar.metric("Pending Requests", status.get('pending_production_orders', 'N/A'))
    st.sidebar.metric("Accepted Orders", status.get('accepted_production_orders', 'N/A')) 
    st.sidebar.metric("In Progress Orders", status.get('in_progress_production_orders', 'N/A'))
    st.sidebar.metric("Pending POs", status.get('pending_purchase_orders', 'N/A'))

    inv_units = status.get('total_inventory_units', 0) 
    capacity = status.get('storage_capacity', 1)
    util = status.get('storage_utilization', 0)
    st.sidebar.progress(util / 100 if capacity > 0 else 0, text=f"Storage: {inv_units}/{capacity} ({util:.1f}%)")

    if st.sidebar.button("Advance 1 Day", use_container_width=True, type="primary"):
        if advance_day():
            load_inventory_data_cached.clear(); st.rerun()
else:
    st.sidebar.warning("Simulation not running or API unreachable. Initialize first.")

st.sidebar.divider()
st.sidebar.header("Navigation")
page = st.sidebar.radio("Go to",
                        ["Dashboard", "Production", "Purchasing", "Inventory", "History", "Setup & Data"],
                        label_visibility="collapsed")
st.sidebar.divider()
st.sidebar.info("Manage your 3D printer factory day by day.")

# --- Main Page Content ---
if page == "Dashboard":
    st.header("🏭 Dashboard Overview")
    if st.session_state.simulation_status:
        status = st.session_state.simulation_status
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Current Day", status.get('current_day', 'N/A'))
        col2.metric("Pending Requests", status.get('pending_production_orders', 'N/A'))
        col3.metric("Accepted Orders", status.get('accepted_production_orders', 'N/A'))
        col4.metric("Pending POs", status.get('pending_purchase_orders', 'N/A'))

        st.subheader("Recent Events (Last 10)")
        events = get_events(limit=10)
        if events:
            events_df = pd.DataFrame(events)[['day', 'timestamp', 'event_type', 'details']]
            events_df['timestamp'] = pd.to_datetime(events_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            st.dataframe(events_df, use_container_width=True, height=300, 
                         column_config={"details": st.column_config.TextColumn("Details", width="large")})
        else: st.info("No simulation events recorded yet.")

        st.subheader("Current Inventory Snapshot (Physical Stock)")
        if inventory_items_detailed:
            physical_inv_list = [{"ID": item_id, "Name": details.get('name',item_id), 
                                  "Type": details.get('type', 'Unknown'), "Quantity": details.get('physical',0)}
                                 for item_id, details in inventory_items_detailed.items() if details.get('physical', 0) > 0]
            if physical_inv_list:
                inv_df = pd.DataFrame(physical_inv_list)
                fig = px.bar(inv_df.sort_values("Quantity", ascending=False).head(15), x="Name", y="Quantity", color="Type",
                             title="Top 15 Items - Physical Stock", labels={'Name':'Item Name'})
                st.plotly_chart(fig, use_container_width=True)
            else: st.info("Physical inventory is currently empty.")
        else: st.info("Could not fetch inventory data or inventory is empty.")
    else: st.warning("Simulation not initialized. Go to 'Setup & Data' to start.")


elif page == "Production":
    st.header("🛠️ Production Management")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        tab_titles = ["Pending Requests", "Accepted Orders", "In Progress", "Completed", "Fulfilled (from Stock)"]
        pending_tab, accepted_tab, in_progress_tab, completed_tab, fulfilled_tab = st.tabs(tab_titles)

        with pending_tab: 
            st.subheader("Pending Production Requests")
            st.markdown("""
            Review new production requests. 
            - **Accept Request**: Attempts to fulfill from existing finished product stock first. If not fully available, the remaining quantity is accepted for future production (materials are *not* committed yet).
            - **Order Missing Materials**: Check physical stock for required materials and place Purchase Orders for any shortages *for this specific request*.
            """)
            pending_orders_data = get_production_orders(status="Pending")
            if pending_orders_data:
                for order in pending_orders_data:
                    product_name = products_dict.get(order['product_id'], {}).get('name', order['product_id'])
                    st.markdown(f"#### Order ID: `{order['id']}`")
                    
                    col_details, col_actions = st.columns([3,1])
                    with col_details:
                        st.markdown(f"**Product:** {product_name} | **Qty:** {order['quantity']} | **Requested:** {pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d')}")
                        
                        # Check finished product stock
                        finished_prod_stock = physical_stock_snapshot.get(order['product_id'], 0)
                        if finished_prod_stock > 0:
                            st.info(f"ℹ️ **Note:** {finished_prod_stock} units of '{product_name}' are currently in physical stock.")
                        
                        # Display material requirements and availability
                        if order.get('required_materials'):
                            materials_display_html, shortage_exists = format_material_list_with_stock_check(
                                order['required_materials'],
                                physical_stock_snapshot, # Pass the direct physical stock dict
                                materials_dict
                            )
                            st.markdown("**Material Availability (Need vs. Physical Stock):**")
                            st.markdown(materials_display_html, unsafe_allow_html=True)
                        else:
                            shortage_exists = False # Or assume shortage if no materials listed but should be
                            st.warning("No required materials listed for this pending order.")

                    with col_actions:
                        if st.button("✅ Accept Request", key=f"accept_{order['id']}", use_container_width=True):
                            if accept_production_order(order['id']): # Backend handles fulfillment/acceptance logic
                                load_inventory_data_cached.clear(); st.rerun()
                        
                        if order.get('required_materials') and shortage_exists : # Only show if materials are listed & shortage
                             if st.button("🛒 Order Missing Materials", key=f"order_missing_{order['id']}", use_container_width=True):
                                if order_missing_materials_for_production_order(order['id']):
                                    load_inventory_data_cached.clear(); st.rerun()
                    st.markdown("---")
            else: st.info("No pending production requests.")

        with accepted_tab: 
            st.subheader("Accepted Orders")
            st.markdown("These orders are approved for production. Materials will be checked and committed from physical stock when you 'Send to Production'.")
            accepted_orders_data = get_production_orders(status="Accepted")
            if accepted_orders_data:
                df_data = []
                for order in accepted_orders_data:
                    df_data.append({
                        "Order ID": order['id'], 
                        "Product": products_dict.get(order['product_id'], {}).get('name', order['product_id']),
                        "Qty": order['quantity'],
                        "Requested": pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d'),
                        "Required Materials": format_bom(
                            [{'material_id': mid, 'quantity': qty} for mid, qty in order.get('required_materials', {}).items()],
                            materials_dict
                        ) if order.get('required_materials') else "N/A",
                        "_order_id_internal": order['id'] 
                    })
                if df_data:
                    orders_df = pd.DataFrame(df_data)
                    orders_df.insert(0, 'Select', False)
                    edited_df = st.data_editor(
                        orders_df[['Select', 'Order ID', 'Product', 'Qty', 'Requested', 'Required Materials']],
                        column_config={ "Select": st.column_config.CheckboxColumn(default=False), 
                                       "Order ID": st.column_config.TextColumn(disabled=True),
                                       "Required Materials": st.column_config.TextColumn(width="large", disabled=True)}, 
                        use_container_width=True, hide_index=True, key="accepted_order_selector"
                    )
                    selected_order_ids = orders_df.loc[edited_df['Select'].fillna(False).tolist(), '_order_id_internal'].tolist()
                    if st.button(f"➡️ Send ({len(selected_order_ids)}) to Production", disabled=not selected_order_ids, type="primary"):
                        if start_production(selected_order_ids): # Backend now checks/commits materials
                            load_inventory_data_cached.clear(); st.rerun()
                else: st.info("No orders currently in 'Accepted' state.")
            else: st.info("No orders currently in 'Accepted' state.")

        with in_progress_tab: 
            st.subheader("In Progress Orders")
            st.markdown("These orders are currently being manufactured. Materials have been committed.")
            # ... (same as before) ...
            in_progress_orders = get_production_orders(status="In Progress")
            if in_progress_orders:
                 orders_df_prog = pd.DataFrame(in_progress_orders)
                 orders_df_prog['Product'] = orders_df_prog['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df_prog['Started At'] = orders_df_prog['started_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_prog['Committed Materials'] = orders_df_prog['committed_materials'].apply(lambda x: format_bom([{'material_id': k, 'quantity': v} for k,v in x.items()], materials_dict) if x else "N/A")
                 st.dataframe(orders_df_prog[['id', 'Product', 'quantity', 'Started At', 'Committed Materials']].rename(columns={'id':'Order ID', 'quantity':'Qty'}), 
                              use_container_width=True, hide_index=True)
            else: st.info("No production orders currently in progress.")


        with completed_tab: 
             st.subheader("Completed Production Orders (Manufactured)")
             # ... (same as before) ...
             completed_orders = get_production_orders(status="Completed")
             if completed_orders:
                 orders_df_comp = pd.DataFrame(completed_orders)
                 orders_df_comp['Product'] = orders_df_comp['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df_comp['Completed At'] = orders_df_comp['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 st.dataframe(orders_df_comp[['id', 'Product', 'quantity', 'Completed At']].rename(columns={'id':'Order ID', 'quantity':'Qty'}), 
                              use_container_width=True, hide_index=True)
             else: st.info("No production orders have been completed through manufacturing yet.")
        
        with fulfilled_tab: 
            st.subheader("Orders Fulfilled Directly From Stock")
            st.markdown("These orders (or parts of original demand) were fulfilled using existing finished product stock instead of new production.")
            fulfilled_orders_data = get_production_orders(status="Fulfilled") 
            if fulfilled_orders_data:
                orders_df_ful = pd.DataFrame(fulfilled_orders_data)
                orders_df_ful['Product'] = orders_df_ful['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                orders_df_ful['Fulfilled At'] = orders_df_ful['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                st.dataframe(orders_df_ful[['id', 'Product', 'quantity', 'Fulfilled At']].rename(columns={'id':'Order ID', 'quantity':'Qty Fulfilled'}), 
                              use_container_width=True, hide_index=True)
            else:
                st.info("No orders have been marked as 'Fulfilled' from stock. Check Event Log for 'product_shipped_from_stock' events for direct demand fulfillment not tied to a specific production order ID.")


elif page == "Purchasing":
    # ... (no major changes for this request, but ensure materials_dict is passed to format_catalogue)
    st.header("🛒 Material Purchasing")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    elif not materials_list_data or not providers_list_data: st.warning("No materials or providers defined.")
    else:
        col1, col2 = st.columns([2, 1])
        with col1:
            st.subheader("Create Purchase Order")
            with st.form("purchase_order_form"):
                mat_opts = {m['id']: f"{m['name']} (ID: {m['id']})" for m in materials_list_data}
                sel_mat_id = st.selectbox("Material", options=list(mat_opts.keys()), format_func=lambda x: mat_opts[x])
                avail_provs = [p for p_id, p in providers_dict.items() if any(o['material_id'] == sel_mat_id for o in p.get('catalogue',[]))] if sel_mat_id else []
                
                if not avail_provs:
                    st.warning(f"No provider offers: {materials_dict.get(sel_mat_id,{}).get('name', sel_mat_id)}")
                    sel_prov_id = None; st.selectbox("Provider", [], disabled=True); st.number_input("Qty", 1,1,1,disabled=True); submit_dis = True
                else:
                    prov_opts = {p['id']: f"{p['name']} (ID: {p['id']})" for p in avail_provs}
                    sel_prov_id = st.selectbox("Provider", options=list(prov_opts.keys()), format_func=lambda x: prov_opts[x])
                    if sel_prov_id:
                         prov_detail = providers_dict.get(sel_prov_id)
                         offering = next((o for o in prov_detail.get('catalogue',[]) if o['material_id'] == sel_mat_id), None)
                         if offering: st.info(f"Price: €{offering['price_per_unit']:.2f}, Lead: {offering['lead_time_days']} days")
                    qty_val = st.number_input("Quantity (units)", 1, 10, 1)
                    submit_dis = not sel_prov_id
                if st.form_submit_button("Place Purchase Order", disabled=submit_dis) and sel_mat_id and sel_prov_id and qty_val > 0:
                    if create_purchase_order(sel_mat_id, sel_prov_id, qty_val):
                        load_inventory_data_cached.clear(); st.rerun()
        with col2:
            st.subheader("Providers & Offerings")
            if providers_list_data:
                for prov_item in providers_list_data:
                    with st.expander(f"{prov_item['name']}"):
                        st.write(f"ID: {prov_item['id']}")
                        st.markdown(format_catalogue(prov_item.get('catalogue',[]), materials_dict))
            else: st.info("No providers defined.")
        st.divider()
        st.subheader("Pending Purchase Orders")
        pending_pos = get_purchase_orders(status="Ordered")
        if pending_pos:
            pos_df = pd.DataFrame([{"PO ID": po['id'], "Material": materials_dict.get(po['material_id'],{}).get('name', po['material_id']),
                                   "Qty": po['quantity_ordered'], "Provider": providers_dict.get(po['provider_id'],{}).get('name', po['provider_id']),
                                   "Ordered": pd.to_datetime(po['order_date']).strftime('%Y-%m-%d %H:%M'),
                                   "ETA": pd.to_datetime(po['expected_arrival_date']).strftime('%Y-%m-%d')} for po in pending_pos])
            st.dataframe(pos_df, use_container_width=True, hide_index=True)
        else: st.info("No pending purchase orders.")


elif page == "Inventory":
    st.header("📦 Inventory Status")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        if inventory_items_detailed:
            inv_list = [{"ID": item_id, "Name": det.get('name',item_id), "Type": det.get('type',"Unk"),
                           "Physical": det.get('physical',0), "Committed": det.get('committed',0),
                           "On Order": det.get('on_order',0), "Projected": det.get('projected_available',0)}
                          for item_id, det in inventory_items_detailed.items()]
            if inv_list:
                 inv_df = pd.DataFrame(inv_list)
                 st.dataframe(inv_df[['Name','Type','Physical','Committed','On Order','Projected','ID']], hide_index=True, use_container_width=True)
                 st.subheader("Inventory Charts")
                 chart_cols = ["Physical","Committed","On Order","Projected"]
                 chart_sel = st.selectbox("Chart Data:", chart_cols, index=0)
                 
                 fig_data = inv_df.copy()
                 if chart_sel == "On Order": fig_data = fig_data[fig_data["Type"] == "Material"] # On Order only for materials
                 fig_data = fig_data[fig_data[chart_sel] != 0] # Filter out zero values for the selected metric

                 if not fig_data.empty:
                    fig = px.bar(fig_data.sort_values(chart_sel, ascending=False).head(20),
                                 x="Name", y=chart_sel, color="Type", title=f"{chart_sel} Levels (Top 20)", labels={'Name':'Item'})
                    st.plotly_chart(fig, use_container_width=True)
                 else: st.info(f"No items with non-zero {chart_sel} data to display.")
            else: st.info("Inventory is currently empty.")
        else: st.info("Could not retrieve inventory data or inventory is empty.")


elif page == "History":
    # ... (Code from previous `app.py` for History, ensure it's the latest from previous step)
    st.header("📜 Simulation Event Log")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        event_limit = st.slider("Number of recent events", 50, 500, 100, 50)
        events = get_events(limit=event_limit)
        if events:
            df = pd.DataFrame(events); df['timestamp'] = pd.to_datetime(df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            df['details_short'] = df['details'].apply(lambda x: (json.dumps(x)[:100] + '...') if len(json.dumps(x)) > 100 else json.dumps(x))
            st.dataframe(df[['day','timestamp','event_type','details_short']], height=500, hide_index=True, use_container_width=True,
                         column_config={"details_short": st.column_config.TextColumn("Details Preview")})
            with st.expander("View Full Event Details"):
                sel_ev_id = st.selectbox("Event ID:", options=df['id'].tolist(), index=None)
                if sel_ev_id: st.json(df[df['id'] == sel_ev_id]['details'].iloc[0])
            
            # Demand vs Fulfillment Charting
            demand_events = df[df['event_type'].isin(['order_received_for_production', 'product_shipped_from_stock'])].copy()
            if not demand_events.empty:
                demand_events['day'] = demand_events['day'].astype(int)
                # Extract total demand quantity (original_demand or demand_qty)
                def get_demand_qty(row):
                    if row['event_type'] == 'order_received_for_production': return row['details'].get('original_demand',0)
                    if row['event_type'] == 'product_shipped_from_stock': return row['details'].get('demand_qty',0)
                    return 0
                demand_events['total_demand_qty'] = demand_events.apply(get_demand_qty, axis=1)
                demand_per_day = demand_events.groupby('day')['total_demand_qty'].sum().reset_index()
                
                # Extract fulfilled from stock quantity
                demand_events['fulfilled_stock_qty'] = demand_events['details'].apply(lambda x: x.get('qty_shipped',0) if x.get('source','').startswith('direct') else x.get('fulfilled_stock',0) if x.get('event_type')=='order_received_for_production' else 0) # Check multiple keys
                fulfilled_stock_per_day = demand_events.groupby('day')['fulfilled_stock_qty'].sum().reset_index()

                if not demand_per_day.empty:
                    fig_demand = px.bar(demand_per_day, x='day', y='total_demand_qty', title='Total Product Units Demanded Per Day')
                    st.plotly_chart(fig_demand, use_container_width=True)
                if not fulfilled_stock_per_day.empty and fulfilled_stock_per_day['fulfilled_stock_qty'].sum() > 0: # Only show if there's data
                    fig_fulfilled = px.bar(fulfilled_stock_per_day, x='day', y='fulfilled_stock_qty', title='Product Units Fulfilled From Stock Per Day')
                    fig_fulfilled.update_traces(marker_color='green')
                    st.plotly_chart(fig_fulfilled, use_container_width=True)
        else: st.info("No simulation events recorded.")


elif page == "Setup & Data":
    # ... (Code from previous `app.py` for Setup & Data)
    st.header("⚙️ Setup & Data Management")
    st.subheader("Initial Conditions")
    st.info("Define the starting state of your factory simulation here. This will reset any current simulation.")
    default_initial_conditions = {
        "materials": [
            {"id": "mat-001", "name": "Plastic Filament Spool", "description": "Standard PLA 1kg"},
            {"id": "mat-002", "name": "Frame Component A"}, {"id": "mat-003", "name": "Frame Component B"},
            {"id": "mat-004", "name": "Electronics Board v1"}, {"id": "mat-005", "name": "Power Supply Unit"},
            {"id": "mat-006", "name": "Fasteners Pack (100pcs)"}
        ], "products": [
            {"id": "prod-001", "name": "Basic 3D Printer", "bom": [
                {"material_id": "mat-001", "quantity": 1}, {"material_id": "mat-002", "quantity": 2},
                {"material_id": "mat-003", "quantity": 2}, {"material_id": "mat-004", "quantity": 1},
                {"material_id": "mat-005", "quantity": 1}, {"material_id": "mat-006", "quantity": 1}
            ], "production_time": 3 },
             {"id": "prod-002", "name": "Advanced 3D Printer", "bom": [
                {"material_id": "mat-001", "quantity": 2}, {"material_id": "mat-002", "quantity": 4},
                {"material_id": "mat-003", "quantity": 4}, {"material_id": "mat-004", "quantity": 2},
                {"material_id": "mat-005", "quantity": 1}, {"material_id": "mat-006", "quantity": 2}
            ], "production_time": 5 }
        ], "providers": [
            {"id": "prov-001", "name": "Filament Inc.", "catalogue": [{"material_id": "mat-001", "price_per_unit": 20.0, "offered_unit_size": 1, "lead_time_days": 2}]},
            {"id": "prov-002", "name": "Frame Parts Co.", "catalogue": [
                {"material_id": "mat-002", "price_per_unit": 5.0, "offered_unit_size": 1, "lead_time_days": 5},
                {"material_id": "mat-003", "price_per_unit": 6.0, "offered_unit_size": 1, "lead_time_days": 5}]},
            {"id": "prov-003", "name": "Electronics Hub", "catalogue": [
                {"material_id": "mat-004", "price_per_unit": 50.0, "offered_unit_size": 1, "lead_time_days": 7},
                {"material_id": "mat-005", "price_per_unit": 30.0, "offered_unit_size": 1, "lead_time_days": 4}]},
            {"id": "prov-004", "name": "Hardware Supplies Ltd.", "catalogue": [{"material_id": "mat-006", "price_per_unit": 10.0, "offered_unit_size": 1, "lead_time_days": 3}]}
        ], "initial_inventory": { 
            "mat-001": 50, "mat-002": 100, "mat-003": 100, "mat-004": 20, "mat-005": 30, "mat-006": 50, "prod-001": 5
        }, "storage_capacity": 5000, "daily_production_capacity": 5,
        "random_order_config": {"min_orders_per_day": 0, "max_orders_per_day": 2, "min_qty_per_order": 1, "max_qty_per_order": 3}
    }
    edited_conditions_str = st.text_area(
        "Initial Conditions JSON", value=json.dumps(default_initial_conditions, indent=2), height=400, key="initial_cond_json"
    )
    if st.button("Initialize Simulation with Above Data", type="primary"):
        try:
            conditions_data = json.loads(edited_conditions_str)
            if initialize_simulation(conditions_data):
                 load_base_data.clear(); load_inventory_data_cached.clear()
                 st.rerun()
        except json.JSONDecodeError: st.error("Invalid JSON format in Initial Conditions.")
        except Exception as e: st.error(f"Error initializing simulation: {e}")
    st.divider()
    st.subheader("Data Export / Import")
    col_exp, col_imp = st.columns(2)
    with col_exp:
        st.write("Export the current simulation state, events, and definitions to a JSON file.")
        if st.session_state.simulation_status: 
            if st.button("Prepare Export Data"):
                exported_data_content = export_data()
                if exported_data_content:
                    current_day_val = st.session_state.simulation_status.get('current_day', 0)
                    st.download_button(label="Download Exported Data (JSON)", data=json.dumps(exported_data_content, indent=2),
                                       file_name=f"mrp_simulation_export_day_{current_day_val}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json")
        else: st.info("Initialize simulation to enable data export.")
    with col_imp:
        st.write("Import a previously exported JSON file. This will **overwrite** the current simulation.")
        uploaded_file = st.file_uploader("Choose a JSON file to import", type="json")
        if uploaded_file is not None:
            try:
                import_file_content = uploaded_file.getvalue().decode("utf-8")
                import_json_data = json.loads(import_file_content)
                if "simulation_state" in import_json_data and "products" in import_json_data and "materials" in import_json_data:
                     if st.button("Confirm Import Data", type="danger"):
                         if import_data(import_json_data): 
                             load_base_data.clear(); load_inventory_data_cached.clear() # Rerun handled by import_data
                else: st.error("Uploaded file does not appear to be a valid simulation export (missing key fields).")
            except json.JSONDecodeError: st.error("Invalid JSON file.")
            except Exception as e: st.error(f"Error processing import file: {e}")