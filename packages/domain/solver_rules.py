"""Solver definition checks shared by proposal saving and generation.

These checks establish compatibility, not feasibility of a particular rota.
The public field catalogue comes from the existing canonical schemas.
"""
from fractions import Fraction
import json

from pydantic import ValidationError
from packages.contracts.schemas import PolicyDefinition, GenerationOptions, GENERATION_SCOPE_CAPABILITY
from .rule_serialization import stored_definition

# Bump when translation semantics change; receipts and proposals bind this version.
# 33: bind weighted scoring, scoped fairness and whole-run search budgets to proposals.
# 35: bind shared working-day quota amounts to generated proposals.
# 36: preserve mandatory quota minima and exclude conflicted supervision from proposals.
# 38: retain supported probing to avoid false infeasibility in weighted fairness models.
COMPILER_VERSION = 'cp-sat-40'
PLANNING_LIMITS = {'max_sessions': 1500, 'max_people': 500, 'max_period_days': 31}


def session_span_days(shifts):
    """Days covered by the sessions being scheduled (a long rota is scheduled a month at a time)."""
    from datetime import datetime
    if not shifts:
        return 0
    first = min(datetime.fromisoformat(str(s['start'])) for s in shifts)
    last = max(datetime.fromisoformat(str(s['end'])) for s in shifts)
    return (last - first).total_seconds() / 86400
SUPPORTED_RULES = frozenset({
    'team_lead_requirement', 'restriction', 'min_rest', 'minimum_cover', 'maximum_participants',
    'supervision_limit', 'spread_evenly', 'max_hours', 'max_duties', 'block_quota',
    'forbidden_transition', 'max_consecutive_days', 'travel_buffer',
    'max_consecutive_weekends', 'allocation_overtime', 'allocation_preference', 'balance_goal',
    'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'together_apart',
    'catalogue',  # its lines are the kinds above (packages/domain/rule_lines.py), each checked here
})


def definition_issues(definition, *, historical=False):
    """Return only field/code/message keys; compatibility adds rule identity before 422 and capped storage."""
    raw = definition if isinstance(definition, PolicyDefinition) and not historical else stored_definition(definition)
    rule = raw.rule if isinstance(raw, PolicyDefinition) else raw.get('rule') if isinstance(raw, dict) else None
    kind = rule.get('kind') if isinstance(rule, dict) else getattr(rule, 'kind', None)
    if isinstance(kind, str) and kind not in SUPPORTED_RULES:
        return [{'field': 'definition.rule.kind', 'code': 'UNSUPPORTED_SOLVER_RULE',
                 'message': 'Automatic scheduling does not support this rule.'}]
    try:
        if historical:
            from .rule_lines import _valid
            raw = _valid(json.dumps(raw, sort_keys=True, default=str), stored_goal=False,
                         resolved_catalogue_line=False)
        else:
            raw = PolicyDefinition.model_validate(raw, context={'historical': False}).model_dump(mode='json')
    except ValidationError as error:
        return [{'field': '.'.join(['definition', *map(str, e['loc'])]),
                 'code': 'INVALID_RULE_DEFINITION', 'message': e['msg']}
                for e in error.errors(include_input=False, include_url=False)]
    if raw['rule']['kind'] == 'catalogue':
        from .rule_lines import expand, issues
        found = issues(raw)
        if found:
            return found
        # Each copy was built through the old schemas: only the solver's precision is left to check.
        return [{**issue, 'field': f"definition.rule.lines.{c['template_line']['line']}" + issue['field'].removeprefix('definition')}
                for c in expand({'id': '', 'definition': {**raw, 'rule': {**raw['rule'], 'on': True}}}, outrank=False)
                for issue in _precision(c['definition']['rule'])]
    return _precision(raw['rule'])


def _precision(rule):
    fields = {}
    if rule['kind'] == 'allocation_preference':
        fields = {'balance_weight': 10_000, 'balance_target_share': 10_000}
    elif rule['kind'] == 'block_quota':
        fields = {'amount': 1_000_000_000}
    elif rule['kind'] == 'balance_goal':
        fields = {'weight': 10_000, 'target_share': 10_000}
    return [{'field': 'definition.rule.' + field, 'code': 'UNSUPPORTED_SOLVER_PRECISION',
             'message': 'Use a simpler fraction for this value.'}
            for field, maximum in fields.items()
            if Fraction(str(rule[field])).denominator > maximum]


def capability_catalogue():
    schema = PolicyDefinition.model_json_schema()
    mapping = schema['properties']['rule']['discriminator']['mapping']
    return {'compiler_version': COMPILER_VERSION,
            'rule_kinds': sorted(SUPPORTED_RULES.intersection(mapping)),
            'definition_schema': schema,
            'generation_defaults': GenerationOptions().model_dump(mode='json'),
            'precision_denominator_limits': {
                'allocation_preference.balance_weight': 10_000,
                'allocation_preference.balance_target_share': 10_000,
                'balance_goal.weight': 10_000,
                'balance_goal.target_share': 10_000,
                'block_quota.amount': 1_000_000_000},
            'boundary': 'Compatible definitions do not establish rota feasibility.'}


def compatibility_issues(data):
    from .conditions import active_rules
    from .rule_lines import bound_policies

    def issues(policy):
        data.get('_check_budget', lambda: None)()
        carrier = policy.get('_v2_validation_definition')
        definition = carrier if carrier is not None else policy['definition']
        found = definition_issues(definition, historical=True)
        if found or carrier is None and not policy.get('_v2_base_variants'):
            return found
        try:
            # The legacy wrapper cannot express heterogeneous v2 rows; every real primitive still passes strictly.
            return [issue for copy in active_rules([policy], overrides=False)
                    for issue in definition_issues({**copy['definition'], 'listed_only': False,
                                                    'scope': definition['scope']}, historical=True)]
        except ValidationError as error:
            return [{'field': '.'.join(['definition', *map(str, e['loc'])]),
                     'code': 'INVALID_RULE_DEFINITION', 'message': e['msg']}
                    for e in error.errors(include_input=False, include_url=False)]

    def check(policies):
        return [{**issue, 'rule_id': policy['id'], 'rule_revision': policy['revision']}
                for policy in policies
                if policy['status'] == 'active' and policy.get('active', True)
                for issue in issues(policy)]

    policies = data['policies']
    found = check(policies)
    if found:
        return found
    # Proof inputs have raw destinations; API snapshots already contain these source partitions.
    original = {policy['id']: policy['definition'] for policy in policies}
    return check(policy for policy in bound_policies(policies, data)
                 if policy['definition'] != original.get(policy['id']))


def generation_manifest():
    return {'algorithm': COMPILER_VERSION, 'engine': 'cp_sat', **PLANNING_LIMITS,
            'generation_scope': GENERATION_SCOPE_CAPABILITY,
            'default_options': GenerationOptions().model_dump(mode='json'),
            'supported_rule_kinds': sorted(SUPPORTED_RULES),
            'supported_type_schema_versions': [1], 'preserves_locked_assignments': True,
            'auto_publish': False, 'optimality_claim': 'Only when proven for all configured objectives',
            'search_time_unit': 'minutes', 'modes': ['rebalance', 'fill_gaps']}
