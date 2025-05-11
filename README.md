# MRP Factory Simulation

A web application simulating the Material Requirements Planning (MRP) and financial operations for a 3D printer factory day by day. Users act as factory managers, making decisions on production, purchasing, and monitoring the financial health of the business.

## Key Features

* **Day-by-Day Simulation:** Advance the simulation one day at a time, triggering material arrivals, production completions, and financial transactions.
* **Comprehensive Financial System:**
    * **Initial Setup:** Configure starting balance, product selling prices, base daily operational costs, and per-item-in-production operational costs via the initial JSON setup.
    * **Transaction Management:** Material purchases deduct from the balance, and product sales (from completed/fulfilled customer orders) add to it. Daily operational costs are automatically deducted.
    * **Fund Constraints:** Material purchases are prevented if the factory lacks sufficient funds.
    * **Financials Page:** A dedicated page to view key financial metrics (current balance, total revenue/expenses, profit), historical financial performance charts (balance over time, daily income/costs), and a projected financial forecast.
    * **Dashboard Summary:** Key financial information, like the current balance, is visible on the main dashboard.
* **Inventory Management:**
    * Track material and finished product stock levels (physical, committed, on order, projected available).
    * View detailed inventory status across all items.
    * Generate item-specific stock level forecasts for a configurable number of days, including historical stock levels.
* **Production Planning & Execution:**
    * View production orders categorized by status (Pending, Accepted, In Progress, Completed, Fulfilled).
    * Review Bill of Materials (BOM) for products.
    * **Accept Orders:** Fulfill from existing stock or commit available materials.
    * **Order Missing Materials:** Automatically check for shortages for a production order and initiate purchase orders (subject to available funds).
    * **Start Production:** Move 'Accepted' orders to 'In Progress', consuming committed materials over time (conceptually, as production progresses).
    * **Fulfill from Stock:** Directly fulfill 'Accepted' orders if sufficient finished product is available, bypassing new production.
* **Material Purchasing:**
    * View provider catalogues with material prices and lead times.
    * Place purchase orders for specific materials from chosen providers.
    * Track pending purchase orders and their expected arrival dates and costs.
* **Dynamic Order Generation:** Random customer production orders are generated daily (configurable frequency and quantity).
* **Resource Constraints:** Simulates limited daily production output capacity and overall storage capacity.
* **Event Logging:** Detailed log of all significant simulation events, including financial transactions, inventory changes, order status updates, and errors.
* **Web Dashboard:** Interactive UI built with Streamlit for managing the simulation.
* **API:** All functionalities exposed via a FastAPI backend.
* **Persistence:** MongoDB stores simulation state, entity definitions (materials, products, providers), configurations, and event history.
* **Data Import/Export:** Save and load the entire simulation state, including definitions and financial configurations, using JSON.
* **Dockerized:** Easily run the entire application stack using Docker Compose.

## Tech Stack

* **Backend:** Python 3.11+, FastAPI, Pydantic, Motor (Async MongoDB Driver), SimPy
* **Frontend:** Streamlit, Requests, Pandas, Plotly
* **Database:** MongoDB
* **Containerization:** Docker, Docker Compose

## Project Structure

```
mrp-simulation/
├── backend/        # FastAPI application, simulation logic, DB interaction
│   ├── app/
│   │   ├── init.py
│   │   ├── crud.py         # Database CRUD operations
│   │   ├── database.py     # MongoDB connection and setup
│   │   ├── main.py         # FastAPI application entrypoint, API routes
│   │   ├── models.py       # Pydantic models for data structures
│   │   ├── simulation.py   # Core simulation logic (FactorySimulation class)
│   │   └── utils.py        # Utility functions
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/       # Streamlit dashboard application
│   ├── init.py
│   ├── api_client.py   # Functions to interact with the backend API
│   ├── app.py          # Main Streamlit application script
│   ├── Dockerfile
│   └── requirements.txt
├── data/           # Example initial data (if any, currently defaults are in frontend)
├── docker-compose.yml # Docker Compose configuration
└── README.md       # This file
```

## Setup & Running

1.  **Prerequisites:**
    * Docker ([Install Docker](https://docs.docker.com/get-docker/))
    * Docker Compose (usually included with Docker Desktop)

2.  **Clone the Repository (or ensure files are present):**
    Make sure you have all the files as per the project structure.

3.  **Build and Run using Docker Compose:**
    Open a terminal in the root directory (`mrp-simulation/`) and run:
    ```bash
    docker-compose up --build -d
    ```
    * `--build`: Forces Docker to build/rebuild the images.
    * `-d`: Runs the containers in detached mode.

4.  **Access the Application:**
    * **Frontend (Streamlit Dashboard):** `http://localhost:8501`
    * **Backend API Docs (Swagger UI):** `http://localhost:8000/docs`
    * **Backend API Docs (ReDoc):** `http://localhost:8000/redoc`

5.  **Initialize the Simulation:**
    * Navigate to the "Setup & Data" page in the Streamlit dashboard.
    * The page provides a default JSON structure for initial conditions, including materials, products, providers, initial inventory, capacities, and the new `financial_config` (initial balance, product prices, operational costs). Review or modify this JSON.
    * Click "Initialize Simulation with Above Data".

6.  **Run the Simulation:**
    * Use the sidebar controls ("Advance 1 Day") and navigate through the pages ("Dashboard", "Finances", "Production", "Purchasing", etc.) to manage the factory.

7.  **Stopping the Application:**
    To stop the containers:
    ```bash
    docker-compose down
    ```
    To stop and remove the data volume (this will delete all MongoDB data):
    ```bash
    docker-compose down -v
    ```

## API Usage

The FastAPI backend exposes all functionalities. You can interact with it directly. Refer to the API documentation (Swagger UI at `http://localhost:8000/docs`) when the application is running for detailed endpoint information. Key new endpoints include `/finances` for financial overview and forecasts.

## Development Notes

* The `docker-compose.yml` mounts the source code directories (`backend/app`, `frontend`) into the running containers, enabling live reloading for most code changes during development.
* If running locally outside Docker, ensure Python 3.11+ and all dependencies from `requirements.txt` in both `backend` and `frontend` directories are installed.

## Potential Future Improvements

* More sophisticated SimPy modeling (e.g., explicit worker resources, machine setup times, parallel processing lines).
* Advanced financial features (e.g., loans, investments, detailed cost breakdown analysis).
* User authentication and multi-user support.
* Enhanced error handling and user feedback across the UI.
* Comprehensive unit and integration tests for backend and frontend.
* More granular configuration for production (e.g., shift patterns, maintenance schedules).
* Integration of a more formal sales order system separate from random demand generation.
