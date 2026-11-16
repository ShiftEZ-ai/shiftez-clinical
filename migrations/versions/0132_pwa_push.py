"""Additive PWA branding artifacts, consented subscriptions and push delivery metadata."""
from alembic import op
import sqlalchemy as sa

revision = '0132_pwa_push'
down_revision = '0131_read_projection_cursor'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('branding_icon_artifacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_image_id', sa.String(36), sa.ForeignKey('branding_images.id'), nullable=True),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('profile', sa.String(40), nullable=False),
        sa.Column('purpose', sa.String(24), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint('source_sha256', 'profile', 'purpose', 'width', 'height'))
    op.create_table('push_installations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('session_id', sa.String(36), nullable=True),
        sa.Column('installation_hint', sa.String(128), nullable=False),
        sa.Column('label', sa.String(80), nullable=False, server_default=''),
        sa.Column('platform', sa.String(40), nullable=False, server_default=''),
        sa.Column('state', sa.String(16), nullable=False, server_default='enabled'),
        sa.Column('state_reason', sa.String(120), nullable=False, server_default=''),
        sa.Column('last_reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('identity_id', 'installation_hint'))
    op.create_index('ix_push_installations_identity_state', 'push_installations', ['identity_id', 'state'])
    op.create_table('push_subscription_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('installation_id', sa.String(36), sa.ForeignKey('push_installations.id'), nullable=False),
        sa.Column('endpoint_ciphertext', sa.Text(), nullable=False),
        sa.Column('endpoint_digest', sa.String(64), nullable=False),
        sa.Column('p256dh_ciphertext', sa.Text(), nullable=False),
        sa.Column('auth_ciphertext', sa.Text(), nullable=False),
        sa.Column('content_encoding', sa.String(16), nullable=False, server_default='aes128gcm'),
        sa.Column('vapid_key_id', sa.String(80), nullable=False, server_default=''),
        sa.Column('browser_expiration_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('state', sa.String(16), nullable=False, server_default='active'),
        sa.Column('revoked_reason', sa.String(120), nullable=False, server_default=''),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_failure_code', sa.String(80), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('installation_id', 'revision'))
    subscriptions = sa.table('push_subscription_versions', sa.column('state', sa.String()))
    op.create_index('ix_push_subscription_endpoint_active', 'push_subscription_versions', ['endpoint_digest'], unique=True,
                    sqlite_where=subscriptions.c.state == 'active', postgresql_where=subscriptions.c.state == 'active')
    op.create_index('ix_push_subscription_installation_active', 'push_subscription_versions', ['installation_id'], unique=True,
                    sqlite_where=subscriptions.c.state == 'active', postgresql_where=subscriptions.c.state == 'active')
    op.create_index('ix_push_subscription_installation_state', 'push_subscription_versions', ['installation_id', 'state'])
    op.create_table('notification_policy_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('notification_policy_versions.id'), nullable=True),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('department_id', 'revision'))
    op.create_index('ix_notification_policy_active', 'notification_policy_versions', ['department_id', 'revision'])
    op.create_table('notification_preference_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('notification_preference_versions.id'), nullable=True),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('department_id', 'identity_id', 'revision'))
    op.create_index('ix_notification_preference_identity', 'notification_preference_versions', ['department_id', 'identity_id', 'revision'])
    op.create_table('push_deliveries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('outbox_id', sa.String(36), sa.ForeignKey('outbox.id'), nullable=False, unique=True),
        sa.Column('source_outbox_id', sa.String(36), sa.ForeignKey('outbox.id'), nullable=False),
        sa.Column('notification_id', sa.String(36), sa.ForeignKey('notifications.id'), nullable=True),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('push_subscription_versions.id'), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('preference_revision', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.String(40), nullable=False),
        sa.Column('detail_cap', sa.String(16), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('service_status', sa.Integer(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_code', sa.String(80), nullable=True),
        sa.UniqueConstraint('notification_id', 'subscription_id'))
    op.create_index('ix_push_deliveries_source_outbox_id', 'push_deliveries', ['source_outbox_id'])
    op.create_index('ix_push_deliveries_notification_id', 'push_deliveries', ['notification_id'])
    op.create_index('ix_push_deliveries_identity_id', 'push_deliveries', ['identity_id'])
    op.create_index('ix_push_deliveries_subscription_id', 'push_deliveries', ['subscription_id'])
    op.create_index('ix_push_deliveries_due', 'push_deliveries', ['department_id', 'expires_at'])

    op.create_table('push_enrollments',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('installation_id', sa.String(36), sa.ForeignKey('push_installations.id'), nullable=False),
        sa.UniqueConstraint('installation_id', 'department_id'))
    op.create_table('pwa_system_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('pwa_system_versions.id'), nullable=True),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False))
    op.create_table('push_transport_leases',
        sa.Column('outbox_id', sa.String(36), sa.ForeignKey('outbox.id'), primary_key=True),
        sa.Column('owner', sa.String(36), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('until', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_push_transport_leases_until', 'push_transport_leases', ['until'])
    op.create_table('push_attempts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivery_id', sa.String(36), sa.ForeignKey('push_deliveries.id'), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('outcome', sa.String(40), nullable=False),
        sa.UniqueConstraint('delivery_id', 'attempt'))
    op.create_index('ix_push_attempts_delivery_id', 'push_attempts', ['delivery_id'])
    op.create_table('push_provider_states',
        sa.Column('origin', sa.String(255), primary_key=True),
        sa.Column('key_id', sa.String(80), primary_key=True),
        sa.Column('authorization_ciphertext', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cooldown_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paused', sa.Boolean(), nullable=False))

    op.create_index('ix_push_installations_department_id', 'push_installations', ['department_id'])
    op.create_index('ix_notification_policy_versions_department_id', 'notification_policy_versions', ['department_id'])
    op.create_index('ix_notification_preference_versions_department_id', 'notification_preference_versions', ['department_id'])
    op.create_index('ix_push_deliveries_department_id', 'push_deliveries', ['department_id'])
    op.create_index('ix_push_enrollments_department_id', 'push_enrollments', ['department_id'])

    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER notification_policy_versions_update_guard BEFORE UPDATE ON notification_policy_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER notification_policy_versions_delete_guard BEFORE DELETE ON notification_policy_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER notification_policy_versions_insert_guard BEFORE INSERT ON notification_policy_versions WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) BEGIN SELECT RAISE(ABORT, 'invalid PWA revision'); END")
        op.execute("CREATE TRIGGER notification_preference_versions_update_guard BEFORE UPDATE ON notification_preference_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER notification_preference_versions_delete_guard BEFORE DELETE ON notification_preference_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER notification_preference_versions_insert_guard BEFORE INSERT ON notification_preference_versions WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) BEGIN SELECT RAISE(ABORT, 'invalid PWA revision'); END")
        op.execute("CREATE TRIGGER pwa_system_versions_update_guard BEFORE UPDATE ON pwa_system_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER pwa_system_versions_delete_guard BEFORE DELETE ON pwa_system_versions BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER pwa_system_versions_insert_guard BEFORE INSERT ON pwa_system_versions WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) BEGIN SELECT RAISE(ABORT, 'invalid PWA revision'); END")
        op.execute("CREATE TRIGGER branding_icon_artifacts_update_guard BEFORE UPDATE ON branding_icon_artifacts BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER branding_icon_artifacts_delete_guard BEFORE DELETE ON branding_icon_artifacts BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER branding_icon_artifacts_insert_guard BEFORE INSERT ON branding_icon_artifacts WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) BEGIN SELECT RAISE(ABORT, 'invalid PWA revision'); END")
        op.execute("CREATE TRIGGER push_attempts_update_guard BEFORE UPDATE ON push_attempts BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER push_attempts_delete_guard BEFORE DELETE ON push_attempts BEGIN SELECT RAISE(ABORT, 'immutable PWA evidence'); END")
        op.execute("CREATE TRIGGER push_attempts_insert_guard BEFORE INSERT ON push_attempts WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) BEGIN SELECT RAISE(ABORT, 'invalid PWA revision'); END")
    else:
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if '$' in schema:
            raise RuntimeError('Schema dollar delimiters are unsupported')
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.pwa_immutable() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable PWA evidence'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1)
              THEN RAISE EXCEPTION 'invalid PWA revision'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER notification_policy_versions_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.notification_policy_versions FOR EACH ROW EXECUTE FUNCTION {q}.pwa_immutable()")
        op.execute(f"CREATE TRIGGER notification_preference_versions_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.notification_preference_versions FOR EACH ROW EXECUTE FUNCTION {q}.pwa_immutable()")
        op.execute(f"CREATE TRIGGER pwa_system_versions_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.pwa_system_versions FOR EACH ROW EXECUTE FUNCTION {q}.pwa_immutable()")
        op.execute(f"CREATE TRIGGER branding_icon_artifacts_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.branding_icon_artifacts FOR EACH ROW EXECUTE FUNCTION {q}.pwa_immutable()")
        op.execute(f"CREATE TRIGGER push_attempts_immutable BEFORE INSERT OR UPDATE OR DELETE ON {q}.push_attempts FOR EACH ROW EXECUTE FUNCTION {q}.pwa_immutable()")


def downgrade():
    raise RuntimeError('Forward-only: retain PWA consent, delivery and branding evidence')
