"""The auto-schedule result head and Score tile (autoschedule_result.ts), from the real compiled client in Node: a run
says it is ready to submit only when it is a current, valid, non-diagnostic candidate (app.ts candidateOptimality gives
its words), never from Must fix 0 alone; and a run without a score of its own never shows the rota's score as its own."""
# Where this file differs from dev maintenance's , each for a LATER deliberate decision on the maintenance line; the
# safety-of-meaning assertions (never ready without a valid candidate; only a strictly current candidate claims its
# proof) are maintenance's, unchanged:
# - issue cards: issueCard was replaced by the one issue list ; the since badge is read from its cards.
# - "Still to fix": one row plus "N more causes in the Fix list" (D76, ), not three rows.
# - changes by cause: "Other changes" comes last, with word counts (SW-04, ).
# - proof words: their own line after the numbers, not inside the outcome box ( anchors the box text).
import json
import subprocess

import pytest

from packages.contracts.rule_catalogue import FIX_LIST_WORDS
from packages.domain.rota_summary import generation_fix_lists
from .test_client_autoschedule_live import GLOBALS, FAKE_DOM

FACTS = {'scoreDelta': None, 'before': {'must': 2, 'could': 0, 'checked_at': None, 'places_filled': 2, 'places_total': 2, 'places_open': 0},
         'after': {'must': 0, 'could': 0, 'checked_at': None, 'places_filled': 2, 'places_total': 2, 'places_open': 0},
         'afterCauses': [], 'changesByCause': [], 'conflict': None, 'currency': 'current', 'reconciliation': None}


def run(client_modules, body, data):
    script = (GLOBALS + FAKE_DOM + "document.createElementNS=(ns,t)=>new N(t);"
              # Still to fix paints its own key (dom.ts legend) in a microtask and counts the rest on a button.
              "N.prototype.addEventListener=function(){};N.prototype.replaceChildren=function(...ns){for(const k of this.children)k.remove();this._t=String();this.append(...ns);};"
              f"const m=await import({json.dumps((client_modules/'autoschedule_result.js').as_uri())});"
              "const a=JSON.parse(process.argv[1]);" + body)
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_group_exclusion_line_counts_only_people_left_out(client_modules):
    texts = run(client_modules, """
        console.log(JSON.stringify(a.map(job=>m.outsideGroupLine(job)?.textContent ?? '')));
    """, [{}, {'planning_context': {'outside_group_count': 0}},
          {'planning_context': {'outside_group_count': 1}}, {'planning_context': {'outside_group_count': 2}}])
    assert texts == [
        '', '', '1 person left out because they are outside this group.',
        '2 people left out because they are outside this group.',
    ]


def test_solver_receipt_distinguishes_unfinished_score_from_no_candidate(client_modules):
    candidate = {'kind': 'generation', 'result': {'status': 'candidate_feasible',
        'solver_status': 'FEASIBLE', 'score': None, 'score_completed': False, 'change_count': 1}}
    empty = {'kind': 'generation', 'result': {'status': 'needs_more_time',
        'solver_status': 'UNKNOWN', 'score': None, 'score_completed': False, 'change_count': 0}}
    text = run(client_modules, "console.log(JSON.stringify(a.map(j=>m.solverReceipt(j).textContent)));",
               [candidate, empty])
    assert text[0].endswith(' · score unfinished')
    assert text[1].endswith(' · no score: nothing placed')


def test_solver_receipt_of_a_stopped_run_ignores_its_capped_summary_score(client_modules):
    stopped = {'kind': 'generation', 'result': {'status': 'candidate_feasible', 'solver_status': 'FEASIBLE',
        'score': None, 'score_completed': False, 'change_count': 1,
        'summary': {'score_saved': {'score': 80, 'score_completed': False}}}}
    finished = {'kind': 'generation', 'result': {'status': 'candidate_feasible', 'solver_status': 'FEASIBLE',
        'score': None, 'change_count': 1, 'summary': {'score_saved': {'score': 80}}}}
    text = run(client_modules, "console.log(JSON.stringify(a.map(j=>m.solverReceipt(j).textContent)));",
               [stopped, finished])
    assert text[0].endswith(' · score unfinished')
    assert text[1].endswith(' · score: 80')


