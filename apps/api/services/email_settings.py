"""Audited department email configuration; credentials stay in deployment secrets."""
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.settings import secret_from_env
from packages.contracts import schemas as s
from .common import expected


def status(db, actor, settings):
    require_role(actor, 'admin')
    department = db.get(m.Department, actor.department_id)
    config = s.EmailSettings.model_validate(department.email_settings)
    def available(name):
        try:
            return bool(secret_from_env(name))
        except RuntimeError:
            return False
    return {'value': config.model_dump(mode='json'), 'revision': department.revision,
            'smtp_password_configured': available('FMUC_SMTP_PASSWORD'),
            'm365_client_secret_configured': available('FMUC_M365_CLIENT_SECRET'),
            'data_key_configured': bool(settings.fmuc_data_key)}


def save(db, actor, command):
    require_role(actor, 'admin')
    require(command.target_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    value = s.EmailSettingsInput.model_validate(command.payload)
    department = db.get(m.Department, actor.department_id)
    expected(department, command.expected_revision)
    previous = s.EmailSettings.model_validate(department.email_settings).model_dump(mode='json')
    department.email_settings = value.value.model_dump(mode='json')
    department.revision += 1
    return {'value': department.email_settings, 'revision': department.revision, 'previous_value': previous}
