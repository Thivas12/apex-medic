"""Credential-free PostgreSQL checks and explicit schema initialization."""

import argparse

from backend.app.db.config import (
    DatabaseConfigurationError, PostgresDatabase, StorageUnavailable, resolve_database,
)
from backend.app.db.postgres import transaction
from backend.app.db.schema import check_schema, migrate


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or initialize ApexMedic PostgreSQL storage.")
    parser.add_argument("command", choices=("check", "migrate"))
    args = parser.parse_args()
    try:
        database = resolve_database()
        if not isinstance(database, PostgresDatabase):
            raise DatabaseConfigurationError("PostgreSQL is not selected. Set DATABASE_URL in .env.local or the environment.")
        if args.command == "migrate":
            changed = migrate(database)
            print("PostgreSQL schema initialized." if changed else "PostgreSQL schema is already current.")
        with transaction(database) as connection:
            connection.execute("SELECT 1").fetchone()
            exists = connection.execute("SELECT to_regclass('apexmedic.schema_migrations')").fetchone()[0]
        print("PostgreSQL connection: OK")
        if exists is None:
            print("Schema: not initialized. Run: python -m backend.app.db.manage migrate")
        else:
            check_schema(database)
            print("Schema: current")
        return 0
    except (DatabaseConfigurationError, StorageUnavailable) as error:
        print(f"Error: {error}")
        return 1
    except OSError:
        print("Error: Could not read the local database configuration or migration file.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