def test_stopped_run_treats_copied_fix_lists_as_unknown(client_modules):
    before = {'summary': FACTS['after'], 'causes': []}
    result = {
        'status': 'candidate_feasible', 'input_fingerprint': 'f', 'stop_reason': 'operator_stop',
        'score_completed': False, 'score': None, 'search_stop': {'reason': 'operator_stop'},
        'validation': {'valid': True, 'error_count': 0},
        'additions': [{'shift_id': 's1', 'participant': {'person_id': 'p1'}}], 'removals': [],
    }
    fallback = generation_fix_lists({'fix_list_before': before}, result)
    assert fallback['fix_list_after']['summary'] == before['summary']
    known = {**result, 'fix_list_before': before, 'fix_list_after': before}
    stopped, complete = run(client_modules, """
        const read = result => {
            const facts = m.runFacts({kind: 'generation', result});
            const head = m.resultHead(facts, {changes: 1, scoreDelta: null});
            const changes = m.changesByCause(facts, 1);
            return {before: facts.before, after: facts.after, afterCauses: facts.afterCauses,
                changesByCause: facts.changesByCause, missing: facts.fixListMissing,
                head: head?.nodes[0].textContent ?? null, changes: changes ? changes.textContent : ''};
        };
        console.log(JSON.stringify(a.map(read)));
    """, [{**result, **fallback}, known])
    sentence = FIX_LIST_WORDS['generation_fallback']
    assert stopped['head'] is None
    assert stopped['before'] is None and stopped['after'] is None
    assert stopped['afterCauses'] == [] and stopped['changesByCause'] == []
    assert stopped['missing'] == sentence and stopped['changes'] == ''
    assert complete['before'] == complete['after'] == before['summary']
    assert complete['missing'] == ''
    assert complete['head'] == 'Nothing must be fixed'


# The outcome box (nodes[0]) says readiness; the proof words are the head's own line after the numbers ( "keep
# the optimality line in the A3 review", D76 run card ), so `text` reads the box and that line: never the tiles.
HEAD = ("const said=h.nodes.filter(n=>!n.className.split(' ').includes('auto-stats'));"
        "console.log(JSON.stringify({text:said.map(n=>n.textContent).join(' '),box:h.nodes[0].textContent,tone:h.nodes[0].className,"
        "cur:typeof cur==='undefined'?null:cur,words:typeof words==='undefined'?null:words}));")


def head(client_modules, optimality):
    return run(client_modules, "const h=m.resultHead(a.facts,{changes:1,scoreDelta:null,optimality:a.optimality});" + HEAD,
               {'facts': FACTS, 'optimality': optimality})


def test_must_fix_zero_without_a_candidate_never_says_ready(client_modules):
    """UNKNOWN or INFEASIBLE without a candidate, an invalid check or a diagnostic run: candidateOptimality gives ''."""
    out = head(client_modules, '')
    assert 'Ready to submit' not in out['text'] and 'Best' not in out['text']
    assert 'ok' not in out['tone'].split()


@pytest.mark.parametrize('words', ['Proposal ready · a better one may exist', 'Best proposal for these priorities'],
                         ids=['FEASIBLE', 'OPTIMAL'])
def test_a_current_valid_candidate_says_ready_with_its_own_proof(client_modules, words):
    out = head(client_modules, words)
    assert 'Ready to submit once applied.' in out['box'] and words in out['text'] and words not in out['box']
    assert 'Best schedule' not in out['text'] and 'ok' in out['tone'].split()


CANDIDATE = {'status': 'candidate_feasible', 'validation': {'valid': True}, 'optimality_proven': False}
CURRENCY = {  # what the server sends for each state (apps/api/main.py: currency from result_stale)
    'current': ({'input_fingerprint': 'f', 'result_stale': False, 'currency': 'current'}, True),
    'revalidation_required': ({'input_fingerprint': 'f', 'result_stale': True, 'currency': 'revalidation_required'}, False),
    'scheduler-changed': ({'input_fingerprint': 'f', 'result_stale': True}, False),
    'unknown': ({'input_fingerprint': 'f', 'result_stale': None, 'currency': 'unknown'}, False),
    'fingerprint-changed': ({'input_fingerprint': 'older', 'result_stale': False, 'currency': 'current'}, False),
    'rechecked-ready': ({'input_fingerprint': 'f', 'result_stale': False, 'currency': 'current', 'reconciliation': {'status': 'ready'}}, False),
    # A newer run replaced it: never current, whatever else the row says (Apply refuses it with RUN_SUPERSEDED).
    'superseded': ({'input_fingerprint': 'f', 'result_stale': False, 'currency': 'superseded'}, False),
}


