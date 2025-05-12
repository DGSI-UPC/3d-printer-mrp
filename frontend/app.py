import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go # For more complex charts like combined bar/line
import json
from datetime import datetime

from api_client import (
    get_simulation_status, initialize_simulation, advance_day,
    get_materials, get_products, get_providers, get_inventory,
    get_production_orders, start_production, accept_production_order,
    fulfill_accepted_production_order_from_stock,
    order_missing_materials_for_production_order,
    get_purchase_orders, create_purchase_order,
    get_events, export_data, import_data, get_item_forecast,
    get_financial_data # New import
)

st.set_page_config(
    page_title="MRP Factory Simulation",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Session State and Query Parameter Handling for Navigation ---
if 'current_page' not in st.session_state:
    st.session_state.current_page = "Dashboard"

page_to_set_from_query = st.query_params.get("page", None)
if page_to_set_from_query:
    # Added "Finances" to valid pages
    valid_pages = ["Dashboard", "Finances", "Production", "Purchasing", "Inventory", "History", "Setup & Data"]
    if page_to_set_from_query in valid_pages:
        st.session_state.current_page = page_to_set_from_query
    
    if "page" in st.query_params:
        del st.query_params["page"]

if 'simulation_status' not in st.session_state:
    st.session_state.simulation_status = None
# --- End Navigation Handling ---

@st.cache_data(ttl=60) # Cache for 1 minute
def load_base_data_cached(): # Renamed for clarity
    materials = get_materials()
    products = get_products()
    providers = get_providers()
    return materials, products, providers

@st.cache_data(ttl=10) # Cache for 10 seconds
def load_inventory_data_cached():
    return get_inventory()

@st.cache_data(ttl=10)
def load_item_forecast_cached(item_id: str, days: int, historical_lookback_days: int = 0):
    return get_item_forecast(item_id, days, historical_lookback_days)

@st.cache_data(ttl=10)
def load_pending_purchase_orders_cached():
    return get_purchase_orders(status="Ordered")

@st.cache_data(ttl=10) # Cache for financial data
def load_financial_data_cached(forecast_days: int = 7):
    return get_financial_data(forecast_days)


def format_bom(bom_list, materials_dict_local, header=""):
    # (Existing function - no changes)
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
    global_on_order_info,
    allocatable_on_order_qty, # This will be modified by the function
    materials_dict_local
):
    # (Existing function - no changes)
    if not materials_needed_dict:
        return "N/A (No materials specified)", False
    lines = []
    overall_shortage_for_this_production_order = False
    for mat_id, qty_needed in materials_needed_dict.items():
        mat_name = materials_dict_local.get(mat_id, {}).get('name', mat_id)
        physical_qty = physical_stock_levels.get(mat_id, 0)
        committed_qty = committed_stock_levels.get(mat_id, 0)
        uncommitted_available = physical_qty - committed_qty
        line_shortage_exists = False
        display_text_parts = [f"- {mat_name}: Need {qty_needed}, Physical {physical_qty} (Committed: {committed_qty})"]
        color = "green"
        if uncommitted_available < qty_needed:
            physical_shortfall = qty_needed - uncommitted_available
            color = "red"; line_shortage_exists = True
            current_allocatable_for_mat = allocatable_on_order_qty.get(mat_id, 0)
            global_total_on_order_for_mat = global_on_order_info.get(mat_id, 0)
            if current_allocatable_for_mat > 0:
                allocated_from_po = 0
                if current_allocatable_for_mat >= physical_shortfall:
                    allocated_from_po = physical_shortfall
                    allocatable_on_order_qty[mat_id] = current_allocatable_for_mat - physical_shortfall
                    color = "orange"
                    display_text_parts.append(f"<span style='font-style:italic;'>(Shortfall of {physical_shortfall} covered by PO. Total on order: {global_total_on_order_for_mat})</span>")
                    line_shortage_exists = False
                else:
                    allocated_from_po = current_allocatable_for_mat
                    allocatable_on_order_qty[mat_id] = 0
                    color = "#FF8C00"
                    display_text_parts.append(f"<span style='font-style:italic;'>(Shortfall of {physical_shortfall}, PO covers {allocated_from_po}. Total on order: {global_total_on_order_for_mat})</span>")
                    line_shortage_exists = True
            elif global_total_on_order_for_mat > 0 :
                 display_text_parts.append(f"<span style='font-style:italic;'>(No PO stock allocatable here. Total on order globally: {global_total_on_order_for_mat})</span>")
        lines.append(f"<span style='color:{color};'>{' '.join(display_text_parts)}</span>")
        if line_shortage_exists: overall_shortage_for_this_production_order = True
    return "<br>".join(lines), overall_shortage_for_this_production_order


def format_catalogue(catalogue_list, materials_dict_local):
    # (Existing function - no changes)
    if not catalogue_list: return "No offerings defined"
    lines = []
    for item in catalogue_list:
        mat_name = materials_dict_local.get(item['material_id'], {}).get('name', item['material_id'])
        lines.append(f"- {mat_name}: €{item['price_per_unit']:.2f}/unit (Lead: {item['lead_time_days']} days)")
    return "\n".join(lines)

# Load base data once
materials_list_data, products_list_data, providers_list_data = load_base_data_cached()
materials_dict = {m['id']: m for m in materials_list_data if m} if materials_list_data else {}
products_dict = {p['id']: p for p in products_list_data if p} if products_list_data else {}
providers_dict = {p['id']: p for p in providers_list_data if p} if providers_list_data else {}

