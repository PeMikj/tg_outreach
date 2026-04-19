from __future__ import annotations

import json

from app.main import database_backend_name, get_applied_migrations, get_db


def main() -> None:
    connection = get_db()
    try:
        applied_migrations = get_applied_migrations(connection)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "database_backend": database_backend_name(),
                    "applied_count": len(applied_migrations),
                    "applied_versions": applied_migrations,
                }
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