def test_a_superseded_run_is_never_rechecked_even_with_an_earlier_receipt(client_modules):
    """A replaced run offers no "Check again and apply": its earlier reconciliation receipt does not make it appliable."""
    out = run(client_modules, "console.log(JSON.stringify({plain:m.rechecked(a.plain),replaced:m.rechecked(a.replaced),cur:m.strictlyCurrent(a.replaced,'f')}));", {
        'plain': {'currency': 'current', 'reconciliation': {'status': 'partial'}},
        'replaced': {'kind': 'generation', 'input_fingerprint': 'f', 'result_stale': False, 'currency': 'superseded',
                     'reconciliation': {'status': 'partial'}}})
    assert out == {'plain': True, 'replaced': False, 'cur': False}


def proof(client_modules, job):
    """The caller chain's proof: candidateOptimality(job, reviewable && strictlyCurrent(job, the rota's fingerprint))."""
    return run(client_modules, "const j=a.job,cur=m.strictlyCurrent(j,'f'),words=m.candidateOptimality(j,cur);"
               "const h=m.resultHead(a.facts,{changes:1,scoreDelta:null,optimality:words});" + HEAD,
               {'job': job, 'facts': FACTS})


@pytest.mark.parametrize('state', list(CURRENCY))
def test_only_a_strictly_current_candidate_claims_its_proof_and_readiness(client_modules, state):
    """A re-checked or changed run stays reviewable and appliable (app.ts currentGeneration), but never says ready."""
    fields, current = CURRENCY[state]
    out = proof(client_modules, {'kind': 'generation', **fields, 'result': CANDIDATE})
    assert out['cur'] is current
    assert ('Ready to submit once applied.' in out['text']) is current and ('Proposal ready' in out['text']) is current
    assert ('ok' in out['tone'].split()) is current and 'Best' not in out['text']


@pytest.mark.parametrize('result,words', [
    ({**CANDIDATE, 'optimality_proven': True}, 'Best proposal for these priorities'),
    ({**CANDIDATE, 'optimality_proven': False}, 'Proposal ready · a better one may exist'),
    ({**CANDIDATE, 'optimality_proven': None}, ''),
    ({**CANDIDATE, 'status': 'unknown_time_limit'}, ''),
    ({**CANDIDATE, 'status': 'candidate_with_gaps'}, ''),
    ({**CANDIDATE, 'status': 'requirements_conflict'}, ''),
    ({**CANDIDATE, 'validation': {'valid': False}}, ''),
    ({**CANDIDATE, 'diagnostic_proposal': True}, ''),
], ids=['OPTIMAL', 'FEASIBLE', 'unproven', 'UNKNOWN', 'with-gaps', 'INFEASIBLE', 'invalid', 'diagnostic'])
def test_the_proof_words_need_a_valid_non_diagnostic_candidate(client_modules, result, words):
    out = proof(client_modules, {'kind': 'generation', **CURRENCY['current'][0], 'result': result})
    assert out['words'] == words and ('Ready to submit' in out['text']) is bool(words)
    assert proof(client_modules, {'kind': 'generation', **CURRENCY['current'][0], 'diagnostic_proposal': True, 'result': CANDIDATE})['words'] == ''


def header(client_modules, job, reviewable=True):
    """app.ts panel header: readyWords(latest, reviewable, the rota's fingerprint) || the A4 label."""
    return run(client_modules, "console.log(JSON.stringify(m.readyWords(a.job,a.reviewable,'f')));", {'job': job, 'reviewable': reviewable})


NO_CHANGE = {**CANDIDATE, 'additions': [], 'removals': []}
CHANGED = {**CANDIDATE, 'additions': [{'shift_id': 's1'}], 'removals': []}


@pytest.mark.parametrize('state', list(CURRENCY))
def test_the_header_says_current_rota_ready_only_when_strictly_current(client_modules, state):
    """Zero changes: only a strictly current run says the current rota is ready; a re-checked one (revalidation_required
    or reconciliation ready, which the header does not call stale) falls back to the A4 "Ready for review" label."""
    fields, current = CURRENCY[state]
    assert header(client_modules, {'kind': 'generation', **fields, 'result': NO_CHANGE}) == ('Current rota is ready for review' if current else '')


