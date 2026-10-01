"""Ad-hoc connectivity check -- confirms DATABASE_URL works and the
Alembic-managed schema is actually there. Not a test (needs a real
network call); run manually: python -m scripts.check_db_connection"""

from __future__ import annotations

from sqlalchemy import inspect, text

from core.db.session import engine


def main() -> None:
    with engine.connect() as conn:
        version = conn.execute(text("select version()")).scalar()
        print(f"connected. {version}")

    tables = sorted(inspect(engine).get_table_names())
    print(f"{len(tables)} tables: {tables}")


if __name__ == "__main__":
    main()
