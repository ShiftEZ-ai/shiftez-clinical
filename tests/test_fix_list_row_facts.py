"""Lean Fix list row facts (LEAD-UIUX decision, 8 Nov 2026): what a collapsed Fix list row, the footer and the gate show
or do must come from the lean view (`view=lean`, generated FixListLean*), never wait for Show evidence. The client reads
the lean view in one place (apps/web/src/fix_list.ts rowFacts); this checks the generated contract carries every row
fact, so a field moved to the detail view fails here before a Safety tag, a line or an action goes missing."""
import re
from pathlib import Path

GENERATED = (Path(__file__).resolve().parents[1] / 'apps/web/src/generated.ts').read_text(encoding='utf-8')

# Each fact and who reads it (fix_list.ts unless said).
ROW_FACTS = {
    'FixListLean': {'review_items': 'Submit and the approver review', 'planning_context': 'scheduling groups (groups_view.ts)'},
    'FixListLeanCause': {
        'safety': 'the Safety tag; never acknowledgeable (D3)', 'new': 'the New tag and Sort › Newest',
        'detail_line': 'the line under the sentence', 'where': 'that line and "Sessions: …" on the chips (app.ts)',
        'acknowledgeable': '"Can’t be acknowledged" on Submit (app.ts)', 'acknowledgement': 'acknowledged gaps',
        'finding_keys': 'a Week issue to its cause (app.ts) and the issue list key',
        'session_findings': 'the fill box issue (app.ts)', 'person_ids': 'Filter by people',
        'attribution': 'Group › Scheduling group', 'rule_id': 'Group › Rule, Change the rule…',
        # Beside rule_id, its first binding line: adjust_rota prefills that line (app.ts fixAction), not only the rule.
        'line_id': 'adjust_rota: the binding line', 'slot': 'adjust_rota: which of its numbers'},
    'FixListLeanAction': {'scope': 'every action, actionLabel (classification), the R1 batch',
                          'unavailable_reason': 'why no one can be assigned (app.ts fixAction)'},
    # What an action does with its scope (app.ts fixAction, fixFocusHooks): Assign's session, a scoped run's keep.
    'FixListLeanActionScope': {'shift_id': 'Assign opens the session the server chose (fixes[0].scope.shift_id)',
                               'keep_existing': 'a scoped run keeps existing duties unless the server says otherwise'},
    'FixListLeanSummary': {
        'checked_at': '"Checked 2 min ago" and heldFixList', 'since': '"Today: 2 cleared · 1 new"',
        'population_note': 'who was checked', 'submission_issue': 'the gate and Add session',
        'score': 'the footer\'s "Ready · score"', 'uncapped_score': 'the score while must-fix causes remain'},
}


def fields(name):
    block = re.search(r'export type %s = \{\n(.*?)\n\};' % re.escape(name), GENERATED, re.S)
    assert block, f'{name} is not in generated.ts'
    return set(re.findall(r'^\s*"(\w+)"\??:', block.group(1), re.M))


def test_the_lean_contract_carries_every_row_fact():
    missing = {name: sorted(set(facts) - fields(name)) for name, facts in ROW_FACTS.items()}
    missing = {name: [f'{k} ({ROW_FACTS[name][k]})' for k in keys] for name, keys in missing.items() if keys}
    assert not missing, 'Row facts the lean Fix list must carry (PERF lean_fix_list):\n' + '\n'.join(
        f'{name}: ' + '; '.join(keys) for name, keys in missing.items())