@pytest.mark.parametrize('state', list(CURRENCY))
def test_the_header_proof_words_need_strict_currency_and_reviewability(client_modules, state):
    fields, current = CURRENCY[state]
    job = {'kind': 'generation', **fields, 'result': CHANGED}
    assert header(client_modules, job) == ('Proposal ready · a better one may exist' if current else '')
    assert header(client_modules, job, reviewable=False) == ''
    assert header(client_modules, {**job, 'result': {**CHANGED, 'optimality_proven': True}}) == ('Best proposal for these priorities' if current else '')
    assert header(client_modules, {**job, 'result': {**CHANGED, 'diagnostic_proposal': True}}) == ''
    assert header(client_modules, {**job, 'result': {**NO_CHANGE, 'validation': {'valid': False}}}) == ''


def tile(client_modules, summary, rota, strict=True):
    return run(client_modules, "const score=m.scoreTile(a.summary,a.rota,a.strict);"
               "console.log(JSON.stringify({text:score.textContent,about:score.querySelectorAll('.link-button').length}));",
               {'summary': summary, 'rota': rota, 'strict': strict})


def test_a_run_without_its_own_score_never_shows_the_rotas_as_its_own(client_modules):
    rota = {'score': 99, 'uncapped_score': 99}
    out = tile(client_modules, {'score_saved': None, 'score': None}, rota)
    assert out == {'text': 'Score now99', 'about': 0}  # the saved rota's, said as now (A-03), no About score
    assert tile(client_modules, {'score_saved': None, 'score': None}, None)['text'] == 'Score now–'
    own = tile(client_modules, {'score_saved': {'score': 59, 'uncapped_score': 70.5}, 'score_before': {'score': 59, 'uncapped_score': 68}}, rota)
    assert own['about'] == 1 and '70.5' in own['text'] and '+2.5 from 68' in own['text'] and '99' not in own['text']


def test_a_run_the_rota_drifted_under_shows_no_score_of_its_own_and_no_change(client_modules):
    """maintenance's rule: a number that compares the run with the rota now is shown only for a strictly current run. Otherwise
    the tile says the rota's score as "Score now" and no change: "from the saved rota now" would be false."""
    rota = {'score': 99, 'uncapped_score': 99}
    run_score = {'score_saved': {'score': 59, 'uncapped_score': 70.5}, 'score_before': {'score': 59, 'uncapped_score': 68}}
    assert tile(client_modules, run_score, rota, strict=False) == {'text': 'Score now99', 'about': 0}
    assert tile(client_modules, run_score, None, strict=False)['text'] == 'Score now–'
    head = run(client_modules, "const f={...a.facts,scoreDelta:2.5};"
               "const text=o=>m.resultHead(f,{changes:3,scoreDelta:2.5,...o}).nodes.map(n=>n.textContent).join('|');"
               "console.log(JSON.stringify({current:text({strict:true}),drifted:text({strict:false})}));", {'facts': FACTS})
    assert 'vs now' in head['current'] and 'vs now' not in head['drifted'] and 'Changes3' in head['drifted']


def finding(code, person, display, severity='error', shift=None, **detail):
    return {'code': code, 'severity': severity, 'rule_id': None, 'shift_id': shift, 'person_id': person, 'start': None,
            'end': None, 'detail': {'display': display, **detail}}


def key(f):
    return '|'.join(str(f.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))  # rota_summary.issue_key


PROPOSAL = [finding('TIME_OVERLAP', 'p1', '1 of 4'), finding('QUOTA_BELOW_MINIMUM', 'p2', '2 of 4'), finding('QUOTA_ABOVE_MAXIMUM', 'p3', '3 of 4')]


def cause(*found, level='must'):
    return {'id': 'c-' + '-'.join(f['code'] for f in found), 'level': level, 'checks': len(found), 'finding_keys': sorted({key(f) for f in found}),
            'sentence': 'Synthetic cause', 'meta': '', 'action': None, 'evidence': [], 'score_gain': None, 'rule_lines': [], 'acknowledgeable': False}


