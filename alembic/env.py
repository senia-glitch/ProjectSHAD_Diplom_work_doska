from alembic import context
from models import metadata

cfg = context.config
connection = cfg.attributes.get('connection')
if connection is None:
    raise RuntimeError('Use python -m scripts.migrate with an explicitly confirmed database')
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
