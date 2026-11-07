"""Versioned, lightweight schema migrations for the packaged application."""

from __future__ import annotations

from sqlalchemy import inspect, text


MIGRATIONS = (
    (
        "20261103_work_ergonomics",
        {
            "work_profiles": {"birth_date": "DATE", "accommodations": "JSON", "consented_at": "DATETIME", "consent_recorded_by": "INTEGER"},
            "work_sections": {"ergonomics": "JSON"},
        },
    ),
    (
        "20261103_tool_fit_status",
        {"tool_fits": {"fit_status": "VARCHAR(24) NOT NULL DEFAULT 'not_checked'"}},
    ),
    (
        "20260823_security_sessions",
        {
            "users": {
                "is_active": "BOOLEAN NOT NULL DEFAULT 1",
                "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
                "locked_until": "DATETIME",
                "last_login_at": "DATETIME",
                "last_seen_at": "DATETIME",
                "password_changed_at": "DATETIME",
            },
        },
    ),
    (
        "20260823_checklist_review_and_handoff",
        {
            "checklist_completions": {
                "status": "VARCHAR(24) NOT NULL DEFAULT 'pending'",
                "note": "TEXT",
                "image_file": "VARCHAR(200)",
                "reviewed_by": "INTEGER",
                "reviewed_at": "DATETIME",
                "review_note": "TEXT",
            },
        },
    ),
    (
        "20260823_checklist_status_backfill",
        {},
    ),
    (
        "20260916_audit_request_context",
        {
            "activity_logs": {
                "ip_address": "VARCHAR(64)",
                "user_agent": "VARCHAR(500)",
            },
        },
    ),
    (
        "20261019_observer_manager_appointment",
        {
            "users": {
                "is_appointed_manager": "BOOLEAN NOT NULL DEFAULT 0",
            },
        },
    ),
)


def apply_migrations(engine) -> list[str]:
    """Apply missing column migrations and return newly applied revisions."""
    applied_revisions = []
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "revision VARCHAR(100) PRIMARY KEY, "
                "applied_at DATETIME NOT NULL"
                ")"
            )
        )
        existing_revisions = {
            row[0]
            for row in connection.execute(
                text("SELECT revision FROM schema_migrations")
            )
        }

        for revision, table_columns in MIGRATIONS:
            if revision in existing_revisions:
                continue

            for table_name, columns in table_columns.items():
                inspector = inspect(connection)
                existing_columns = {
                    column["name"]
                    for column in inspector.get_columns(table_name)
                }
                for column_name, definition in columns.items():
                    if column_name in existing_columns:
                        continue
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table_name}" '
                            f'ADD COLUMN "{column_name}" {definition}'
                        )
                    )
                    existing_columns.add(column_name)

            if revision == "20261019_observer_manager_appointment":
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_users_single_appointed_manager "
                        "ON users (is_appointed_manager) "
                        "WHERE is_appointed_manager = 1"
                    )
                )

            if revision == "20260823_checklist_status_backfill":
                connection.execute(
                    text(
                        "UPDATE checklist_completions "
                        "SET status = 'completed' "
                        "WHERE is_completed = 1 "
                        "AND (status IS NULL OR status = 'pending')"
                    )
                )

            connection.execute(
                text(
                    "INSERT INTO schema_migrations (revision, applied_at) "
                    "VALUES (:revision, CURRENT_TIMESTAMP)"
                ),
                {"revision": revision},
            )
            applied_revisions.append(revision)

    return applied_revisions
