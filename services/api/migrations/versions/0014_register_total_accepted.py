"""Pick registers: a written column total that does not match the picks must be checked before approval.

- pick_register_total.accepted: the exact check (written total and calculated sum) a person looked at and accepted.

Revision ID: 0014
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pick_register_total ADD COLUMN accepted text "
        "CHECK (accepted IS NULL OR char_length(accepted) <= 300);"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pick_register_total DROP COLUMN IF EXISTS accepted;")
