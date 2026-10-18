"""The migrated history schema supports the ORM without losing existing rows."""

import os
from io import StringIO
from pathlib import Path
import unittest
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import History


class HistoryMetadataTests(unittest.TestCase):
    def test_history_metadata_preserves_existing_chat_index(self):
        self.assertIn("ix_history_chat_id", {index.name for index in History.__table__.indexes})

    def test_timestamp_upgrade_can_be_emitted_without_a_database_connection(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        migration = ScriptDirectory.from_config(config).get_revision("0a7e2c9d4b61").module
        output = StringIO()
        with Operations.context(MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})):
            migration.upgrade()
        self.assertIn("ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()", output.getvalue())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run Postgres migration tests")
class HistorySchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_preserves_rows_and_existing_timestamps(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        migration = ScriptDirectory.from_config(config).get_revision("0a7e2c9d4b61").module
        engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                for existing_timestamp in (False, True):
                    with self.subTest(existing_timestamp=existing_timestamp):
                        schema = "history_migration_" + uuid4().hex
                        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                        await connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                        await connection.exec_driver_sql("CREATE TABLE history (chat_id BIGINT NOT NULL, content TEXT NOT NULL)")
                        await connection.exec_driver_sql("CREATE INDEX ix_history_chat_id ON history (chat_id)")
                        if existing_timestamp:
                            await connection.exec_driver_sql("ALTER TABLE history ADD created_at TIMESTAMPTZ NOT NULL DEFAULT '2000-01-01T00:00:00Z'")
                        await connection.exec_driver_sql("INSERT INTO history (chat_id, content) VALUES (42, 'Existing conversation')")

                        def upgrade(conn):
                            with Operations.context(MigrationContext.configure(conn)):
                                migration.upgrade()
                        await connection.run_sync(upgrade)
                        row = (await connection.exec_driver_sql("SELECT content, created_at FROM history")).one()
                        self.assertEqual(row[0], "Existing conversation")
                        self.assertIsNotNone(row[1])
                        if existing_timestamp:
                            self.assertEqual(row[1].year, 2000)

                        def downgrade(conn):
                            with Operations.context(MigrationContext.configure(conn)):
                                migration.downgrade()
                        await connection.run_sync(downgrade)
                        self.assertEqual((await connection.exec_driver_sql("SELECT content FROM history")).scalar(), "Existing conversation")
                        indexes = await connection.run_sync(lambda conn: sa.inspect(conn).get_indexes("history"))
                        self.assertEqual([index["name"] for index in indexes], ["ix_history_chat_id"])
                        await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
