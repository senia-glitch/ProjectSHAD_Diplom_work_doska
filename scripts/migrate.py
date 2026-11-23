"""Apply reviewed revisions only. No autogenerate or reset."""
import argparse
import os
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect
from app.config import ROOT, database_name, database_url


def configuration():
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'alembic'))
    return cfg


def assert_current():
    cfg = configuration()
    engine = create_engine(database_url(), echo=False, hide_parameters=True, connect_args={'connect_timeout': 5})
    try:
        with engine.connect() as connection:
            migration = MigrationContext.configure(connection, opts={'compare_type': True, 'compare_server_default': True})
            actual = set(migration.get_current_heads())
            expected = set(ScriptDirectory.from_config(cfg).get_heads())
            if actual != expected:
                raise RuntimeError('Database revision is not current; apply reviewed migrations separately')
            from models import metadata
            if compare_metadata(migration, metadata):
                raise RuntimeError('Database schema differs from models; review a migration before starting')
    finally:
        engine.dispose()


def upgrade(confirmed, allowed_legacy_tables=()):
    if confirmed != database_name(os.getenv('FACTORY_TEST_MODE') == '1'):
        raise ValueError('Confirm the exact configured target database name')
    engine = create_engine(database_url(), echo=False, hide_parameters=True, connect_args={'connect_timeout': 5})
    try:
        with engine.begin() as connection:
            # Refuse unrelated tables before migration; never clean the schema.
            from models import metadata
            tables = set(inspect(connection).get_table_names())
            # Reviewed RENAME transitions may have former names absent from new metadata.
            if tables - set(metadata.tables) - {'alembic_version'} - set(allowed_legacy_tables):
                raise ValueError('Target database contains unrelated tables')
            cfg = configuration()
            cfg.attributes['connection'] = connection
            command.upgrade(cfg, 'head')
    finally:
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--allow-legacy-table', action='append', default=[],
                        help='Exact former table name used by a reviewed migration; repeat if needed')
    args = parser.parse_args()
    upgrade(args.database, args.allow_legacy_table)
    print('Reviewed migrations applied')