def test_issue_cards_and_still_to_fix_keep_their_badges_and_rows(client_modules):
    """The cards are the one issue list's (issue_marker.ts issueList replaced findings.ts issueCard in ), and
    Still to fix is the one next step: the top must-fix cause as its row, the rest counted (D76 run card, ).
    The cards keep the since badge; Still to fix has none (the per-cause since read the client's draftComparison)."""
    out = run(client_modules, f"const im=await import({json.dumps((client_modules/'issue_marker.js').as_uri())});"
              "const card=im.issueList([{severity:'error',words:'X',since:'changed'}]),plain=im.issueList([{severity:'error',words:'Y',detail:'d'}]);"
              "const facts={...a.facts,afterCauses:a.causes},more=()=>{};"
              "const bare=m.stillToFix(facts,{},more);"
              "const q=(n,s)=>n.querySelectorAll(s).map(x=>x.textContent);"
              "console.log(JSON.stringify({card:q(card,'.issue-since'),cards:card.querySelectorAll('.issue-card').length+plain.querySelectorAll('.issue-card').length,"
              "plain:q(plain,'.issue-since'),rows:bare.querySelectorAll('[data-level=\"must\"]').length,rest:q(bare,'.link-button'),"
              "bare:q(bare,'.issue-since')}));",
              {'facts': FACTS, 'causes': [cause(f) for f in PROPOSAL]})
    assert out == {'card': ['Changed by this proposal'], 'cards': 2, 'plain': [], 'rows': 1, 'rest': ['2 more causes in the Fix list'],
                   'bare': []}


def test_changes_by_cause_name_the_cause_each_change_clears(client_modules):
    """The server's changes_by_cause carries only {cause_id, changes} (rota_summary.generation_fix_lists): each row is
    worded by its cause in the Fix list before the run and counted in words; changes on sessions no cause named are
    "Other changes", last, never blank and never dropped from the total (SW-04; the same behaviour as
    tests/test_browser_fe_c4.py test_run_changes_are_named_by_cause_and_a_busy_button_shows_its_time)."""
    before = [{'id': 'c1', 'level': 'must', 'sentence': '1 session needs cover.', 'meta': 'Sun 4 Oct · 1 check'}]
    facts = {**FACTS, 'beforeCauses': before, 'changesByCause': [{'cause_id': None, 'changes': 1}, {'cause_id': 'c1', 'changes': 2}]}
    # FAKE_DOM matches selectors by class only (a tag selector matches every node), so rows are read by class.
    out = run(client_modules, "const n=m.changesByCause(a.facts,3),q=s=>n.querySelectorAll(s).map(x=>x.textContent);"
              "console.log(JSON.stringify({rows:q('.home-need-text'),counts:q('.home-count')}));", {'facts': facts})
    assert out == {'rows': ['1 session needs cover.2 changes · Sun 4 Oct · 1 check', 'Other changes1 change'], 'counts': ['2', '1']}
    gone = run(client_modules, "console.log(JSON.stringify(m.changesByCause(a.facts,1).querySelectorAll('.home-need-text').map(x=>x.textContent)));",
               {'facts': {**FACTS, 'beforeCauses': [], 'changesByCause': [{'cause_id': 'gone', 'changes': 1}]}})
    assert gone == ['Other changes1 change']  # a cause the before list no longer names is never a blank row
    empty = run(client_modules, "console.log(JSON.stringify(m.changesByCause(a.facts,0)));", {'facts': {**FACTS, 'beforeCauses': [], 'changesByCause': []}})
    assert empty == ''


def test_still_to_fix_prefers_the_kept_absence_card(client_modules):
    absence = {**finding('UNAVAILABLE', 'p1', '', shift='s1'), 'protected': True}
    result = {'issues': [absence], 'fix_list_after': {'summary': {**FACTS['after'], 'must': 2},
              'causes': [cause(PROPOSAL[1]), cause(absence)]}}
    out = run(client_modules,
              "const f=m.runFacts(a), issue={severity:'error',words:'On leave or away',session:'Morning session'};"
              "const panel=m.stillToFix(f,{},()=>{},f.keptAbsence?issue:undefined);"
              "console.log(JSON.stringify({code:f.keptAbsence?.code,card:panel.querySelector('.issue-card')?.textContent,"
              "more:panel.querySelector('.link-button')?.textContent}));", {'result': result})
    assert out == {'code': 'UNAVAILABLE', 'card': 'On leave or awayMorning session',
                   'more': '1 more cause in the Fix list'}


