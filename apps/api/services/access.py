from sqlalchemy import and_, or_, select
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.auth import _live, require_role, person_access
from apps.api.errors import require
from apps.api.serialization import record, generation_defaults_record
from .common import scoped, expected, history, range_overlaps, bump_inputs
from .bulk_settings import limits as bulk_limits
from .events import invalidate_audience_inputs


SWAP_AUTOMATION_ISSUER = "fmuc:swap-automation"


def require_sign_in(identity):
    from scripts.system_command import MAINTENANCE_ISSUER
    system = {SWAP_AUTOMATION_ISSUER: "Department swap automation", MAINTENANCE_ISSUER: "System maintenance"}
    require(identity.issuer not in system, "AUTOMATION_IDENTITY",
            f"{system.get(identity.issuer, 'This identity')} is not a sign-in account", 409)


def identity_for_change(db, actor, target_id):
    """The shared sign-in change boundary, before exposing a target's revision or values."""
    require_role(actor, "admin")
    row = db.get(m.Identity, target_id)
    require(row is not None, "NOT_FOUND", "Identity not found", 404)
    require_sign_in(row)
    bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == target_id)))
    require(bindings and all(b.department_id == actor.department_id for b in bindings),
            "OPERATOR_REQUIRED", "Cross-department sign-in changes require the institutional operator", 403)
    return row


def live_role_holder(db, binding, today):
    """A current, enabled holder can sign in only after accepting a managed account."""
    if not _live(binding, today) or db.get(m.Identity, binding.identity_id).disabled:
        return False
    account = db.scalar(select(m.Account).where(m.Account.identity_id == binding.identity_id))
    return account is None or account.accepted_at is not None


def other_role_holders(db, department_id, identity_ids, role):
    """Keep a live, open-ended holder outside the sign-ins losing access."""
    from .independence import today as local_today
    today = local_today(db, department_id)
    return any(b.identity_id not in identity_ids and b.ends_on is None and live_role_holder(db, b, today)
               for b in db.scalars(select(m.RoleBinding).where(m.RoleBinding.department_id == department_id, m.RoleBinding.role == role)))


def other_admins(db, department_id, identity_id):
    return other_role_holders(db, department_id, {identity_id}, "admin")


def keep_developer(db, identity_ids, department_id=None):
    """Disabling sign-ins must leave Developer cover in every affected department."""
    from .independence import today as local_today
    query = select(m.RoleBinding).where(
        m.RoleBinding.identity_id.in_(identity_ids), m.RoleBinding.role.in_(s.SYSTEM_ROLES))
    if department_id is not None:
        query = query.where(m.RoleBinding.department_id == department_id)
    for binding in db.scalars(query):
        if live_role_holder(db, binding, local_today(db, binding.department_id)):
            require(other_role_holders(db, binding.department_id, identity_ids, binding.role), "LAST_DEVELOPER",
                    "Keep at least one Developer: give someone else the Developer role first", 409)


def organisation_identities(actor):
    """Sign-ins an administrator may see and grant: those of this organisation's people, or with no person yet,
    and never one that already has access in another organisation."""
    departments = select(m.Department.id).where(m.Department.organisation_id == actor.organisation_id)
    people = select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id)
    return and_(m.Identity.id.not_in(select(m.RoleBinding.identity_id).where(m.RoleBinding.department_id.not_in(departments))),
                or_(m.Identity.person_id.is_(None), m.Identity.person_id.in_(people)))


def sign_in_identities(actor, settings):
    """Access and bulk pickers expose the same managed sign-ins, including unbound accounts."""
    from scripts.system_command import MAINTENANCE_ISSUER
    return and_(organisation_identities(actor),
                or_(m.Identity.issuer.in_(["local-development", settings.oidc_issuer,
                    select(m.AuthConfiguration.data["issuer"].as_string()).where(
                        m.AuthConfiguration.id == "environment").scalar_subquery()]),
                    m.Identity.id.in_(select(m.Account.identity_id)),
                    m.Identity.id.in_(select(m.RoleBinding.identity_id))),
                m.Identity.issuer.not_in([SWAP_AUTOMATION_ISSUER, MAINTENANCE_ISSUER]))


