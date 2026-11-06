"""Tiny forward-only schema upgrade.

`create_all` creates missing tables but never adds columns to existing ones.
When a later step adds a column, this adds it to the deployed database on
start-up so the Render Postgres keeps working without a manual migration.
Columns are only ever added, never changed or dropped.
"""
import logging

from sqlalchemy import Boolean, Engine, String, Text, inspect, text

log = logging.getLogger("migrate")


def _default_sql(col) -> str:
    default = col.default
    if default is None or not getattr(default, "is_scalar", False):
        return ""
    value = default.arg
    if isinstance(col.type, Boolean):
        return " DEFAULT TRUE" if value else " DEFAULT FALSE"
    if isinstance(col.type, (String, Text)) and isinstance(value, str):
        return " DEFAULT '" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    return ""


def add_missing_columns(engine: Engine, metadata) -> list[str]:
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{_default_sql(col)}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.warning("added columns: %s", ", ".join(added))
    return added
