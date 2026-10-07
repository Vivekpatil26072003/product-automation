"""Email channel: EmailJS (browser) alongside Microsoft Graph (server).

With the EmailJS channel the intent ledger is unchanged (one email_message per confirmed draft, same states and
transition trigger), but the provider call happens in the Sender's browser: there is no Microsoft connection,
no server-side sender mailbox and no attachment (the report figures are in the message). Existing rows are
'graph'.

Revision ID: 0009
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE email_message ADD COLUMN channel text NOT NULL DEFAULT 'graph'
          CHECK (channel IN ('graph', 'emailjs'));
        ALTER TABLE email_message ALTER COLUMN connection_id DROP NOT NULL;
        ALTER TABLE email_message ALTER COLUMN sender_mailbox DROP NOT NULL;
        ALTER TABLE email_message ALTER COLUMN attachment_name DROP NOT NULL;
        ALTER TABLE email_message ALTER COLUMN attachment_bytes DROP NOT NULL;
        ALTER TABLE email_message ADD CONSTRAINT email_message_graph_fields CHECK (
          channel <> 'graph' OR (connection_id IS NOT NULL AND sender_mailbox IS NOT NULL
                                 AND attachment_name IS NOT NULL AND attachment_bytes IS NOT NULL));
        """
    )


def downgrade() -> None:
    # Sent-email history is kept; the relaxed columns stay nullable so EmailJS rows remain valid.
    op.execute(
        "ALTER TABLE email_message DROP CONSTRAINT IF EXISTS email_message_graph_fields;"
        "ALTER TABLE email_message DROP COLUMN channel;"
    )