def organisation_settings_holders(db, organisation_id):
    """Enabled sign-ins with current admin bindings in every department, in one batch."""
    from .independence import today
    departments = set(db.scalars(select(m.Department.id).where(m.Department.organisation_id == organisation_id)))
    if not departments:
        return {}
    day = today(db, next(iter(departments)))
    grants, names = {}, {}
    rows = db.execute(select(m.RoleBinding, m.Identity.id, m.Identity.display_name).join(
        m.Identity, m.Identity.id == m.RoleBinding.identity_id).where(
        m.RoleBinding.department_id.in_(departments), m.RoleBinding.role == 'admin', m.Identity.disabled.is_(False)))
    for binding, identity_id, name in rows:
        if _live(binding, day):
            grants.setdefault(identity_id, set()).add(binding.department_id)
            names[identity_id] = name
    return {identity_id: names[identity_id] for identity_id, held in grants.items() if held == departments}


def display_settings(db, actor, command):
    from .display_settings import change
    return change(db, actor, command)


def department_settings(db, actor, command, settings):
    require_role(actor, 'admin')
    require(command.target_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    value = s.DepartmentSettingsInput.model_validate(command.payload)
    require(value.generation_defaults is None or value.generation_defaults.weight_preset == 'department_defaults',
            'INVALID_OPTIONS', 'Named weight presets apply only to individual runs; save department defaults with '
            'weight_preset set to department_defaults')
    department = db.scalar(select(m.Department).where(m.Department.id == actor.department_id).with_for_update())
    organisation = db.get(m.Organisation, actor.organisation_id)
    expected(department, value.department_revision)
    expected(organisation, command.expected_revision)
    calendar_id = (value.operating_calendar_id if 'operating_calendar_id' in value.model_fields_set
                   else department.operating_calendar_id)
    changed_calendar = calendar_id != department.operating_calendar_id
    if changed_calendar and calendar_id is not None:
        calendar = scoped(db, m.OperatingCalendar, calendar_id, actor)
        require(calendar.active, 'CALENDAR_INACTIVE', 'Choose an active department operating calendar', 409)
    changed_org = any(getattr(organisation, key) != val for key, val in
        {'name': value.organisation_name, 'timezone': value.timezone, 'week_start': value.week_start}.items())
    stored_defaults = s.GenerationOptions.model_validate(generation_defaults_record(department.generation_defaults) or {})
    from . import score_settings
    previous_score_definition = score_settings.definition_from_options(stored_defaults.model_dump(mode='json'))
    from .jobs import merged
    generation_defaults = stored_defaults.model_dump(mode='json', exclude_unset=True)
    if value.generation_defaults is not None:
        generation_defaults = s.GenerationOptions.model_validate(merged(generation_defaults,
            value.generation_defaults.model_dump(mode='json', exclude_unset=True))).model_dump(mode='json')
    wish_settings = (value.wish_settings.model_dump(mode='json') if value.wish_settings is not None
                     else department.wish_settings)
    from .wishes import require_default_points, require_supported_approval
    if value.wish_settings is not None:
        for preference in value.wish_settings.types.values():
            require_supported_approval(preference)
    budget = s.GenerationOptions.model_validate(generation_defaults).scoring.wish_budget_points
    for kind, preference in s.WishSettings.model_validate(wish_settings or {}).types.items():
        require_default_points(kind, preference.default_points, budget)
    if value.inherit_generation_mode:
        generation_defaults.pop('mode', None)
    elif value.generation_defaults is not None and 'mode' not in value.generation_defaults.model_fields_set:
        # Saving other defaults must neither change a saved choice nor pin an inherited assignment mode.
        current_defaults = department.generation_defaults or {}
        if 'mode' in current_defaults:
            generation_defaults['mode'] = current_defaults['mode']
        else:
            generation_defaults.pop('mode', None)
    resolved_defaults = s.GenerationOptions.model_validate({**s.GenerationOptions().model_dump(), **generation_defaults})
    changed_generation = department.generation_defaults != generation_defaults
    changed_score_settings = previous_score_definition != score_settings.definition_from_options(generation_defaults)
    changed_department = department.name != value.department_name or department.wish_settings != wish_settings
    changed_inputs = (changed_department or changed_calendar
                      or ('mode' in stored_defaults.model_fields_set) != ('mode' in generation_defaults)
                      or stored_defaults.model_dump(mode='json', exclude=s.GENERATION_RUNTIME_OPTIONS) !=
                      resolved_defaults.model_dump(mode='json', exclude=s.GENERATION_RUNTIME_OPTIONS))
    weekend = value.weekend_days if value.weekend_days is not None else department.weekend_days
    changed_weekend = department.weekend_days != weekend
    changed_inputs |= changed_weekend and calendar_id is None
    # How leave shows on findings is applied when they are read (leave_disclosure.py): no rota input changes either.
    leave = value.leave_reason_display or department.leave_reason_display
    swap_kinds = value.swap_kinds_allowed if value.swap_kinds_allowed is not None else department.swap_kinds_allowed
    swap_open = value.swap_open_offers if value.swap_open_offers is not None else department.swap_open_offers
    changed_display = (changed_weekend or department.leave_reason_display != leave
                       or department.swap_kinds_allowed != swap_kinds or department.swap_open_offers != swap_open)
    announcement_settings = {key: getattr(value, key) for key in (
        'announcement_send_time', 'announcement_auto_reminder_days', 'announcement_reminder_interval_hours')
        if key in value.model_fields_set}
    require(all(val is not None or key == 'announcement_auto_reminder_days' for key, val in announcement_settings.items()),
            'VALUE_INVALID', 'Send time and reminder interval cannot be empty')
    changed_announcements = any(getattr(department, key) != val for key, val in announcement_settings.items())
    event_settings = {key: getattr(value, key) for key in ('event_horizon_days', 'event_receipt_window_days')
                      if key in value.model_fields_set}
    require(all(val is not None for val in event_settings.values()), 'VALUE_INVALID', 'Event windows cannot be empty')
    changed_events = any(getattr(department, key) != val for key, val in event_settings.items())
    swap_settings = {key: getattr(value, key) if key in value.model_fields_set else getattr(department, key)
                     for key in ('swap_review_route_id', 'swap_auto_prepare', 'swap_auto_publish')}
    swap_settings.update({key: getattr(value, key) if getattr(value, key) is not None else getattr(department, key)
                          for key in ('swap_offer_window_days', 'swap_open_offer_cap')})
    if 'swap_review_route_id' in value.model_fields_set and value.swap_review_route_id is not None:
        route = scoped(db, m.ReviewRoute, value.swap_review_route_id, actor)
        require(route.purpose == 'swap', 'ROUTE_PURPOSE', 'Choose a swap review route for swaps', 409)
        if value.swap_review_route_id != department.swap_review_route_id:
            require(route.status == 'active', 'ROUTE_STATE', 'Choose an active review route for swaps', 409)
    changed_swaps = any(getattr(department, key) != val for key, val in swap_settings.items())
    bulk_settings = value.bulk_settings.model_dump() if value.bulk_settings is not None else department.bulk_settings
    changed_bulk = department.bulk_settings != bulk_settings
    policy_approvals = (value.policy_retire_approvals_required if value.policy_retire_approvals_required is not None
                        else department.policy_retire_approvals_required)
    changed_policy_approvals = policy_approvals != department.policy_retire_approvals_required
    display = department.display_settings or {}
    controls = (value.generation_controls.model_dump(mode='json') if value.generation_controls is not None
                else display.get('generation_controls'))
    changed_controls = display.get('generation_controls') != controls
    animation = (value.autoschedule_animation.model_dump(mode='json') if value.autoschedule_animation is not None
                 else display.get('autoschedule_animation'))
    changed_animation = display.get('autoschedule_animation') != animation
    score_check_enabled = (value.score_check_enabled if value.score_check_enabled is not None
                           else department.score_check_enabled)
    changed_score_check = department.score_check_enabled != score_check_enabled
    if changed_score_check and score_check_enabled:
        require_role(actor, *s.PERMISSIONS['access.system_roles'])
    # What Undo puts back (the client sends it with the new revisions).
    previous = {'department_name': department.name, 'organisation_name': organisation.name, 'timezone': organisation.timezone,
                'score_check_enabled': department.score_check_enabled,
                'week_start': organisation.week_start, 'weekend_days': department.weekend_days,
                'operating_calendar_id': department.operating_calendar_id,
                'leave_reason_display': department.leave_reason_display,
                'swap_kinds_allowed': department.swap_kinds_allowed,
                'swap_open_offers': department.swap_open_offers,
                'announcement_send_time': department.announcement_send_time,
                'announcement_auto_reminder_days': department.announcement_auto_reminder_days,
                'announcement_reminder_interval_hours': department.announcement_reminder_interval_hours,
                'event_horizon_days': department.event_horizon_days,
                'event_receipt_window_days': department.event_receipt_window_days,
                'policy_retire_approvals_required': department.policy_retire_approvals_required,
                **{key: getattr(department, key) for key in swap_settings}}
    if value.generation_defaults is not None or changed_generation:
        previous['generation_defaults'] = stored_defaults.model_dump(mode='json')
        if 'mode' not in stored_defaults.model_fields_set:
            previous['generation_defaults'].pop('mode')
            previous['inherit_generation_mode'] = True
    if value.bulk_settings is not None:
        previous['bulk_settings'] = bulk_limits(db, actor, settings)
    if value.wish_settings is not None:
        previous['wish_settings'] = s.WishSettings.model_validate(department.wish_settings or {}).model_dump(mode='json')
    if value.autoschedule_animation is not None:
        previous['autoschedule_animation'] = s.AutoscheduleAnimationSettings.model_validate(
            display.get('autoschedule_animation') or {}).model_dump(mode='json')
    if value.generation_controls is not None:
        previous['generation_controls'] = s.GenerationControls.model_validate(
            display.get('generation_controls') or {}).model_dump(mode='json')
    if changed_org:
        require(actor.identity_id in organisation_settings_holders(db, organisation.id),
                'ORGANISATION_ADMIN_REQUIRED', 'Organisation-wide settings require admin access to every affected department', 403)
        organisation.name, organisation.timezone, organisation.week_start = value.organisation_name, value.timezone, value.week_start
        organisation.revision += 1
    if changed_department or changed_calendar or changed_generation or changed_display or changed_announcements or changed_events or changed_swaps or changed_bulk or changed_policy_approvals or changed_animation or changed_controls or changed_score_check:
        department.score_check_enabled = score_check_enabled
        department.name = value.department_name
        if changed_score_settings:
            score_settings.sync_projection(db, actor, department, generation_defaults, value.reason)
        department.generation_defaults = generation_defaults
        department.wish_settings = wish_settings
        department.weekend_days = weekend
        department.operating_calendar_id = calendar_id
        department.leave_reason_display = leave
        department.bulk_settings = bulk_settings
        department.policy_retire_approvals_required = policy_approvals
        if changed_animation or changed_controls:
            from .display_settings import append
            append(db, actor, department, {**display,
                **({'autoschedule_animation': animation} if changed_animation else {}),
                **({'generation_controls': controls} if changed_controls else {})}, value.reason)
        department.swap_kinds_allowed = swap_kinds
        department.swap_open_offers = swap_open
        for key, val in (announcement_settings | event_settings | swap_settings).items():
            setattr(department, key, val)
        department.revision += 1
        history(db, actor, 'department_settings', department, value.reason)
    if changed_org or changed_inputs:
        bump_inputs(db, actor)
    if changed_org:
        history(db, actor, 'organisation_settings', organisation, value.reason)
    if changed_events or changed_org:
        department.events_checked_on = None
    return {'organisation': record(organisation), 'department': record(department),
            'previous': {**previous, 'department_revision': department.revision}}


def require_role_change(db, actor, role, *, grant=False):
    if role not in s.EXPLICIT_ROLES:
        require_role(actor, "admin")
        return
    if actor.permits(*s.PERMISSIONS["access.system_roles"]):
        return
    action = "grant" if grant else "revoke"
    require(False, "SYSTEM_ROLE_" + action.upper(),
            f"Only a Developer can {action} {s.ROLE_LABELS[role]} access"
            + "; ask the system owner to recover missing Developer access", 403)


def access_command(db, actor, command, settings):
    if not command.kind.startswith("role."):
        require_role(actor, "admin")
    if command.kind == "identity.bind":
        value = s.IdentityInput.model_validate(command.payload)
        require_sign_in(value)
        from .auth_configuration import current
        issuer = current(db, settings).issuer
        require(value.issuer == issuer and bool(issuer),
                "ISSUER_MISMATCH", "Identity binding must reference the configured institutional issuer")
        if value.person_id:
            person_access(db, actor, value.person_id)
        require(db.scalar(select(m.Identity).where(m.Identity.issuer == value.issuer, m.Identity.subject == value.subject)) is None,
                "IDENTITY_EXISTS", "This sign-in already exists. If it was disabled, use Enable sign-in on the Access page", 409)
        row = m.Identity(**value.model_dump(exclude={"reason"}))
        db.add(row)
        db.flush()
        return {"record": record(row, exclude=("password_hash",))}
    if command.kind in {"role.grant", "role.restore"}:
        restoring = command.kind == "role.restore"
        if restoring:
            decision = s.Decision.model_validate(command.payload)
            row = scoped(db, m.RoleBinding, command.target_id, actor)
            require_role_change(db, actor, row.role, grant=True)
            expected(row, command.expected_revision)
            require(row.revoked, "WORKFLOW_STATE", "Only a revoked access binding can be restored", 409)
        value = s.RoleInput.model_validate({**{key: getattr(row, key) for key in s.RoleInput.model_fields if key != "reason"},
                                           "reason": decision.reason} if restoring else command.payload)
        require_role_change(db, actor, value.role, grant=True)
        identity = db.scalar(select(m.Identity).where(m.Identity.id == value.identity_id, organisation_identities(actor)))
        require(identity is not None, "NOT_FOUND", "Identity not found", 404)
        require_sign_in(identity)
        require(not identity.disabled, "NOT_FOUND", "Identity not found", 404)
        existing = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity.id,
                   m.RoleBinding.department_id == actor.department_id, m.RoleBinding.role == value.role)))
        for old in existing:
            if old.revoked:
                continue
            require(not range_overlaps(old.starts_on, old.ends_on, value.starts_on, value.ends_on),
                    "DUPLICATE_ROLE", "An overlapping access binding already exists", 409)
        if restoring:
            row.revoked = False
            row.revision += 1
        else:
            row = m.RoleBinding(department_id=actor.department_id, **value.model_dump(exclude={"reason"}))
            db.add(row)
            db.flush()
        if value.role in s.EXPLICIT_ROLES:
            from .accounts import open_links
            for link in open_links(db, identity.id):
                link.revoked_at, link.revision = m.now(), link.revision + 1
        history(db, actor, "access", row, value.reason)
        invalidate_audience_inputs(db, actor)
        return {"record": record(row)}
    if command.kind == "role.revoke":
        value = s.Decision.model_validate(command.payload)
        row = scoped(db, m.RoleBinding, command.target_id, actor)
        require_role_change(db, actor, row.role)
        expected(row, command.expected_revision)
        require(not row.revoked, "WORKFLOW_STATE", "This access binding has already been revoked", 409)
        if row.role == "admin":
            require(other_admins(db, actor.department_id, row.identity_id), "LAST_ADMIN",
                    "Keep at least one admin: give someone else the Admin role first", 409)
        if row.role in s.SYSTEM_ROLES:
            from .independence import today as local_today
            if live_role_holder(db, row, local_today(db, actor.department_id)):
                keep_developer(db, {row.identity_id}, actor.department_id)
        row.revoked = True
        row.revision += 1
        from .auth_configuration import protect_break_glass
        protect_break_glass(db, settings)
        history(db, actor, "access", row, value.reason)
        invalidate_audience_inputs(db, actor)
        return {"record": record(row)}
    if command.kind in {"identity.disable", "identity.enable"}:
        s.Decision.model_validate(command.payload)  # the reason is kept in the audit log
        row = identity_for_change(db, actor, command.target_id)
        enable = command.kind == "identity.enable"
        if not enable:
            require(other_admins(db, actor.department_id, command.target_id), "LAST_ADMIN",
                    "Keep at least one admin: give someone else the Admin role first", 409)
        expected(row, command.expected_revision)
        require(row.disabled == enable, "IDENTITY_STATE", "Sign-in is already " + ("enabled" if enable else "disabled"), 409)
        if not enable:
            keep_developer(db, {row.id})
        row.disabled = not enable
        row.revision += 1
        if not enable:
            from .auth_configuration import protect_break_glass
            protect_break_glass(db, settings)
        # A managed account follows in the sign-in service (an invitation not yet accepted stays disabled there).
        account = db.scalar(select(m.Account).where(m.Account.identity_id == row.id))
        from apps.api.identity_store import store_for
        users = store_for(settings)
        require(not (db.info.get("bulk") and account and users and (not enable or account.accepted_at)),
                "BULK_EXTERNAL_SIDE_EFFECT", "Change this managed sign-in individually through Access", 409)
        if account and users and (not enable or account.accepted_at):
            from apps.api.identity_store import IdentityStoreError
            try:
                users.set_disabled(account.username, not enable)
            except IdentityStoreError as exc:
                # Disabling always succeeds here: the app already refuses this identity on every request.
                require(not enable, "SIGN_IN_SERVICE_UNAVAILABLE", str(exc), 503)
        invalidate_audience_inputs(db, actor)
        return {"record": record(row, exclude=("password_hash",))}
    require(False, "UNKNOWN_COMMAND", "Unknown access command")