# Load dynamic data that changes often
current_inventory_status_response = load_inventory_data_cached()
inventory_items_detailed = current_inventory_status_response.get('items', {}) if current_inventory_status_response else {}
physical_stock_snapshot = {item_id: details.get('physical', 0) for item_id, details in inventory_items_detailed.items()}
committed_stock_snapshot = {item_id: details.get('committed', 0) for item_id, details in inventory_items_detailed.items()}
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
st.session_state.simulation_status = get_simulation_status() # Refresh status

if st.session_state.simulation_status:
    status = st.session_state.simulation_status
    st.sidebar.metric("🏦 Current Balance", f"{status.get('current_balance', 0.0):,.2f} EUR") # Added Balance
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
            # Clear all relevant caches after advancing day
            load_inventory_data_cached.clear()
            load_pending_purchase_orders_cached.clear()
            load_base_data_cached.clear() # Base data might not change, but good practice if sim could alter it
            load_financial_data_cached.clear() # Clear financial cache
            load_item_forecast_cached.clear() # Clear item forecast cache
            st.query_params["page"] = st.session_state.current_page # Stay on current page
            st.rerun()
else:
    st.sidebar.warning("Simulation not running or API unreachable. Initialize first via 'Setup & Data'.")

st.sidebar.divider()
st.sidebar.header("Navigation")
# Added "Finances" to page options
page_options = ["Dashboard", "Finances", "Production", "Purchasing", "Inventory", "History", "Setup & Data"]
page = st.sidebar.radio("Go to",
                        page_options,
                        key="current_page",
                        label_visibility="collapsed")
st.sidebar.divider()
st.sidebar.info("Manage your 3D printer factory day by day, keeping an eye on your finances!")

