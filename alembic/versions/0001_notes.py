"""Initial reviewed schema for the example notes resource."""
from alembic import op
import sqlalchemy as sa

revision = '0001_notes'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('notes', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('title', sa.String(200), nullable=False))


def downgrade():
    raise RuntimeError('Destructive downgrade requires an explicit reviewed data migration')
