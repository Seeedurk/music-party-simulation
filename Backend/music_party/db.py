import os
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


DATABASE = {
    "host": os.getenv("PGHOST", "127.0.0.1"),
    "port": int(os.getenv("PGPORT", "4000")),
    "dbname": os.getenv("PGDATABASE", "postgres"),
    "user": os.getenv("PGUSER", "postgres"),
}
if os.getenv("PGPASSWORD"):
    DATABASE["password"] = os.environ["PGPASSWORD"]


def connect_db():
    return psycopg.connect(**DATABASE, row_factory=dict_row)


def json_safe(value):
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
