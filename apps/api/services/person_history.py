"""SQL subject filtering for direct person-record audit history; actors are a separate filter."""
from sqlalchemy import case, cast, func, literal, or_, select, union
from sqlalchemy.dialects.postgresql import JSONB

from apps.api import models as m
from apps.api.auth import person_access


def for_person(db, actor, query, person_id):
    person_access(db, actor, person_id)
    identities = select(m.Identity.id).where(m.Identity.person_id == person_id).correlate(None)
    targets = [select(literal(person_id).label('id')), identities]
    for cls in (m.Membership, m.Eligibility, m.CapabilityGrant, m.ProgrammeMembership, m.Rotation,
                m.Mentorship, m.Availability, m.RequestRecord, m.AvailabilityPattern, m.StaffWish,
                m.PolicyException, m.CalendarFeed, m.TraineeNote):
        who = cls.person_id == person_id
        if cls is m.Mentorship:
            who = or_(who, cls.mentor_id == person_id)
        targets.append(select(cls.id).where(cls.department_id == actor.department_id, who))
    targets.append(select(m.SwapRequest.id).where(m.SwapRequest.department_id == actor.department_id,
        or_(m.SwapRequest.proposer_person_id == person_id, m.SwapRequest.recipient_person_id == person_id)))
    targets.append(select(m.RoleBinding.id).where(m.RoleBinding.department_id == actor.department_id,
                                                m.RoleBinding.identity_id.in_(identities)))
    # Undo can remove a newly created dated fact. Its history snapshot still binds its ID to the person.
    H = m.DefinitionHistory
    targets.append(select(H.object_id).where(H.department_id == actor.department_id,
        or_(H.snapshot['person_id'].as_string() == person_id, H.snapshot['mentor_id'].as_string() == person_id)))
    ids = select(union(*targets).cte('person_record_ids').c.id)
    E, R = m.AuditEvent, m.CommandReceipt

    def array_has_subject(key, objects):
        value = R.result[key]
        if db.get_bind().dialect.name == 'postgresql':
            value = cast(value, JSONB)
            value = case((func.jsonb_typeof(value) == 'array', value), else_=cast(literal('[]'), JSONB))
            items = func.jsonb_array_elements(value).table_valued('value').alias('subject_' + key)
            item = cast(items.c.value, JSONB)
            target = item['id'].as_string() if objects else item[()].as_string()
        else:
            items = func.json_each(value).table_valued('value', 'type').alias('subject_' + key)
            target = case((items.c.type == 'object', func.json_extract(items.c.value, '$.id'))) if objects else items.c.value
        return select(literal(1)).select_from(items).where(target.in_(ids)).correlate(R).exists()

    # Legacy creates often have no event target. Read ONLY result IDs in SQL, never return receipt payloads.
    created = [R.result[key]['id'].as_string().in_(ids) for key in ('record', 'pattern', 'wish', 'swap')]
    created += [array_has_subject(key, objects) for key, objects in (
        ('records', True), ('created', True), ('created_ids', False), ('ended', True), ('removed_ids', False),
        ('added', True), ('removed', True))]
    receipt = select(R.id).where(R.department_id == actor.department_id,
        R.id == E.detail['receipt_id'].as_string(), or_(*created)).correlate(E).exists()
    return query.where(or_(E.target_id.in_(ids), receipt))