@pytest.mark.parametrize(('issues', 'said'), [
    ([{'code': 'OUTSIDE_HOURS'}], 'Not run: the draft has 1 conflict the run must keep'),
    ([{'code': 'A'}, {'code': 'B'}], 'Not run: the draft has 2 conflicts the run must keep'),
    (None, 'Not run: the draft has conflicts the run must keep'),
])
def test_a_run_refused_before_the_solver_says_not_run_never_a_solver_error(client_modules, issues, said):
    """D-126: a pre-run refusal (cp_sat invalid_inputs) has no solver status, no "error" and no score."""
    job = {'kind': 'generation', 'result': {'status': 'invalid_inputs', 'issues': issues, 'stop_reason': 'failed'}}
    got = run(client_modules, "console.log(JSON.stringify(m.solverReceipt(a).textContent));", job)
    assert got == said
    for word in ('Solver status', 'error', 'score'):
        assert word not in got


def receipt(client_modules, result):
    return run(client_modules, "console.log(JSON.stringify(m.solverReceipt(a).textContent));",
               {'kind': 'generation', 'result': result})


def receipt_tip(client_modules, result):
    return run(client_modules, "const h=m.solverReceipt(a).querySelector('.icon-button');"
               "console.log(JSON.stringify(h?.dataset.tip||null));", {'kind': 'generation', 'result': result})


def test_finished_model_and_incomplete_proposal_have_distinct_status_labels(client_modules):
    result = {'status': 'candidate_with_gaps', 'solver_status': 'OPTIMAL',
              'search_stop': {'reason': 'model_proven'},
              'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'with_gaps', 'optimality_proven': False}}
    got = receipt(client_modules, result)
    assert 'Proposal status: UNKNOWN' in got
    assert 'Solver status: UNKNOWN' not in got
    assert 'Stop reason: search finished, then the checks changed the proposal' in got and 'proven best' not in got
    assert "model ended OPTIMAL" in receipt_tip(client_modules, result)


@pytest.mark.parametrize('result', [
    {
        'status': 'candidate_with_gaps', 'solver_status': 'OPTIMAL',
        'search_stop': {'reason': 'optimal'},
        'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'with_gaps',
                               'strict_status': None, 'optimality_proven': False},
    },
    {
        'status': 'candidate_feasible', 'validation': {'valid': False}, 'solver_status': 'OPTIMAL',
        'search_stop': {'reason': 'optimal'},
        'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'with_gaps',
                               'strict_status': None, 'optimality_proven': False},
    },
], ids=['candidate-with-gaps', 'validation-failed'])
def test_solver_receipt_uses_unknown_final_outcome_for_diagnostic_candidates(client_modules, result):
    got = receipt(client_modules, result)
    assert 'Proposal status: UNKNOWN' in got
    assert 'diagnostic model:' not in got
    assert 'Stop reason' not in got
    assert 'proven best' not in got
    tip = receipt_tip(client_modules, result)
    assert tip and 'OPTIMAL' in tip and 'not proven best' in tip


def test_solver_receipt_reports_needs_more_time_as_unknown_with_diagnostic_model(client_modules):
    result = {
        'status': 'needs_more_time', 'solver_status': 'FEASIBLE',
        'search_stop': {'reason': 'time_limit'},
        'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'with_gaps',
                               'strict_status': None, 'optimality_proven': False},
    }
    got = receipt(client_modules, result)
    assert 'Proposal status: UNKNOWN' in got
    assert 'diagnostic model:' not in got
    assert 'search time used up' in got
    tip = receipt_tip(client_modules, result)
    assert tip and 'FEASIBLE' in tip and 'not proven best' in tip


def test_solver_receipt_reports_requirements_conflict_without_diagnostic_model(client_modules):
    got = receipt(client_modules, {
        'status': 'requirements_conflict', 'solver_status': 'INFEASIBLE',
        'search_stop': {'reason': 'conflict'},
        'generation_outcome': {'status': 'INFEASIBLE', 'candidate': 'none',
                               'strict_status': 'INFEASIBLE', 'optimality_proven': False},
    })
    assert 'Solver status: INFEASIBLE' in got
    assert 'Stop reason: rules conflict' in got
    assert 'diagnostic model:' not in got


