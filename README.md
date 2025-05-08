# MRP Factory Simulation

A web application simulating the Material Requirements Planning (MRP) process for a 3D printer factory day by day. Users act as factory managers, making decisions on production and purchasing.

## Features

* **Day-by-Day Simulation:** Advance the simulation one day at a time.
* **Inventory Management:** Track material and finished product stock levels.
* **Production Planning:** View production orders, check BOMs, and initiate production runs based on material availability.
* **Material Purchasing:** View provider catalogues, place purchase orders, and track expected arrivals.
* **Dynamic Order Generation:** Random production orders are generated daily (configurable).
* **Resource Constraints:** Simulates limited daily production output capacity and storage capacity.
* **Web Dashboard:** Interactive UI built with Streamlit for managing the simulation.
* **API:** All functionalities exposed via a FastAPI backend.
* **Persistence:** MongoDB stores simulation state, definitions, and event history.
* **Data Import/Export:** Save and load simulation state and definitions using JSON.
* **Dockerized:** Easily run the entire application stack using Docker Compose.

## Tech Stack

* **Backend:** Python 3.11.2, FastAPI, Pydantic, Motor (Async MongoDB Driver), SimPy
* **Frontend:** Streamlit, Requests, Pandas, Plotly
* **Database:** MongoDB
* **Containerization:** Docker, Docker Compose

## Project Structure

mrp_simulation/
├── backend/        # FastAPI application, simulation logic, DB interaction
├── frontend/       # Streamlit dashboard application
├── data/           # Example initial data and export location
├── docker-compose.yml # Docker Compose configuration
└── README.md       # This file

## Setup & Running

1.  **Prerequisites:**
    * Docker ([Install Docker](https://docs.docker.com/get-docker/))
    * Docker Compose ([usually included with Docker Desktop](https://docs.docker.com/compose/install/))

2.  **Clone the Repository (or create files):**
    Ensure you have all the files listed above in the correct directory structure.

3.  **Build and Run using Docker Compose:**
    Open a terminal in the root directory (`mrp_simulation/`) and run:
    ```bash
    docker-compose up --build -d
    ```
    * `--build`: Forces Docker to build the images if they don't exist or if code changed.
    * `-d`: Runs the containers in detached mode (in the background).

4.  **Access the Application:**
    * **Frontend (Streamlit Dashboard):** Open your web browser and go to `http://localhost:8501`
    * **Backend (API Docs):**
        * Swagger UI: `http://localhost:8000/docs`
        * ReDoc: `http://localhost:8000/redoc`

5.  **Initialize the Simulation:**
    * Navigate to the "Setup & Data" page in the Streamlit dashboard.
    * Review or modify the initial conditions JSON.
    * Click "Initialize Simulation with Above Data".

6.  **Run the Simulation:**
    * Use the sidebar controls ("Advance 1 Day") and navigate through the pages ("Dashboard", "Production", "Purchasing", etc.) to manage the factory.

7.  **Stopping the Application:**
    To stop the containers, run the following command in the terminal from the root directory:
    ```bash
    docker-compose down
    ```
    To stop and remove the data volume (lose all MongoDB data):
    ```bash
    docker-compose down -v
    ```

## API Usage

The FastAPI backend exposes all functionalities. You can interact with it directly using tools like `curl`, Postman, or programmatically. Refer to the API documentation available at `http://localhost:8000/docs` when the application is running.

## Development Notes

* The included `docker-compose.yml` mounts the source code directories (`backend/app`, `frontend`) into the running containers. This means you can modify the Python code, and the changes *should* be reflected automatically (FastAPI's `uvicorn` and Streamlit usually handle reloading). If not, you might need to restart the specific container (`docker-compose restart backend` or `docker-compose restart frontend`).
* Ensure Python 3.11.2 is used if running locally outside Docker.

## Potential Improvements

* More sophisticated SimPy modelling (e.g., worker resources, machine downtime, parallel processes).
* Detailed cost tracking (material costs, production costs, inventory holding costs).
* More advanced charting and analytics (e.g., lead time analysis, stockout frequency).
* User authentication/authorization for the API/UI.
* Asynchronous task queue (like Celery) for long-running simulation steps if `run_day` becomes too slow.
* Enhanced error handling and logging.
* Unit and integration tests.