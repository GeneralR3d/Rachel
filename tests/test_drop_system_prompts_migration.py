"""Exercise the retired prompt table's migration against a disposable database."""

from pathlib import Path
import unittest

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa

from app.models import Base


class DropSystemPromptsMigrationTests(unittest.TestCase):
    def setUp(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
        self.scripts = ScriptDirectory.from_config(config)

    def test_migration_extends_the_existing_head(self):
        self.assertEqual(self.scripts.get_heads(), ["0a7e2c9d4b61"])
        self.assertEqual(self.scripts.get_revision("0a7e2c9d4b61").down_revision, "f9c6a1d4b3e8")
        self.assertEqual(self.scripts.get_revision("f9c6a1d4b3e8").down_revision, "e8b5d0f3a2c7")

    def test_database_at_proactive_revision_can_upgrade_to_head(self):
        # A deployed DB can already be stamped at this feature-branch revision.
        # Resolving only main's head misses the missing applied-revision failure.
        steps = list(self.scripts.iterate_revisions("heads", "e8b5d0f3a2c7"))
        self.assertEqual([step.revision for step in steps], ["0a7e2c9d4b61", "f9c6a1d4b3e8"])

    def test_upgrade_removes_table_and_downgrade_restores_its_empty_schema(self):
        migration = self.scripts.get_revision("f9c6a1d4b3e8").module
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = sa.MetaData()
        prompts = sa.Table(
            "system_prompts", metadata,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("responder_system_prompt", sa.Text(), nullable=False),
            sa.Column("summarizer_system_prompt", sa.Text(), nullable=False, server_default=""),
        )
        unrelated = sa.Table("unrelated", metadata, sa.Column("id", sa.Integer(), primary_key=True))
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(prompts.insert().values(responder_system_prompt="old prompt"))
            connection.execute(unrelated.insert().values(id=7))
            original_columns = sa.inspect(connection).get_columns("system_prompts")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                self.assertFalse(sa.inspect(connection).has_table("system_prompts"))
                self.assertEqual(connection.scalar(sa.select(unrelated.c.id)), 7)
                migration.downgrade()

            restored = sa.inspect(connection)
            for before, after in zip(original_columns, restored.get_columns("system_prompts"), strict=True):
                for key in ("name", "nullable", "default", "primary_key"):
                    self.assertEqual(before[key], after[key])
                self.assertEqual(str(before["type"]), str(after["type"]))
            self.assertEqual(restored.get_pk_constraint("system_prompts")["constrained_columns"], ["id"])
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(prompts)), 0)

    def test_metadata_does_not_reintroduce_the_retired_table(self):
        self.assertNotIn("system_prompts", Base.metadata.tables)


if __name__ == "__main__":
    unittest.main()
