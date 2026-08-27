"""Neon pool-database connection helper.

Called through the module (``db.get_pool_db_connection()``) everywhere so that
tests can substitute the connection factory with a single patch point.
"""

import os
from typing import Any

import psycopg2


def get_pool_db_connection() -> Any:
    """Open a psycopg2 connection to the Neon pool database."""
    database_url = os.environ["DATABASE_URL"]
    return psycopg2.connect(database_url)
