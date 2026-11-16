"""AUTOSCHED-UX: a finished run is headed by its final proposal, never by the strict phase's history (LIVE-QA cycle 5
R-5: "These rules can't all be met" beside Must fix 0 and Apply 4 changes), and says why its search ended only from the
result's own receipts. Real compiled client modules in Node."""
import json

from .test_client_autoschedule_live import node

CONFLICT = {'strict_status': 'INFEASIBLE', 'proven': True, 'causes': []}
SUMMARY = {'must': 0, 'could': 0, 'places_open': 0}


def clash(client_modules, result):
    return node(client_modules, 'autoschedule_result.js',
                f"console.log(JSON.stringify(m.isClash(m.runFacts({{result:{json.dumps(result)}}}))));")


def test_a_valid_proposal_is_never_headed_as_a_clash(client_modules):
    after = {'summary': SUMMARY, 'causes': []}
    # The contract's final outcome decides first.
    assert clash(client_modules, {'conflict': CONFLICT, 'fix_list_after': after,
        'generation_outcome': {'status': 'FEASIBLE', 'candidate': 'valid', 'strict_status': 'INFEASIBLE', 'optimality_proven': False}}) is False
    assert clash(client_modules, {'conflict': CONFLICT, 'fix_list_after': {'summary': {**SUMMARY, 'must': 2}, 'causes': []},
        'generation_outcome': {'status': 'INFEASIBLE', 'candidate': 'with_gaps', 'strict_status': 'INFEASIBLE', 'optimality_proven': False}}) is True
    # Before the outcome field: nothing must be fixed after the run, so no clash headline.
    assert clash(client_modules, {'conflict': CONFLICT, 'fix_list_after': after}) is False
    assert clash(client_modules, {'conflict': CONFLICT, 'fix_list_after': {'summary': {**SUMMARY, 'must': 1}, 'causes': []}}) is True
    # No proposal at all (requirements_conflict): the proven clash is the story.
    assert clash(client_modules, {'conflict': CONFLICT}) is True
    assert clash(client_modules, {'conflict': {**CONFLICT, 'strict_status': 'UNKNOWN'}}) is False


def test_why_the_search_ended_comes_from_the_result_and_is_never_guessed(client_modules):
    cases = [
        {'optimality_proven': True},
        {'generation_outcome': {'optimality_proven': True}},
        {'fair_share_search': {'stop_reason': 'operator_stop'}},
        {'objectives': [{'name': 'weighted', 'stopped_without_improvement': True}]},
        {'objectives': [{'name': 'weighted', 'search': {'stop_reason': 'time_limit'}}]},
        {'objectives': [{'name': 'weighted', 'stopped_without_improvement': False}]},
        None,
    ]
    out = node(client_modules, 'autoschedule_result.js',
               f"console.log(JSON.stringify({json.dumps(cases)}.map(r=>m.searchEnded(r))));")
    assert out == ['proven best for these priorities', 'proven best for these priorities', 'stopped by you',
                   'stopped when it stopped improving', 'search time used up', '', '']
