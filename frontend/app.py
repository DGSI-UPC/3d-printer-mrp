import streamlit as st
import pandas as pd
import plotly.express as px
import json
from datetime import datetime

from api_client import (
    get_simulation_status, initialize_simulation, advance_day,
    get_materials, get_products, get_providers, get_inventory,
    get_production_orders, start_production, accept_production_order,
    fulfill_accepted_production_order_from_stock,
    order_missing_materials_for_production_order,
    get_purchase_orders, create_purchase_order,
    get_events, export_data, import_data, get_item_forecast
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

@st.cache_data(ttl=10)
def load_item_forecast_cached(item_id: str, days: int, historical_lookback_days: int = 0):
    return get_item_forecast(item_id, days, historical_lookback_days)

@st.cache_data(ttl=10) # Cache for 10 seconds
def load_pending_purchase_orders_cached():
    return get_purchase_orders(status="Ordered")

def format_bom(bom_list, materials_dict_local, header=""):
    if not bom_list: return f"{header}No BOM defined" if header else "No BOM defined"
    lines = [header] if header else []
    for item in bom_list:
        mat_id = item.get('material_id')
        qty = item.get('quantity', 'N/A')
        mat_name = materials_dict_local.get(mat_id, {}).get('name', mat_id)
        lines.append(f"- {mat_name}: {qty}")
    return "\n".join(lines)

def format_material_list_with_stock_check(
    materials_needed_dict,
    physical_stock_levels,
    committed_stock_levels,
    global_on_order_info,     # Dict[str, int] - original total on order (for display)
    allocatable_on_order_qty, # Mutable Dict[str, int] - remaining for allocation
    materials_dict_local
):
    if not materials_needed_dict:
        return "N/A (No materials specified)", False # shortage_for_this_order

    lines = []
    overall_shortage_for_this_production_order = False # Tracks if any line has a true shortage

    for mat_id, qty_needed in materials_needed_dict.items():
        mat_name = materials_dict_local.get(mat_id, {}).get('name', mat_id)
        physical_qty = physical_stock_levels.get(mat_id, 0)
        committed_qty = committed_stock_levels.get(mat_id, 0)
        
        uncommitted_available = physical_qty - committed_qty
        line_shortage_exists = False # Shortage for this specific material line

        display_text_parts = [f"- {mat_name}: Need {qty_needed}, Physical {physical_qty} (Committed: {committed_qty})"]
        color = "green" # Default for sufficient physical stock

        if uncommitted_available < qty_needed:
            physical_shortfall = qty_needed - uncommitted_available
            color = "red" # Default to red if physical shortage
            line_shortage_exists = True

            # Check against allocatable on-order quantity
            current_allocatable_for_mat = allocatable_on_order_qty.get(mat_id, 0)
            global_total_on_order_for_mat = global_on_order_info.get(mat_id, 0)

            if current_allocatable_for_mat > 0:
                allocated_from_po = 0
                if current_allocatable_for_mat >= physical_shortfall:
                    allocated_from_po = physical_shortfall
                    allocatable_on_order_qty[mat_id] = current_allocatable_for_mat - physical_shortfall
                    color = "orange" # Fully covered by PO allocation
                    display_text_parts.append(f"<span style='font-style:italic;'>(Shortfall of {physical_shortfall} covered by PO. Total on order: {global_total_on_order_for_mat})</span>")
                    line_shortage_exists = False # Not a shortage for the button if PO covers it
                else: # current_allocatable_for_mat < physical_shortfall
                    allocated_from_po = current_allocatable_for_mat
                    allocatable_on_order_qty[mat_id] = 0
                    color = "#FF8C00" # Darker Orange for partially covered, still a shortage
                    display_text_parts.append(f"<span style='font-style:italic;'>(Shortfall of {physical_shortfall}, PO covers {allocated_from_po}. Total on order: {global_total_on_order_for_mat})</span>")
                    line_shortage_exists = True # Still a shortage as PO doesn't fully cover
            elif global_total_on_order_for_mat > 0 : # Nothing allocatable NOW, but some were on order globally
                 display_text_parts.append(f"<span style='font-style:italic;'>(No PO stock allocatable here. Total on order globally: {global_total_on_order_for_mat})</span>")
                 # color remains red, line_shortage_exists remains true

        lines.append(f"<span style='color:{color};'>{' '.join(display_text_parts)}</span>")
        if line_shortage_exists:
            overall_shortage_for_this_production_order = True
            
    return "<br>".join(lines), overall_shortage_for_this_production_order


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

physical_stock_snapshot = {
    item_id: details.get('physical', 0)
    for item_id, details in inventory_items_detailed.items()
}
committed_stock_snapshot = {
    item_id: details.get('committed', 0)
    for item_id, details in inventory_items_detailed.items()
}

# Load and process pending purchase orders for material highlighting
# This is the GLOBAL on-order quantity, used for display and initial allocation pool.
pending_pos_data_global = load_pending_purchase_orders_cached()
global_on_order_materials_info = {}
if pending_pos_data_global:
    for po in pending_pos_data_global:
        mat_id = po.get('material_id')
        qty_ordered = po.get('quantity_ordered', 0)
        if mat_id and qty_ordered > 0:
            global_on_order_materials_info[mat_id] = global_on_order_materials_info.get(mat_id, 0) + qty_ordered


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
            load_inventory_data_cached.clear()
            load_pending_purchase_orders_cached.clear()
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
            - **Accept Request**: Attempts to fulfill from existing finished product stock. If not fully available, it checks if required materials (uncommitted) are in stock. If both finished products and materials are insufficient, acceptance will fail. Otherwise, the order (or remaining part) is accepted and materials are committed.
            - **Order Missing Materials**: Check physical stock for required materials and place Purchase Orders for any shortages *for this specific request*.
            Materials with shortfalls covered by incoming Purchase Orders will be shown in <span style='color:orange;'>orange</span>.
            Partially covered or still short materials will be in <span style='color:#FF8C00;'>darker orange</span> or <span style='color:red;'>red</span>.
            """)
            pending_orders_data = get_production_orders(status="Pending")
            
            # Sort pending orders by creation date (ascending) for fair allocation
            if pending_orders_data:
                pending_orders_data.sort(key=lambda x: pd.to_datetime(x.get('created_at', x.get('requested_date'))))

                # This dictionary will be modified by format_material_list_with_stock_check
                allocatable_on_order_qty_for_run = global_on_order_materials_info.copy()

                for order in pending_orders_data:
                    product_name = products_dict.get(order['product_id'], {}).get('name', order['product_id'])
                    st.markdown(f"#### Order ID: `{order['id']}`")

                    col_details, col_actions = st.columns([3,1])
                    with col_details:
                        st.markdown(f"**Product:** {product_name} | **Qty:** {order['quantity']} | **Created:** {pd.to_datetime(order.get('created_at', order.get('requested_date'))).strftime('%Y-%m-%d %H:%M')}")

                        finished_prod_stock = physical_stock_snapshot.get(order['product_id'], 0)
                        if finished_prod_stock > 0:
                            st.info(f"ℹ️ **Note:** {finished_prod_stock} units of '{product_name}' are currently in physical stock.")

                        if order.get('required_materials'):
                            materials_display_html, shortage_exists_for_order_button = format_material_list_with_stock_check(
                                order['required_materials'],
                                physical_stock_snapshot,
                                committed_stock_snapshot,
                                global_on_order_materials_info, # Pass for display of total
                                allocatable_on_order_qty_for_run, # Pass for mutable allocation
                                materials_dict
                            )
                            st.markdown("**Material Availability (Need vs. Physical Stock - Committed to Others):**")
                            st.markdown(materials_display_html, unsafe_allow_html=True)
                        else:
                            shortage_exists_for_order_button = False
                            st.warning("No required materials listed for this pending order (BOM might be missing or quantity is zero).")

                    with col_actions:
                        if st.button("✅ Accept Request", key=f"accept_{order['id']}", use_container_width=True):
                            if accept_production_order(order['id']):
                                load_inventory_data_cached.clear()
                                load_pending_purchase_orders_cached.clear()
                                st.rerun()

                        if order.get('required_materials') and shortage_exists_for_order_button :
                             if st.button("🛒 Order Missing Materials", key=f"order_missing_{order['id']}", use_container_width=True):
                                if order_missing_materials_for_production_order(order['id']):
                                    load_inventory_data_cached.clear()
                                    load_pending_purchase_orders_cached.clear()
                                    st.rerun()
                    st.markdown("---")
            else: st.info("No pending production requests.")

        with accepted_tab:
            st.subheader("Accepted Orders")
            st.markdown("""
            These orders have been accepted, and necessary materials have been committed from stock.
            - **Finished Product Stock:** Check current physical stock of the *finished product*.
            - **Fulfill from Stock**: If enough finished product is now available, fulfill the order directly. This will un-commit the previously allocated materials.
            - **Send to Production**: Moves the order to 'In Progress' using the already committed materials.
            """)
            accepted_orders_data = get_production_orders(status="Accepted")

            if accepted_orders_data:
                for i, order in enumerate(accepted_orders_data):
                    order_id = order['id']
                    product_id = order['product_id']
                    product_name = products_dict.get(product_id, {}).get('name', product_id)
                    qty_needed = order['quantity']
                    requested_date_str = pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d')

                    st.markdown(f"#### Order ID: `{order_id}`")
                    col1, col2 = st.columns([3, 1])

                    with col1:
                        st.write(f"**Product:** {product_name}")
                        st.write(f"**Quantity Needed:** {qty_needed}")
                        st.write(f"**Requested Date:** {requested_date_str}")

                        if order.get('committed_materials'):
                            st.markdown("**Materials Committed for this Order:**")
                            committed_display = format_bom(
                                [{'material_id': mid, 'quantity': q} for mid, q in order['committed_materials'].items()],
                                materials_dict
                            )
                            st.markdown(committed_display)
                        else:
                            st.warning("No materials appear to be committed for this accepted order. This might indicate an issue.")

                        finished_product_stock = physical_stock_snapshot.get(product_id, 0)
                        color = "green" if finished_product_stock >= qty_needed else "red"
                        st.markdown(f"**Finished Product Stock for '{product_name}':** <span style='color:{color};'>{finished_product_stock} available</span>", unsafe_allow_html=True)

                    with col2:
                        can_fulfill_now = finished_product_stock >= qty_needed
                        if st.button("✅ Fulfill from Stock", key=f"fulfill_accepted_{order_id}", use_container_width=True, disabled=not can_fulfill_now):
                            if fulfill_accepted_production_order_from_stock(order_id):
                                load_inventory_data_cached.clear()
                                load_pending_purchase_orders_cached.clear()
                                st.rerun()

                        if st.button("➡️ Send to Production", key=f"start_single_accepted_{order_id}", use_container_width=True):
                            if start_production([order_id]):
                                load_inventory_data_cached.clear()
                                load_pending_purchase_orders_cached.clear()
                                st.rerun()
                    st.markdown("---")
            else:
                st.info("No orders currently in 'Accepted' state.")


        with in_progress_tab:
            st.subheader("In Progress Orders")
            st.markdown("These orders are currently being manufactured. Materials have been consumed from committed stock.")
            in_progress_orders = get_production_orders(status="In Progress")
            if in_progress_orders:
                 orders_df_prog = pd.DataFrame(in_progress_orders)
                 orders_df_prog['Product'] = orders_df_prog['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df_prog['Started At'] = orders_df_prog['started_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_prog['Committed Materials (at start)'] = orders_df_prog['committed_materials'].apply(lambda x: format_bom([{'material_id': k, 'quantity': v} for k,v in x.items()], materials_dict) if x else "N/A")
                 st.dataframe(orders_df_prog[['id', 'Product', 'quantity', 'Started At', 'Committed Materials (at start)']].rename(columns={'id':'Order ID', 'quantity':'Qty'}),
                              use_container_width=True, hide_index=True)
            else: st.info("No production orders currently in progress.")


        with completed_tab:
             st.subheader("Completed Production Orders (Manufactured)")
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
            st.markdown("These orders (or parts of original demand) were fulfilled using existing finished product stock.")
            fulfilled_orders_data = get_production_orders(status="Fulfilled")
            if fulfilled_orders_data:
                orders_df_ful = pd.DataFrame(fulfilled_orders_data)
                orders_df_ful['Product'] = orders_df_ful['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                orders_df_ful['Fulfilled At'] = orders_df_ful['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                st.dataframe(orders_df_ful[['id', 'Product', 'quantity', 'Fulfilled At']].rename(columns={'id':'Order ID', 'quantity':'Qty Fulfilled'}),
                              use_container_width=True, hide_index=True)
            else:
                st.info("No orders have been marked as 'Fulfilled' from stock based on production order status. Check Event Log for direct demand fulfillment.")


elif page == "Purchasing":
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
                    qty_val = st.number_input("Quantity (units)", 1, 10000, 1)
                    submit_dis = not sel_prov_id
                if st.form_submit_button("Place Purchase Order", disabled=submit_dis) and sel_mat_id and sel_prov_id and qty_val > 0:
                    if create_purchase_order(sel_mat_id, sel_prov_id, qty_val):
                        load_inventory_data_cached.clear()
                        load_pending_purchase_orders_cached.clear() # Also clear PO cache
                        st.rerun()
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
        # Use global_on_order_materials_info which is already from pending_pos_data_global
        if pending_pos_data_global: # Re-use the fetched data
            pos_df = pd.DataFrame([{"PO ID": po['id'], "Material": materials_dict.get(po['material_id'],{}).get('name', po['material_id']),
                                   "Qty": po['quantity_ordered'], "Provider": providers_dict.get(po['provider_id'],{}).get('name', po['provider_id']),
                                   "Ordered": pd.to_datetime(po['order_date']).strftime('%Y-%m-%d %H:%M'),
                                   "ETA": pd.to_datetime(po['expected_arrival_date']).strftime('%Y-%m-%d')} for po in pending_pos_data_global])
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
                 if chart_sel == "On Order": fig_data = fig_data[fig_data["Type"] == "Material"]
                 fig_data = fig_data[fig_data[chart_sel] != 0]

                 if not fig_data.empty:
                    fig = px.bar(fig_data.sort_values(chart_sel, ascending=False).head(20),
                                 x="Name", y=chart_sel, color="Type", title=f"{chart_sel} Levels (Top 20)", labels={'Name':'Item'})
                    st.plotly_chart(fig, use_container_width=True)
                 else: st.info(f"No items with non-zero {chart_sel} data to display.")
            else: st.info("Inventory is currently empty.")
        else: st.info("Could not retrieve inventory data or inventory is empty.")

        st.divider()
        st.subheader("📈 Item Stock Forecast")
        
        all_items_for_select = []
        if materials_list_data:
            all_items_for_select.extend([
                {"id": m['id'], "name": f"{m['name']} (Material)", "type": "Material"} for m in materials_list_data
            ])
        if products_list_data:
            all_items_for_select.extend([
                {"id": p['id'], "name": f"{p['name']} (Product)", "type": "Product"} for p in products_list_data
            ])
        
        if not all_items_for_select:
            st.info("No materials or products defined to generate a forecast.")
        else:
            sorted_items_for_select = sorted(all_items_for_select, key=lambda x: x['name'])
            
            col_item_select, col_days_select = st.columns(2)
            selected_item_id = col_item_select.selectbox(
                "Select Item for Forecast:",
                options=[item['id'] for item in sorted_items_for_select],
                format_func=lambda item_id: next((item['name'] for item in sorted_items_for_select if item['id'] == item_id), "Unknown Item"),
                index=0 if sorted_items_for_select else None,
                key="forecast_item_select"
            )
            
            forecast_days_options = [7, 14, 30]
            selected_forecast_days = col_days_select.selectbox(
                "Select Forecast Horizon (days):",
                options=forecast_days_options,
                index=0,
                key="forecast_days_select"
            )

            if selected_item_id and selected_forecast_days:
                if selected_forecast_days == 7: historical_days_to_show = 3
                elif selected_forecast_days == 14: historical_days_to_show = 5
                elif selected_forecast_days == 30: historical_days_to_show = 10
                else: historical_days_to_show = 3

                forecast_data_response = load_item_forecast_cached(selected_item_id, selected_forecast_days, historical_days_to_show)
                
                if forecast_data_response and 'forecast' in forecast_data_response and forecast_data_response['forecast']:
                    forecast_df = pd.DataFrame(forecast_data_response['forecast'])
                    forecast_df['date'] = pd.to_datetime(forecast_df['date'])
                    forecast_df = forecast_df.sort_values(by='date')

                    item_display_name = forecast_data_response.get('item_name', selected_item_id)
                    current_day_data = forecast_df[forecast_df['day_offset'] == 0]
                    current_date_vline = current_day_data['date'].iloc[0] if not current_day_data.empty else None
                    fig_forecast = px.line(title=f"Projected Stock for '{item_display_name}'")
                    past_and_current_df = forecast_df[forecast_df['day_offset'] <= 0]
                    current_and_future_df = forecast_df[forecast_df['day_offset'] >= 0]

                    if not past_and_current_df.empty:
                        fig_forecast.add_trace(
                            px.line(past_and_current_df, x='date', y='quantity').data[0].update(
                                line=dict(color='royalblue', dash='dash'), name='Historical Context / Current'
                            )
                        )
                    
                    if not current_and_future_df.empty:
                        fig_forecast.add_trace(
                            px.line(current_and_future_df, x='date', y='quantity').data[0].update(
                                line=dict(color='darkorange'), name='Forecast'
                            )
                        )
                    
                    if current_date_vline:
                        fig_forecast.add_vline(x=current_date_vline, line_width=2, line_dash="solid", line_color="green")
                        fig_forecast.add_annotation(
                            x=current_date_vline, y=1.03, yref="paper", text="Current Day",
                            showarrow=False, font=dict(color="green", size=12),
                            xanchor="center", yanchor="bottom"
                        )
                    
                    fig_forecast.update_layout(xaxis_title='Date', yaxis_title='Projected Quantity', legend_title_text='Legend')
                    fig_forecast.update_traces(mode='lines+markers')
                    st.plotly_chart(fig_forecast, use_container_width=True)
                elif forecast_data_response is None and st.session_state.simulation_status:
                    st.warning(f"Could not retrieve forecast data for item ID '{selected_item_id}'. The item might not exist or an error occurred.")
                elif not st.session_state.simulation_status:
                     st.info("Simulation not initialized. Forecast unavailable.")
                else:
                    st.info(f"No forecast data available for '{selected_item_id}' for the selected period.")


elif page == "History":
    st.header("📜 Simulation Event Log")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        event_limit = st.slider("Number of recent events", 50, 500, 100, 50)
        events = get_events(limit=event_limit)
        if events:
            df = pd.DataFrame(events); df['timestamp'] = pd.to_datetime(df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            df['details_short'] = df['details'].apply(lambda x: (json.dumps(x)[:100] + '...') if isinstance(x, dict) and len(json.dumps(x)) > 100 else json.dumps(x) if isinstance(x,dict) else str(x)[:100])
            st.dataframe(df[['day','timestamp','event_type','details_short']], height=500, hide_index=True, use_container_width=True,
                         column_config={"details_short": st.column_config.TextColumn("Details Preview")})
            with st.expander("View Full Event Details"):
                sel_ev_id = st.selectbox("Event ID:", options=df['id'].tolist(), index=None)
                if sel_ev_id: st.json(df[df['id'] == sel_ev_id]['details'].iloc[0])

            demand_events = df[df['event_type'].isin(['order_received_for_production', 'product_shipped_from_stock', 'production_order_fulfilled_from_stock', 'accepted_order_fulfilled_from_stock'])].copy()
            if not demand_events.empty:
                demand_events['day'] = demand_events['day'].astype(int)
                def get_demand_qty(row):
                    if row['event_type'] == 'order_received_for_production': return row['details'].get('original_demand', row['details'].get('qty_for_prod',0))
                    if row['event_type'] == 'product_shipped_from_stock': return row['details'].get('demand_qty', row['details'].get('qty_shipped',0))
                    return 0
                demand_events['total_demand_qty'] = demand_events.apply(get_demand_qty, axis=1)
                demand_per_day = demand_events[demand_events['total_demand_qty'] > 0].groupby('day')['total_demand_qty'].sum().reset_index()

                def get_fulfilled_qty(row):
                    if row['event_type'] == 'product_shipped_from_stock': return row['details'].get('qty_shipped', 0)
                    if row['event_type'] == 'production_order_fulfilled_from_stock': return row['details'].get('quantity_fulfilled', 0)
                    if row['event_type'] == 'accepted_order_fulfilled_from_stock': return row['details'].get('quantity_fulfilled', 0)
                    if row['event_type'] == 'order_received_for_production': return row['details'].get('fulfilled_stock',0)
                    return 0
                demand_events['fulfilled_stock_qty'] = demand_events.apply(get_fulfilled_qty, axis=1)
                fulfilled_stock_per_day = demand_events[demand_events['fulfilled_stock_qty'] > 0].groupby('day')['fulfilled_stock_qty'].sum().reset_index()

                if not demand_per_day.empty:
                    fig_demand = px.bar(demand_per_day, x='day', y='total_demand_qty', title='Total Product Units Demanded Per Day (New Orders)')
                    st.plotly_chart(fig_demand, use_container_width=True)
                if not fulfilled_stock_per_day.empty and fulfilled_stock_per_day['fulfilled_stock_qty'].sum() > 0:
                    fig_fulfilled = px.bar(fulfilled_stock_per_day, x='day', y='fulfilled_stock_qty', title='Product Units Fulfilled From Stock Per Day (All Sources)')
                    fig_fulfilled.update_traces(marker_color='green')
                    st.plotly_chart(fig_fulfilled, use_container_width=True)
        else: st.info("No simulation events recorded.")


elif page == "Setup & Data":
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
            "mat-001": 50, "mat-002": 100, "mat-003": 100, "mat-004": 20, "mat-005": 30, "mat-006": 50, "prod-001": 5, "prod-002": 0
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
                 load_base_data.clear()
                 load_inventory_data_cached.clear()
                 load_pending_purchase_orders_cached.clear()
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
                             load_base_data.clear()
                             load_inventory_data_cached.clear()
                             load_pending_purchase_orders_cached.clear()
                             # st.rerun() is handled by import_data in api_client if successful
                else: st.error("Uploaded file does not appear to be a valid simulation export (missing key fields).")
            except json.JSONDecodeError: st.error("Invalid JSON file.")
            except Exception as e: st.error(f"Error processing import file: {e}")