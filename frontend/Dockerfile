# Use the specified Python version
FROM python:3.11.2-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy frontend code
COPY . /app/

# Expose Streamlit default port
EXPOSE 8501

# Command to run Streamlit
# Use --server.enableCORS false if CORS is handled by FastAPI or a gateway
# Use --server.enableXsrfProtection=false for simpler setups if needed, but be aware of security implications.
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]