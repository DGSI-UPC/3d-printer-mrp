import uuid
from datetime import datetime, timezone

def generate_id():
    """Generates a unique string ID."""
    return str(uuid.uuid4())

def get_current_utc_timestamp():
    """Returns the current UTC timestamp."""
    return datetime.now(timezone.utc)

def get_current_utc_date_iso():
    """Returns the current UTC date in ISO format (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).date().isoformat()