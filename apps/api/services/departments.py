"""Atomic test-department provisioning through the governed command bus."""
from dataclasses import replace

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import require_permission
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.settings import new_department_session_limits
from apps.api.snapshots import snapshot_hash
from packages.contracts import schemas as s
from .access import live_role_holder, organisation_settings_holders, require_sign_in, sign_in_identities
from .common import history_many
from .independence import person_key, today


def require_test_deployment(settings):
    # DEV deliberately runs strict ROTA_ENV=production with ROTA_DEPLOYMENT=dev.
    allowed = settings.deployment == 'dev' or (
        settings.environment in {'development', 'test'} and settings.deployment in {'development', 'test'})
    require(allowed, 'TEST_DEPLOYMENT_REQUIRED', 'Test departments can only be created on DEV or test deployments', 403)


def require_test_administrator(identity):
    name = identity.display_name.strip() if identity is not None else ''
    require(name.endswith('(test)') and bool(name[:-6].strip()), 'FIRST_ADMIN_NOT_TEST',
            'The first administrator display name must include a name ending with (test)', 409)


def prepare(db, actor, command, settings):
    require_permission(actor, 'department.create')
    require_test_deployment(settings)
    value = s.DepartmentCreateInput.model_validate(command.payload)
    require(command.target_id is None, 'DEPARTMENT_CREATE_TARGET', 'Create a department without a target')
    require(person_key(db, value.first_admin_identity_id) != person_key(db, actor.identity_id), 'DIFFERENT_FIRST_ADMIN',
            'Choose a different person as the first administrator', 409)
    identity = db.scalar(select(m.Identity).where(
        m.Identity.id == value.first_admin_identity_id, sign_in_identities(actor, settings)))
    require(identity is not None and not identity.disabled, 'NOT_FOUND', 'Active sign-in not found', 404)
    require_sign_in(identity)
    require_test_administrator(identity)
    account = db.scalar(select(m.Account).where(m.Account.identity_id == identity.id))
    require(account is None or account.accepted_at is not None, 'FIRST_ADMIN_NOT_ACTIVE',
            'The first administrator must accept their existing invitation before department creation', 409)
    departments = list(db.scalars(select(m.Department).where(
        m.Department.organisation_id == actor.organisation_id,
        func.lower(func.trim(m.Department.name)) == func.lower(func.trim(value.name)))))
    require(len(departments) <= 1, 'DEPARTMENT_NAME_CONFLICT', 'More than one department already has this name', 409)
    department, binding = None, None
    if departments:
        department = departments[0]
        binding = next((row for row in db.scalars(select(m.RoleBinding).where(
            m.RoleBinding.department_id == department.id, m.RoleBinding.identity_id == identity.id,
            m.RoleBinding.role == 'admin', m.RoleBinding.ends_on.is_(None)))
            if live_role_holder(db, row, today(db, department.id))), None)
        require(binding is not None, 'FIRST_ADMIN_CONFLICT',
                'This department exists with different administrator access; manage its access separately', 409)
    holders = organisation_settings_holders(db, actor.organisation_id) if department is None else {}
    review = {'preview': True, 'kind': 'department.create', 'would_create': department is None,
        'name': department.name if department is not None else value.name,
        'department_id': department.id if department is not None else None, 'first_admin_identity_id': identity.id,
        'org_settings_holders_lost': [{'identity_id': ident, 'display_name': holders[ident]}
                                     for ident in sorted(holders) if ident != identity.id]}
    review['expected_fingerprint'] = snapshot_hash({'contract': 'department-create-v1',
        'actor': [actor.identity_id, actor.department_id],
        'change': command.model_dump(mode='json', exclude={'expected_fingerprint'}),
        'organisation_revision': db.get(m.Organisation, actor.organisation_id).revision,
        'first_admin': [identity.id, identity.revision, identity.display_name],
        'existing_binding': [binding.id, binding.revision] if binding is not None else None, 'review': review})
    return value, identity, department, binding, s.DepartmentCreatePreview.model_validate(review).model_dump(mode='json')


def preview(db, actor, command, settings):
    return prepare(db, actor, command, settings)[-1]


def create(db, actor, command, settings):
    value, identity, department, binding, review = prepare(db, actor, command, settings)
    if department is not None:
        return {'department': record(department), 'first_admin_binding': record(binding), 'created': False}
    require(command.expected_fingerprint is not None, 'PREVIEW_REQUIRED',
            'Preview who will lose organisation-wide settings authority before confirming', 428)
    require(command.expected_fingerprint == review['expected_fingerprint'], 'STALE_PREVIEW',
            'The department, administrator or organisation access changed; preview again before confirming', 409)
    # The command bus serializes writers before this name check; all steps commit together or roll back.
    department = m.Department(organisation_id=actor.organisation_id, name=value.name,
        generation_defaults=s.GenerationOptions().model_dump(mode='json'),
        wish_settings=s.WishSettings().model_dump(mode='json'), bulk_settings=s.BulkSettings().model_dump(mode='json'),
        **new_department_session_limits())
    db.add(department)
    db.flush()
    binding = m.RoleBinding(department_id=department.id, identity_id=identity.id, role='admin',
                            starts_on=today(db, department.id))
    db.add(binding)
    department_actor = replace(actor, department_id=department.id)
    history_many(db, department_actor, [('department_settings', department, value.reason), ('access', binding, value.reason)])
    # All department catalogue stamps include the organisation revision; roles themselves are re-read per request.
    db.get(m.Organisation, actor.organisation_id).revision += 1
    for action, row, detail in (
        ('department.create', department, {'target_label': department.name, 'first_admin_binding_id': binding.id}),
        ('role.grant', binding, {'target_label': identity.display_name, 'roles': [binding.role]}),
    ):
        db.add(m.AuditEvent(department_id=department.id, actor_id=actor.identity_id, action=action, target_id=row.id,
            reason=value.reason, detail={**detail, 'receipt_id': db.info['receipt_id']}, correlation_id=db.info['receipt_id']))
    return {'department': record(department), 'first_admin_binding': record(binding), 'created': True}
