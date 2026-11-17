"""Why an auto-schedule run stopped or did not start (DL-049), from the real compiled client in Node: findings.ts
runError, the one error code → words table the run dialog and the refused start both use."""
import json
import subprocess


def words(client_modules, code, issues=None, total=None):
    script = (f"globalThis.document={{cookie:''}};globalThis.matchMedia=()=>({{matches:false,addEventListener(){{}}}});"
              f"const f=await import({json.dumps((client_modules/'findings.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(f.runError(i.code,i.issues,i.total??undefined)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'code': code, 'issues': issues, 'total': total})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def issue(rule, line=None, message='Use whole hours.'):
    field = f'definition.rule.lines.{line}.numbers' if line is not None else 'definition.rule.kind'
    return {'rule_id': rule, 'rule_revision': 1, 'field': field, 'code': 'INVALID_RULE_DEFINITION', 'message': message}


def test_incompatible_rules_name_each_line_once_with_the_servers_reason(client_modules):
    said = words(client_modules, 'SOLVER_RULE_INCOMPATIBLE',
                 [issue('r1', 0), issue('r1', 0, 'Again.'), issue('r1', 2, ''), issue('r2')])
    assert said['head'] == '3 rule lines can’t be scheduled automatically'
    assert said['body'] == 'Change these lines, then run auto-schedule again:'
    assert said['action'] is None
    assert said['lines'] == [{'rule_id': 'r1', 'line': 0, 'reason': 'Use whole hours.'},
                             {'rule_id': 'r1', 'line': 2, 'reason': ''},
                             {'rule_id': 'r2', 'line': None, 'reason': 'Use whole hours.'}]
    assert words(client_modules, 'SOLVER_RULE_INCOMPATIBLE', [issue('r1', 1)])['head'] == (
        '1 rule line can’t be scheduled automatically')


def test_incompatible_rules_without_a_list_open_rules(client_modules):
    for issues in (None, [], [{'field': 'x'}]):
        assert words(client_modules, 'SOLVER_RULE_INCOMPATIBLE', issues) == {
            'head': 'A rule can’t be scheduled automatically',
            'body': 'Change the lines marked on Rules, then run auto-schedule again.', 'action': 'rules', 'lines': []}


def test_access_revoked_keeps_check_your_access(client_modules):
    assert words(client_modules, 'ACCESS_REVOKED') == {
        'head': 'This run could not finish', 'body': 'Check your access and try again.', 'action': None, 'lines': []}


def test_group_generation_scope_names_the_group_and_offers_no_feedback(client_modules):
    assert words(client_modules, 'GROUP_GENERATION_SCOPE') == {
        'head': 'This run was refused',
        'body': 'The selected population is outside your scheduling group. Choose only your group’s people, then run auto-schedule again.',
        'action': None, 'lines': []}


def test_an_unknown_code_offers_feedback_and_never_asks_to_quote_it(client_modules):
    said = words(client_modules, 'WORKER_LEASE_EXPIRED')
    assert said == {'head': 'This run could not finish',
                    'body': 'This run stopped. Run auto-schedule again. If it stops again, send feedback.',
                    'action': 'feedback', 'lines': []}
    assert 'WORKER_LEASE_EXPIRED' not in said['body'] and 'quote' not in said['body'].lower()


def test_a_capped_list_names_what_came_and_opens_rules_for_the_rest(client_modules):
    """A failed run's stored error_detail is capped (total = every issue): the head counts the lines named, never total."""
    said = words(client_modules, 'SOLVER_RULE_INCOMPATIBLE', [issue('r1', 0), issue('r1', 0, 'Again.')], total=40)
    assert said['head'] == '1 rule line can’t be scheduled automatically'
    assert said['action'] == 'rules'
    assert words(client_modules, 'SOLVER_RULE_INCOMPATIBLE', [issue('r1', 0)], total=1)['action'] is None
