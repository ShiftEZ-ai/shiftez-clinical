"""The Rules page's "Download rules in plain words": the department's rules explained (questions in their order, each
rule's level, safety, sentence and lines, then the conflicts) as plain text. Real compiled rules_tree.ts in Node."""
import json
import subprocess


def test_rules_text_reads_every_question_in_order_with_its_rules_lines_and_conflicts(client_modules):
    rules = {
        'at': '2026-11-28',
        'groups': [{'key': 'b', 'letter': 'B', 'label': 'Who works when', 'order': 2},
                   {'key': 'a', 'letter': 'A', 'label': 'How many people', 'order': 1},
                   {'key': 'z', 'letter': 'Z', 'label': 'Nothing here', 'order': 3}],
        'rules': [{'rule_id': 'r1', 'catalogue_id': 'c1', 'question_group': 'b', 'safety': True, 'level': 'mandatory',
                   'summary_sentence': 'Rest 11 hours between duties.', 'line_ids': ['l1']},
                  {'rule_id': 'r2', 'catalogue_id': 'c2', 'question_group': 'a', 'safety': False, 'level': 'advisory',
                   'summary_sentence': '', 'unexplained': True},
                  {'rule_id': 'r3', 'question_group': 'later', 'safety': False, 'level': 'advisory',
                   'summary_sentence': 'A rule under a newer question.', 'summary_short': 'Newer'}],
        'conflicts': [{'conflict_id': 'k1', 'sentence': 'Two lines set different maximums.', 'line_ids': ['l1', 'l2'],
                       'rule_ids': ['r1'], 'line_refs': [1, 2], 'reach': {'sessions_count': 3}}],
    }
    script = f"""const tree=await import({json.dumps((client_modules / 'rules_tree.js').as_uri())});
const d={json.dumps(rules)};
const text=tree.rulesText(d,r=>r.summary_short||r.rule_id,r=>r.rule_id==='r1'?['Everyone in all clinics · every day']:[]);
console.log(JSON.stringify(text.split('\\n')));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        'Rules on 28 Nov 2026', '',
        # Questions in their order; one with no rules is left out.
        'A. How many people', '',
        # A rule the server cannot explain says so; its check still applies.
        'r2 · Advisory', 'Can’t explain this rule right now. Its check still applies.', '',
        'B. Who works when', '',
        'r1 · Mandatory · Safety', 'Rest 11 hours between duties.', '- Everyone in all clinics · every day', '',
        # A rule under a question this client does not know is still in the file.
        'Newer · Advisory', 'A rule under a newer question.', '',
        'Conflicts', '', '- Two lines set different maximums. Lines #1 and #2.', '',
    ]