def test_solver_receipt_reports_valid_feasible_without_proven_best(client_modules):
    job = {'kind': 'generation', 'input_fingerprint': 'f', 'result_stale': False, 'currency': 'current', 'result': {
        'status': 'candidate_feasible', 'validation': {'valid': True}, 'solver_status': 'OPTIMAL',
        'optimality_proven': False,
        'additions': [{'shift_id': 's1'}], 'removals': [],
        'search_stop': {'reason': 'optimal'},
        'generation_outcome': {'status': 'FEASIBLE', 'candidate': 'valid',
                               'strict_status': None, 'optimality_proven': False},
    }}
    got = receipt(client_modules, job['result'])
    assert 'Proposal status: FEASIBLE' in got
    tip = receipt_tip(client_modules, job['result'])
    assert tip and 'OPTIMAL' in tip and 'not proven best' in tip
    assert 'diagnostic model:' not in got
    assert 'proven best' not in got
    assert header(client_modules, job) == 'Proposal ready \u00b7 a better one may exist'
    assert 'proven best' not in header(client_modules, job)


def test_valid_feasible_header_never_says_proven_best_when_model_was_optimal(client_modules):
    job = {'kind': 'generation', 'input_fingerprint': 'f', 'result_stale': False, 'currency': 'current', 'result': {
        'status': 'candidate_feasible', 'validation': {'valid': True}, 'solver_status': 'OPTIMAL',
        'optimality_proven': False,
        'additions': [{'shift_id': 's1'}], 'removals': [],
        'search_stop': {'reason': 'optimal'},
        'generation_outcome': {'status': 'FEASIBLE', 'candidate': 'valid',
                               'strict_status': None, 'optimality_proven': False},
    }}
    assert header(client_modules, job) == 'Proposal ready \u00b7 a better one may exist'
    assert 'proven best' not in header(client_modules, job)


def test_solver_receipt_reports_valid_optimal_as_proven_best(client_modules):
    got = receipt(client_modules, {
        'status': 'candidate_feasible', 'validation': {'valid': True}, 'solver_status': 'OPTIMAL',
        'search_stop': {'reason': 'optimal'},
        'generation_outcome': {'status': 'OPTIMAL', 'candidate': 'valid',
                               'strict_status': None, 'optimality_proven': True},
    })
    assert 'Solver status: OPTIMAL' in got
    assert 'Stop reason: proven best for these priorities' in got
    assert 'diagnostic model:' not in got


def test_solver_receipt_falls_back_to_model_status_without_generation_outcome(client_modules):
    got = receipt(client_modules, {
        'status': 'candidate_feasible', 'solver_status': 'OPTIMAL',
        'search_stop': {'reason': 'optimal'},
    })
    assert 'Solver status: OPTIMAL' in got
    assert 'Stop reason: proven best for these priorities' in got


def test_a_run_continued_past_a_conflict_counts_its_rules_broken(client_modules):
    texts = run(client_modules, "console.log(JSON.stringify(a.map(r=>m.breaksNote(r)?.textContent ?? '')));", [
        {'on_conflict': 'continue_flagged', 'breaks': [{}]}, {'on_conflict': 'continue_flagged', 'breaks': [{}, {}]},
        {'on_conflict': 'report', 'breaks': [{}]}])
    assert texts == ['Continued with 1 rule broken · each must be fixed before publishing',
                     'Continued with 2 rules broken · each must be fixed before publishing', '']


def test_a_capacity_cause_without_a_total_names_its_requirements_not_the_engine_note(client_modules):
    named = ('These requirements cannot all be met together with eligibility, availability and the other mandatory '
             'rules: Night cover (line 1, minimum), Weekly cap (line 2, maximum)')
    cause = {'message': named, 'requirements': [{'kind': 'policy', 'rule_id': 'r1', 'name': 'Night cover'},
                                                {'kind': 'policy', 'rule_id': 'r2', 'name': 'Weekly cap'}],
             'evidence': {'complete': False, 'basis': 'not_established', 'required_places': None, 'maximum_possible': None,
                          'message': 'These named requirements conflict; a staffing total has not been established.'},
             'decisions': []}
    got = run(client_modules, """
        const h={ruleName:id=>id,person:id=>id,clinic:()=>'',openRule:()=>{},eligibility:()=>{},workspace:{id:'w',revision:1},done:async()=>{}};
        console.log(JSON.stringify(m.clashCard(a,h).querySelector('.clash-math').textContent));
    """, {'proven': True, 'causes': [cause]})
    assert got == named
    assert 'staffing total' not in got
