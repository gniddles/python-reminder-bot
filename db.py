import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ["DATABASE_URL"]

def get_conn():
    """Return a new connection to the PostgreSQL database."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
