"""One population predicate and transient existing-row protection for every generation path."""
from .conditions import scope_matches_throughout, tag_scope
from .time import aware


def selected_session_ids(data):
    """Missing (or SQL-projected null) selection uses focus; an explicit empty list selects nothing."""
    selected = data.get('generation_session_ids')
    return data.get('focus_ids', []) if selected is None else selected


def population_allows(data, shift, person_id, participant=None):
    population = data.get('planning_population')
    if population is not None and person_id not in population.get(shift['id'], ()):
        return False
    scope = data.get('generation_scope')
    if scope is None:
        return True
    interval = participant if participant is not None else shift
    return person_id in data['people'] and scope_matches_throughout(
        data, tag_scope(data, {'conditions': scope['conditions']}), person_id, shift,
        aware(interval['start']), aware(interval['end']))


def run_population_people(data):
    """Resolve a pass audience using the same dated predicate as candidate admission."""
    if data.get('planning_population') is None and data.get('generation_scope') is None:
        return None
    selected = set(selected_session_ids(data))
    people = set()
    for shift in data['shifts']:
        if shift['id'] in selected:
            people.update(pid for pid in data['people'] if pid not in people and population_allows(data, shift, pid))
    return people


def union_population(data, receipts):
    """Union applied pass audiences; an unrestricted pass absorbs every restriction.

    Receipts contain only canonical population inputs, never candidate assignments.
    An absent union means no pass has yet defined this rota's audience.
    """
    people = set(data.get('applied_planning_population') or ())
    if 'applied_planning_population' in data and data['applied_planning_population'] is None:
        return None
    for receipt in receipts:
        context = {**data, **{key: receipt.get(key) for key in (
            'generation_scope', 'planning_population', 'generation_session_ids')}}
        population = (receipt['population_people'] if receipt.get('population_recorded')
                      else run_population_people(context))
        if population is None:
            return None
        people.update(population)
    return sorted(people)


def planning_population_people(data):
    """One quota/fairness audience for the current run plus all applied passes."""
    if data.get('generation_scope') is not None or data.get('planning_population') is not None:
        people = union_population(data, [data])
    elif 'applied_planning_population' in data:
        people = data['applied_planning_population']
    else:
        return None
    return None if people is None else set(people)


def population_info(data):
    people = planning_population_people(data)
    if people is None:
        return None
    workspace = data['workspace']
    members = {m['person_id'] for m in data['memberships'] if m['department_id'] == workspace['department_id']
               and str(m['starts_on']) < str(workspace['ends_on'])
               and (not m.get('ends_on') or str(m['ends_on']) > str(workspace['starts_on']))}
    return {'inside_count': len(members & people), 'outside_count': len(members - people)}


def protects_existing(data, shift, participant):
    if participant.get('_witness_fixed'):
        return True
    witness = data.get('_group_witness_population')
    if witness is not None and participant['person_id'] not in witness.get(shift['id'], ()):
        return True
    scope = data.get('generation_scope')
    return scope is not None and (scope['protection'] == 'keep_existing'
        or not population_allows(data, shift, participant['person_id'], participant))


def has_population(data):
    selected = set(selected_session_ids(data))
    return any(population_allows(data, shift, pid) for shift in data['shifts']
               if shift['id'] in selected and not shift.get('cancelled') for pid in data['people'])


def scope_receipt(data):
    # Old workers omit this acknowledgement: the API refuses their scoped proposals.
    return {key: data[key] for key in ('generation_scope', 'generation_scope_source', 'planning_population', 'planning_group_version_id', 'planning_context') if key in data}