# --- Main Page Content ---
if page == "Dashboard":
    st.header("🏭 Dashboard Overview")
    if st.session_state.simulation_status:
        status = st.session_state.simulation_status
        # Added Balance to dashboard metrics
        col_bal, col_day, col_pend, col_acc, col_po = st.columns(5)
        col_bal.metric("🏦 Current Balance", f"{status.get('current_balance', 0.0):,.2f} EUR")
        col_day.metric("Current Day", status.get('current_day', 'N/A'))
        col_pend.metric("Pending Production", status.get('pending_production_orders', 'N/A'))
        col_acc.metric("Accepted Production", status.get('accepted_production_orders', 'N/A'))
        col_po.metric("Pending POs", status.get('pending_purchase_orders', 'N/A'))

        st.subheader("Recent Events (Last 10)")
        events = get_events(limit=10)
        if events:
            events_df = pd.DataFrame(events)[['day', 'timestamp', 'event_type', 'details']]
            events_df['timestamp'] = pd.to_datetime(events_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            # Attempt to make details more readable by converting dict to string nicely for the column
            events_df['details_str'] = events_df['details'].apply(lambda x: json.dumps(x, indent=2) if isinstance(x, dict) else str(x))
            st.dataframe(events_df[['day', 'timestamp', 'event_type', 'details_str']].rename(columns={'details_str':'Details'}),
                         use_container_width=True, height=300,
                         column_config={"Details": st.column_config.TextColumn("Details", width="large")})
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


elif page == "Finances":
    st.header("💰 Finances Overview")
    if not st.session_state.simulation_status:
        st.warning("Simulation not initialized. Financial data unavailable. Go to 'Setup & Data'.")
    else:
        # Replace slider with selectbox for forecast horizon
        forecast_horizon_options = [7, 14, 30]
        forecast_horizon = st.selectbox(
            "Select forecast horizon (days for charts):",
            options=forecast_horizon_options,
            index=0,  # Default to 7 days
            key="fin_forecast_days_select"
        )
        financial_page_data = load_financial_data_cached(forecast_days=forecast_horizon)

        if financial_page_data:
            summary = financial_page_data.get('summary', {})
            history = financial_page_data.get('historical_performance', [])
            forecast = financial_page_data.get('forecast', [])

            st.subheader("Current Financial Summary")
            col_s1, col_s2, col_s3, col_s4 = st.columns(4)
            col_s1.metric("Current Balance", f"{summary.get('current_balance', 0.0):,.2f} EUR")
            col_s2.metric("Total Revenue (to date)", f"{summary.get('total_revenue_to_date', 0.0):,.2f} EUR")
            col_s3.metric("Total Expenses (to date)", f"{summary.get('total_expenses_to_date', 0.0):,.2f} EUR")
            col_s4.metric("Profit (to date)", f"{summary.get('profit_to_date', 0.0):,.2f} EUR",
                          delta_color=("inverse" if summary.get('profit_to_date', 0.0) < 0 else "normal"))

            history_df = pd.DataFrame()
            current_day_vline_date = None
            if history:
                history_df = pd.DataFrame(history)
                if not history_df.empty:
                    history_df['date'] = pd.to_datetime(history_df['date'])
                    history_df = history_df.sort_values(by='date', ascending=True)
                    if not history_df.empty:
                        current_day_vline_date = history_df['date'].iloc[-1]

            forecast_df = pd.DataFrame()
            if forecast:
                forecast_df = pd.DataFrame(forecast)
                if not forecast_df.empty:
                    forecast_df['date'] = pd.to_datetime(forecast_df['date'])
                    forecast_df = forecast_df.sort_values(by='date', ascending=True)

            st.subheader(f"Financial Performance & Projection (Forecast: {forecast_horizon} Days)")

            # Combined Balance Chart
            fig_balance_overview = go.Figure()
            has_balance_data = False

            plot_forecast_balance_dates = pd.Series(dtype='datetime64[ns]')
            plot_forecast_balance_values = pd.Series(dtype='float64')

            if not history_df.empty:
                fig_balance_overview.add_trace(go.Scatter(
                    x=history_df['date'], y=history_df['balance'], name='Historical/Current Balance',
                    mode='lines+markers', line=dict(color='royalblue', dash='dash')
                ))
                has_balance_data = True

                if not forecast_df.empty and 'projected_balance' in forecast_df.columns:
                    last_hist_date = history_df['date'].iloc[-1]
                    last_hist_balance = history_df['balance'].iloc[-1]
                    
                    temp_forecast_df_balance = forecast_df.copy()
                    if not temp_forecast_df_balance.empty:
                        first_forecast_date = temp_forecast_df_balance['date'].iloc[0]
                        # Ensure forecast data for plotting starts from the last historical point to connect lines
                        if first_forecast_date == last_hist_date:
                            # If forecast starts on the same day, ensure its first point matches history's last
                            temp_forecast_df_balance.loc[temp_forecast_df_balance.index[0], 'projected_balance'] = last_hist_balance
                            plot_forecast_balance_dates = temp_forecast_df_balance['date']
                            plot_forecast_balance_values = temp_forecast_df_balance['projected_balance']
                        elif first_forecast_date > last_hist_date:
                             # Prepend last historical point to forecast data for a continuous line
                            connection_point_date = pd.Series([last_hist_date], index=[-1])
                            connection_point_balance = pd.Series([last_hist_balance], index=[-1])
                            plot_forecast_balance_dates = pd.concat([connection_point_date, temp_forecast_df_balance['date']]).reset_index(drop=True)
                            plot_forecast_balance_values = pd.concat([connection_point_balance, temp_forecast_df_balance['projected_balance']]).reset_index(drop=True)
                        else: # Fallback if forecast data is somehow before last history (should not happen with sorted data)
                            plot_forecast_balance_dates = temp_forecast_df_balance['date']
                            plot_forecast_balance_values = temp_forecast_df_balance['projected_balance']

            elif not forecast_df.empty and 'projected_balance' in forecast_df.columns: # Only forecast, no history
                plot_forecast_balance_dates = forecast_df['date']
                plot_forecast_balance_values = forecast_df['projected_balance']

            if not plot_forecast_balance_dates.empty:
                 fig_balance_overview.add_trace(go.Scatter(
                    x=plot_forecast_balance_dates, y=plot_forecast_balance_values, name='Projected Balance',
                    mode='lines+markers', line=dict(color='darkorange')
                ))
                 has_balance_data = True
            
            if has_balance_data:
                fig_balance_overview.update_layout(
                    title_text='Balance Over Time (Historical & Projected)',
                    xaxis_title='Date', yaxis_title='Balance (EUR)',
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
                )
                if current_day_vline_date:
                    fig_balance_overview.add_vline(x=current_day_vline_date, line_width=2, line_dash="solid", line_color="green")
                    fig_balance_overview.add_annotation(
                        x=current_day_vline_date, y=1.03, yref="paper", text="Current Day",
                        showarrow=False, font=dict(color="green", size=12),
                        xanchor="center", yanchor="bottom"
                    )
                st.plotly_chart(fig_balance_overview, use_container_width=True)
            else:
                st.info("No balance data (historical or forecast) to display.")

            # Combined Daily Financial Flows Chart
            fig_flows_overview = go.Figure()
            has_flows_data = False

            plot_forecast_profit_dates = pd.Series(dtype='datetime64[ns]')
            plot_forecast_profit_values = pd.Series(dtype='float64')

            if not history_df.empty and 'profit' in history_df.columns:
                fig_flows_overview.add_trace(go.Bar(x=history_df['date'], y=history_df['revenue'], name='Revenue (Hist.)', marker_color='blue'))
                fig_flows_overview.add_trace(go.Bar(x=history_df['date'], y=history_df['material_costs'], name='Material Costs (Hist.)', marker_color='orange'))
                fig_flows_overview.add_trace(go.Bar(x=history_df['date'], y=history_df['operational_costs'], name='Operational Costs (Hist.)', marker_color='red'))
                fig_flows_overview.add_trace(go.Scatter(x=history_df['date'], y=history_df['profit'], name='Daily Profit (Hist.)', mode='lines+markers', line=dict(color='purple', dash='solid')))
                has_flows_data = True

                if not forecast_df.empty and 'projected_profit' in forecast_df.columns:
                    last_hist_date_profit = history_df['date'].iloc[-1]
                    last_hist_profit = history_df['profit'].iloc[-1]
                    
                    temp_forecast_df_profit = forecast_df.copy()
                    if not temp_forecast_df_profit.empty:
                        first_forecast_date_profit = temp_forecast_df_profit['date'].iloc[0]
                        if first_forecast_date_profit == last_hist_date_profit:
                            temp_forecast_df_profit.loc[temp_forecast_df_profit.index[0], 'projected_profit'] = last_hist_profit
                            plot_forecast_profit_dates = temp_forecast_df_profit['date']
                            plot_forecast_profit_values = temp_forecast_df_profit['projected_profit']
                        elif first_forecast_date_profit > last_hist_date_profit:
                            connection_point_date_profit = pd.Series([last_hist_date_profit], index=[-1])
                            connection_point_profit = pd.Series([last_hist_profit], index=[-1])
                            plot_forecast_profit_dates = pd.concat([connection_point_date_profit, temp_forecast_df_profit['date']]).reset_index(drop=True)
                            plot_forecast_profit_values = pd.concat([connection_point_profit, temp_forecast_df_profit['projected_profit']]).reset_index(drop=True)
                        else:
                            plot_forecast_profit_dates = temp_forecast_df_profit['date']
                            plot_forecast_profit_values = temp_forecast_df_profit['projected_profit']
            
            elif not forecast_df.empty and 'projected_profit' in forecast_df.columns: 
                plot_forecast_profit_dates = forecast_df['date']
                plot_forecast_profit_values = forecast_df['projected_profit']

            if not forecast_df.empty:
                if 'projected_revenue' in forecast_df.columns:
                    fig_flows_overview.add_trace(go.Bar(x=forecast_df['date'], y=forecast_df['projected_revenue'], name='Revenue (Proj.)', marker_color='lightblue'))
                    has_flows_data = True
                if 'projected_material_costs' in forecast_df.columns:
                    fig_flows_overview.add_trace(go.Bar(x=forecast_df['date'], y=forecast_df['projected_material_costs'], name='Material Costs (Proj.)', marker_color='lightsalmon'))
                    has_flows_data = True
                if 'projected_operational_costs' in forecast_df.columns:
                    fig_flows_overview.add_trace(go.Bar(x=forecast_df['date'], y=forecast_df['projected_operational_costs'], name='Operational Costs (Proj.)', marker_color='pink'))
                    has_flows_data = True

            if not plot_forecast_profit_dates.empty:
                fig_flows_overview.add_trace(go.Scatter(
                    x=plot_forecast_profit_dates, y=plot_forecast_profit_values, name='Daily Profit (Proj.)',
                    mode='lines+markers', line=dict(color='indigo', dash='dot')
                ))
                has_flows_data = True
            
            if has_flows_data:
                # Changed barmode to 'group'
                fig_flows_overview.update_layout(barmode='group', title_text='Daily Financial Flows (Historical & Projected)', xaxis_title='Date', yaxis_title='Amount (EUR)', legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                if current_day_vline_date: 
                    fig_flows_overview.add_vline(x=current_day_vline_date, line_width=2, line_dash="solid", line_color="green")
                st.plotly_chart(fig_flows_overview, use_container_width=True)
            else:
                st.info("No daily financial flow data (historical or forecast) to display.")
        else:
            st.error("Could not retrieve financial data. The simulation might not be initialized or an API error occurred.")


elif page == "Production":
    # (Existing Production page logic - no direct changes needed here for finance display,
    # but it benefits from the API client's updated error handling for insufficient funds on material orders)
    st.header("🛠️ Production Management")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        tab_titles = ["Pending Requests", "Accepted Orders", "In Progress", "Completed", "Fulfilled (from Stock)"]
        pending_tab, accepted_tab, in_progress_tab, completed_tab, fulfilled_tab = st.tabs(tab_titles)
        with pending_tab:
            st.subheader("Pending Production Requests")
            # ... (rest of existing pending_tab logic)
            pending_orders_data = get_production_orders(status="Pending")
            if pending_orders_data:
                pending_orders_data.sort(key=lambda x: pd.to_datetime(x.get('created_at', x.get('requested_date'))))
                allocatable_on_order_qty_for_run = global_on_order_materials_info.copy()
                for order in pending_orders_data:
                    product_name = products_dict.get(order['product_id'], {}).get('name', order['product_id'])
                    st.markdown(f"#### Order ID: `{order['id']}`")
                    col_details, col_actions = st.columns([3,1])
                    with col_details:
                        st.markdown(f"**Product:** {product_name} | **Qty:** {order['quantity']} | **Created:** {pd.to_datetime(order.get('created_at', order.get('requested_date'))).strftime('%Y-%m-%d %H:%M')}")
                        finished_prod_stock = physical_stock_snapshot.get(order['product_id'], 0)
                        if finished_prod_stock > 0: st.info(f"ℹ️ **Note:** {finished_prod_stock} units of '{product_name}' are currently in physical stock.")
                        if order.get('required_materials'):
                            materials_display_html, shortage_exists_for_order_button = format_material_list_with_stock_check(
                                order['required_materials'], physical_stock_snapshot, committed_stock_snapshot,
                                global_on_order_materials_info, allocatable_on_order_qty_for_run, materials_dict
                            )
                            st.markdown("**Material Availability (Need vs. Physical Stock - Committed to Others):**")
                            st.markdown(materials_display_html, unsafe_allow_html=True)
                        else:
                            shortage_exists_for_order_button = False
                            st.warning("No required materials listed for this pending order.")
                    with col_actions:
                        if st.button("✅ Accept Request", key=f"accept_{order['id']}", use_container_width=True):
                            if accept_production_order(order['id']):
                                load_inventory_data_cached.clear(); load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear(); st.rerun()
                        if order.get('required_materials') and shortage_exists_for_order_button :
                             if st.button("🛒 Order Missing Materials", key=f"order_missing_{order['id']}", use_container_width=True):
                                if order_missing_materials_for_production_order(order['id']): # This now handles 402 from API
                                    load_inventory_data_cached.clear(); load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear(); st.rerun()
                    st.markdown("---")
            else: st.info("No pending production requests.")

        with accepted_tab:
            # ... (rest of existing accepted_tab logic)
            st.subheader("Accepted Orders")
            accepted_orders_data = get_production_orders(status="Accepted")
            if accepted_orders_data:
                for i, order in enumerate(accepted_orders_data):
                    order_id = order['id']; product_id = order['product_id']
                    product_name = products_dict.get(product_id, {}).get('name', product_id)
                    qty_needed = order['quantity']
                    requested_date_str = pd.to_datetime(order['requested_date']).strftime('%Y-%m-%d')
                    st.markdown(f"#### Order ID: `{order_id}`")
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.write(f"**Product:** {product_name}\n\n**Quantity Needed:** {qty_needed}\n\n**Requested Date:** {requested_date_str}")
                        if order.get('committed_materials'):
                            st.markdown("**Materials Committed for this Order:**"); st.markdown(format_bom([{'material_id': mid, 'quantity': q} for mid, q in order['committed_materials'].items()], materials_dict))
                        else: st.warning("No materials committed.")
                        finished_product_stock = physical_stock_snapshot.get(product_id, 0)
                        color = "green" if finished_product_stock >= qty_needed else "red"
                        st.markdown(f"**Finished Product Stock for '{product_name}':** <span style='color:{color};'>{finished_product_stock} available</span>", unsafe_allow_html=True)
                    with col2:
                        can_fulfill_now = finished_product_stock >= qty_needed
                        if st.button("✅ Fulfill from Stock", key=f"fulfill_accepted_{order_id}", use_container_width=True, disabled=not can_fulfill_now):
                            if fulfill_accepted_production_order_from_stock(order_id):
                                load_inventory_data_cached.clear(); load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear(); st.rerun()
                        if st.button("➡️ Send to Production", key=f"start_single_accepted_{order_id}", use_container_width=True):
                            if start_production([order_id]):
                                load_inventory_data_cached.clear(); load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear(); st.rerun()
                    st.markdown("---")
            else: st.info("No orders currently in 'Accepted' state.")

        with in_progress_tab:
            # ... (rest of existing in_progress_tab logic)
            st.subheader("In Progress Orders")
            in_progress_orders = get_production_orders(status="In Progress")
            if in_progress_orders:
                 orders_df_prog = pd.DataFrame(in_progress_orders)
                 orders_df_prog['Product'] = orders_df_prog['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df_prog['Started At'] = orders_df_prog['started_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_prog['Committed Materials (at start)'] = orders_df_prog['committed_materials'].apply(lambda x: format_bom([{'material_id': k, 'quantity': v} for k,v in x.items()], materials_dict) if x else "N/A")
                 st.dataframe(orders_df_prog[['id', 'Product', 'quantity', 'Started At', 'Committed Materials (at start)']].rename(columns={'id':'Order ID', 'quantity':'Qty'}), use_container_width=True, hide_index=True)
            else: st.info("No production orders currently in progress.")

        with completed_tab:
             # ... (rest of existing completed_tab logic)
            st.subheader("Completed Production Orders (Manufactured)")
            completed_orders = get_production_orders(status="Completed")
            if completed_orders:
                 orders_df_comp = pd.DataFrame(completed_orders)
                 orders_df_comp['Product'] = orders_df_comp['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                 orders_df_comp['Completed At'] = orders_df_comp['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                 orders_df_comp['Revenue Collected'] = orders_df_comp['revenue_collected'].apply(lambda x: "Yes" if x else "No")
                 st.dataframe(orders_df_comp[['id', 'Product', 'quantity', 'Completed At', 'Revenue Collected']].rename(columns={'id':'Order ID', 'quantity':'Qty'}), use_container_width=True, hide_index=True)
            else: st.info("No production orders have been completed through manufacturing yet.")

        with fulfilled_tab:
            # ... (rest of existing fulfilled_tab logic)
            st.subheader("Orders Fulfilled Directly From Stock")
            fulfilled_orders_data = get_production_orders(status="Fulfilled")
            if fulfilled_orders_data:
                orders_df_ful = pd.DataFrame(fulfilled_orders_data)
                orders_df_ful['Product'] = orders_df_ful['product_id'].apply(lambda x: products_dict.get(x, {}).get('name', x))
                orders_df_ful['Fulfilled At'] = orders_df_ful['completed_at'].apply(lambda x: pd.to_datetime(x).strftime('%Y-%m-%d %H:%M') if pd.notnull(x) else 'N/A')
                orders_df_ful['Revenue Collected'] = orders_df_ful['revenue_collected'].apply(lambda x: "Yes" if x else "No")
                st.dataframe(orders_df_ful[['id', 'Product', 'quantity', 'Fulfilled At', 'Revenue Collected']].rename(columns={'id':'Order ID', 'quantity':'Qty Fulfilled'}), use_container_width=True, hide_index=True)
            else: st.info("No orders have been marked as 'Fulfilled' from stock.")


elif page == "Purchasing":
    # (Existing Purchasing page logic - benefits from API client's 402 handling)
    st.header("🛒 Material Purchasing")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    elif not materials_list_data or not providers_list_data: st.warning("No materials or providers defined.")
    else:
        col1, col2 = st.columns([2, 1])
        with col1:
            st.subheader("Create Purchase Order")
            mat_opts = {m['id']: f"{m['name']} (ID: {m['id']})" for m in materials_list_data}
            sel_mat_id = st.selectbox("Material", options=list(mat_opts.keys()), format_func=lambda x: mat_opts[x], key="po_selected_material")
            if st.session_state.get("po_selected_material_prev") != sel_mat_id:
                st.session_state.pop("po_selected_provider", None); st.session_state.pop("po_selected_quantity", None)
                st.session_state["po_selected_material_prev"] = sel_mat_id
            with st.form("purchase_order_form"):
                avail_provs = [p for p in providers_dict.values() if any(o['material_id'] == sel_mat_id for o in p.get('catalogue', []))] if sel_mat_id else []
                if not avail_provs:
                    st.warning(f"No provider offers: {materials_dict.get(sel_mat_id, {}).get('name', sel_mat_id)}")
                    sel_prov_id = None; st.selectbox("Provider", options=[], disabled=True, key="po_selected_provider")
                    qty_val = st.number_input("Quantity (units)", 1, 1, 1, key="po_selected_quantity", disabled=True); submit_disabled = True
                else:
                    prov_opts = {p['id']: f"{p['name']} (ID: {p['id']})" for p in avail_provs}
                    sel_prov_id = st.selectbox("Provider", options=list(prov_opts.keys()), format_func=lambda x: prov_opts[x], key="po_selected_provider")
                    if sel_prov_id:
                        offering = next((o for o in providers_dict[sel_prov_id]['catalogue'] if o['material_id'] == sel_mat_id), None)
                        if offering: st.info(f"Price: €{offering['price_per_unit']:.2f}, Lead: {offering['lead_time_days']} days. Cost for order: €{offering['price_per_unit'] * st.session_state.get('po_selected_quantity',1):.2f}")
                    qty_val = st.number_input("Quantity (units)", 1, 10000, st.session_state.get('po_selected_quantity',1), key="po_selected_quantity") # Use session state for quantity
                    submit_disabled = not sel_prov_id
                if st.form_submit_button("Place Purchase Order", disabled=submit_disabled) and sel_mat_id and sel_prov_id and qty_val > 0:
                    if create_purchase_order(sel_mat_id, sel_prov_id, qty_val): # This now handles 402 from API
                        load_inventory_data_cached.clear(); load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear(); st.rerun()
        with col2:
            st.subheader("Providers & Offerings")
            if providers_list_data:
                for prov_item in providers_list_data:
                    with st.expander(f"{prov_item['name']}"):
                        st.write(f"ID: {prov_item['id']}"); st.markdown(format_catalogue(prov_item.get('catalogue',[]), materials_dict))
            else: st.info("No providers defined.")
        st.divider()
        st.subheader("Pending Purchase Orders")
        if pending_pos_data_global:
            pos_df_list = []
            for po in pending_pos_data_global:
                po_data = {
                    "PO ID": po['id'], 
                    "Material": materials_dict.get(po['material_id'],{}).get('name', po['material_id']),
                    "Qty": po['quantity_ordered'], 
                    "Provider": providers_dict.get(po['provider_id'],{}).get('name', po['provider_id']),
                    "Ordered": pd.to_datetime(po['order_date']).strftime('%Y-%m-%d %H:%M'),
                    "ETA": pd.to_datetime(po['expected_arrival_date']).strftime('%Y-%m-%d'),
                    "Cost EUR": f"{po.get('total_cost', 0.0):.2f}" # Display cost
                }
                pos_df_list.append(po_data)
            pos_df = pd.DataFrame(pos_df_list)
            st.dataframe(pos_df, use_container_width=True, hide_index=True)
        else: st.info("No pending purchase orders.")


elif page == "Inventory":
    # (Existing Inventory page logic)
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
                    fig = px.bar(fig_data.sort_values(chart_sel, ascending=False).head(20), x="Name", y=chart_sel, color="Type", title=f"{chart_sel} Levels (Top 20)", labels={'Name':'Item'})
                    st.plotly_chart(fig, use_container_width=True)
                 else: st.info(f"No items with non-zero {chart_sel} data to display.")
            else: st.info("Inventory is currently empty.")
        else: st.info("Could not retrieve inventory data or inventory is empty.")
        st.divider()
        st.subheader("📈 Item Stock Forecast")
        all_items_for_select = []
        if materials_list_data: all_items_for_select.extend([{"id": m['id'], "name": f"{m['name']} (Material)", "type": "Material"} for m in materials_list_data])
        if products_list_data: all_items_for_select.extend([{"id": p['id'], "name": f"{p['name']} (Product)", "type": "Product"} for p in products_list_data])
        if not all_items_for_select: st.info("No materials or products defined to generate a forecast.")
        else:
            sorted_items_for_select = sorted(all_items_for_select, key=lambda x: x['name'])
            col_item_select, col_days_select = st.columns(2)
            selected_item_id = col_item_select.selectbox("Select Item for Forecast:", options=[item['id'] for item in sorted_items_for_select], format_func=lambda item_id: next((item['name'] for item in sorted_items_for_select if item['id'] == item_id), "Unknown Item"), index=0 if sorted_items_for_select else None, key="forecast_item_select")
            selected_forecast_days = col_days_select.selectbox("Select Forecast Horizon (days):", options=[7, 14, 30], index=0, key="forecast_days_select")
            if selected_item_id and selected_forecast_days:
                historical_days_to_show = {7:3, 14:5, 30:10}.get(selected_forecast_days,3)
                forecast_data_response = load_item_forecast_cached(selected_item_id, selected_forecast_days, historical_days_to_show)
                if forecast_data_response and 'forecast' in forecast_data_response and forecast_data_response['forecast']:
                    forecast_df = pd.DataFrame(forecast_data_response['forecast']); forecast_df['date'] = pd.to_datetime(forecast_df['date']); forecast_df = forecast_df.sort_values(by='date')
                    item_display_name = forecast_data_response.get('item_name', selected_item_id)
                    current_day_data = forecast_df[forecast_df['day_offset'] == 0]
                    current_date_vline = current_day_data['date'].iloc[0] if not current_day_data.empty else (datetime.strptime(st.session_state.simulation_status['current_day'], '%Y-%m-%d') if st.session_state.simulation_status else datetime.now()) # Fallback
                    # ... (rest of existing forecast chart logic)
                    fig_forecast = px.line(title=f"Projected Stock for '{item_display_name}'")
                    past_and_current_df = forecast_df[forecast_df['day_offset'] <= 0]
                    current_and_future_df = forecast_df[forecast_df['day_offset'] >= 0]
                    if not past_and_current_df.empty: fig_forecast.add_trace(px.line(past_and_current_df, x='date', y='quantity').data[0].update(line=dict(color='royalblue', dash='dash'), name='Historical Context / Current'))
                    if not current_and_future_df.empty: fig_forecast.add_trace(px.line(current_and_future_df, x='date', y='quantity').data[0].update(line=dict(color='darkorange'), name='Forecast'))
                    if current_date_vline:
                        fig_forecast.add_vline(x=current_date_vline, line_width=2, line_dash="solid", line_color="green")
                        fig_forecast.add_annotation(x=current_date_vline, y=1.03, yref="paper", text="Current Day", showarrow=False, font=dict(color="green", size=12), xanchor="center", yanchor="bottom")
                    fig_forecast.update_layout(xaxis_title='Date', yaxis_title='Projected Quantity', legend_title_text='Legend'); fig_forecast.update_traces(mode='lines+markers')
                    st.plotly_chart(fig_forecast, use_container_width=True)

                elif forecast_data_response is None and st.session_state.simulation_status: st.warning(f"Could not retrieve forecast data for item ID '{selected_item_id}'.")
                elif not st.session_state.simulation_status: st.info("Simulation not initialized. Forecast unavailable.")
                else: st.info(f"No forecast data available for '{selected_item_id}' for the selected period.")


elif page == "History":
    # (Existing History page logic)
    st.header("📜 Simulation Event Log")
    if not st.session_state.simulation_status: st.warning("Simulation not initialized.")
    else:
        event_limit = st.slider("Number of recent events", 50, 500, 100, 50)
        events = get_events(limit=event_limit)
        if events:
            df = pd.DataFrame(events); df['timestamp'] = pd.to_datetime(df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
            df['details_short'] = df['details'].apply(lambda x: (json.dumps(x)[:100] + '...') if isinstance(x, dict) and len(json.dumps(x)) > 100 else json.dumps(x) if isinstance(x,dict) else str(x)[:100])
            st.dataframe(df[['day','timestamp','event_type','details_short']].rename(columns={'details_short':'Details Preview'}), height=500, hide_index=True, use_container_width=True)
            # ... (rest of existing history event details and charts logic)
            with st.expander("View Full Event Details"):
                sel_ev_id = st.selectbox("Event ID:", options=df['id'].tolist(), index=None)
                if sel_ev_id: st.json(df[df['id'] == sel_ev_id]['details'].iloc[0])
            demand_events = df[df['event_type'].isin(['order_received_for_production', 'product_shipped_from_stock', 'production_order_fulfilled_from_stock', 'accepted_order_fulfilled_from_stock'])].copy()
            if not demand_events.empty:
                demand_events['day'] = demand_events['day'].astype(int)
                def get_demand_qty(row):
                    details = row['details'];_ = row['event_type']
                    if not isinstance(details, dict): return 0
                    if _ == 'order_received_for_production': return details.get('original_demand', details.get('qty_for_prod',0))
                    if _ == 'product_shipped_from_stock': return details.get('demand_qty', details.get('qty_shipped',0))
                    return details.get('quantity_fulfilled',0) # for other fulfilled types
                demand_events['total_demand_qty'] = demand_events.apply(get_demand_qty, axis=1)
                demand_per_day = demand_events[demand_events['total_demand_qty'] > 0].groupby('day')['total_demand_qty'].sum().reset_index()
                if not demand_per_day.empty: st.plotly_chart(px.bar(demand_per_day, x='day', y='total_demand_qty', title='Total Product Units Demanded Per Day (New Orders)'), use_container_width=True)
        else: st.info("No simulation events recorded.")


elif page == "Setup & Data":
    st.header("⚙️ Setup & Data Management")
    st.subheader("Initial Conditions")
    st.info("Define the starting state of your factory simulation here. This will reset any current simulation. Ensure product IDs in 'product_prices' match those in the 'products' list.")
    
    # Updated default_initial_conditions with financial_config
    default_initial_conditions = {
        "materials": [
            {"id": "mat-001", "name": "Plastic Filament Spool", "description": "Standard PLA 1kg"},
            {"id": "mat-002", "name": "Frame Component A"}, {"id": "mat-003", "name": "Frame Component B"},
            {"id": "mat-004", "name": "Electronics Board v1"}, {"id": "mat-005", "name": "Power Supply Unit"},
            {"id": "mat-006", "name": "Fasteners Pack (100pcs)"}
        ], 
        "products": [
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
        ], 
        "providers": [
            {"id": "prov-001", "name": "Filament Inc.", "catalogue": [{"material_id": "mat-001", "price_per_unit": 20.0, "offered_unit_size": 1, "lead_time_days": 2}]},
            {"id": "prov-002", "name": "Frame Parts Co.", "catalogue": [
                {"material_id": "mat-002", "price_per_unit": 5.0, "offered_unit_size": 1, "lead_time_days": 5},
                {"material_id": "mat-003", "price_per_unit": 6.0, "offered_unit_size": 1, "lead_time_days": 5}]},
            {"id": "prov-003", "name": "Electronics Hub", "catalogue": [
                {"material_id": "mat-004", "price_per_unit": 50.0, "offered_unit_size": 1, "lead_time_days": 7},
                {"material_id": "mat-005", "price_per_unit": 30.0, "offered_unit_size": 1, "lead_time_days": 4}]},
            {"id": "prov-004", "name": "Hardware Supplies Ltd.", "catalogue": [{"material_id": "mat-006", "price_per_unit": 10.0, "offered_unit_size": 1, "lead_time_days": 3}]}
        ], 
        "initial_inventory": {
            "mat-001": 50, "mat-002": 100, "mat-003": 100, "mat-004": 20, "mat-005": 30, "mat-006": 50, 
            "prod-001": 5, "prod-002": 0
        }, 
        "storage_capacity": 5000, 
        "daily_production_capacity": 5,
        "random_order_config": {"min_orders_per_day": 0, "max_orders_per_day": 2, "min_qty_per_order": 1, "max_qty_per_order": 3},
        "financial_config": { # Added financial_config block
            "initial_balance": 50000.0,
            "product_prices": {
                "prod-001": 350.0, # Price for Basic 3D Printer
                "prod-002": 750.0  # Price for Advanced 3D Printer
            },
            "daily_operational_cost_base": 100.0, # Fixed cost per day
            "daily_operational_cost_per_item_in_production": 10.0 # Cost per item being made
        }
    }
    edited_conditions_str = st.text_area(
        "Initial Conditions JSON (includes financial_config)", value=json.dumps(default_initial_conditions, indent=2), height=400, key="initial_cond_json"
    )
    if st.button("Initialize Simulation with Above Data", type="primary"):
        try:
            conditions_data = json.loads(edited_conditions_str)
            # Validate that financial_config and product_prices exist before initializing
            if "financial_config" not in conditions_data:
                st.error("Error: 'financial_config' block is missing in the JSON.")
            elif "product_prices" not in conditions_data["financial_config"]:
                st.error("Error: 'product_prices' is missing within 'financial_config'.")
            else:
                api_success = initialize_simulation(conditions_data)
                if api_success:
                     load_base_data_cached.clear(); load_inventory_data_cached.clear()
                     load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear()
                     st.query_params["page"] = "Dashboard"; st.rerun()
        except json.JSONDecodeError: st.error("Invalid JSON format in Initial Conditions.")
        except Exception as e: st.error(f"Error initializing simulation: {e}")
    
    st.divider()
    st.subheader("Data Export / Import")
    col_exp, col_imp = st.columns(2)
    with col_exp:
        st.write("Export the current simulation state, events, definitions, and financial config to a JSON file.")
        if st.session_state.simulation_status: # Check if sim is initialized
            if st.button("Prepare Export Data"):
                exported_data_content = export_data()
                if exported_data_content:
                    current_day_val = st.session_state.simulation_status.get('current_day', 0)
                    st.download_button(label="Download Exported Data (JSON)", data=json.dumps(exported_data_content, indent=2),
                                       file_name=f"mrp_sim_export_day{current_day_val}_{datetime.now().strftime('%Y%m%d_%H%M')}.json", mime="application/json")
        else: st.info("Initialize simulation to enable data export.")
    with col_imp:
        st.write("Import a previously exported JSON file. This will **overwrite** the current simulation.")
        uploaded_file = st.file_uploader("Choose a JSON file to import", type="json")
        if uploaded_file is not None:
            try:
                import_file_content = uploaded_file.getvalue().decode("utf-8")
                import_json_data = json.loads(import_file_content)
                # Basic validation for key structures in the import file
                if all(k in import_json_data for k in ["simulation_state", "products", "materials", "financial_config"]):
                     if st.button("Confirm Import Data", type="danger"):
                         if import_data(import_json_data): # api_client.import_data returns bool
                             load_base_data_cached.clear(); load_inventory_data_cached.clear()
                             load_pending_purchase_orders_cached.clear(); load_financial_data_cached.clear()
                             st.query_params["page"] = "Dashboard"; st.rerun()
                else: st.error("Uploaded file does not appear to be a valid simulation export (missing key fields like 'simulation_state' or 'financial_config').")
            except json.JSONDecodeError: st.error("Invalid JSON file.")
            except Exception as e: st.error(f"Error processing import file: {e}")