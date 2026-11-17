"""Search options (apps/web/src/search.ts): partial versus exact text per field, case, own text versus related records,
picked records by id, and what a server read sends. A search matches text or explicit references only; it never says a
rule applies to someone."""
import json
import subprocess

from packages.contracts import schemas as s


def run(client_modules, expression):
    script = (f"const m=await import({json.dumps((client_modules / 'search.js').as_uri())});"
              f"console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_partial_is_the_default_and_exact_compares_one_whole_field(client_modules):
    fields = ['Dr Olivia Quinn', 'Urgent Cover', ' Urgent Cover']
    assert run(client_modules, f"[{json.dumps(fields)}.map(m.fieldMatcher(' quinn ')),"
                               f"{json.dumps(fields)}.map(m.fieldMatcher('quinn',{{match:'exact'}})),"
                               f"{json.dumps(fields)}.map(m.fieldMatcher(' urgent cover ',{{match:'exact'}})),"
                               f"{json.dumps(fields)}.map(m.fieldMatcher('urgent cover',{{case_sensitive:true}})),"
                               "m.fieldMatcher('  ')]") == [
        # Only the query's ends are trimmed (as the server's text_matches): a field's own spaces are part of it.
        [True, False, False], [False, False, False], [False, True, False], [False, False, False], None]


def test_literal_unicode_matching_has_no_fuzzy_or_accent_folding(client_modules):
    assert run(client_modules, "[m.fieldMatcher('ÉCOLE')('école'),m.fieldMatcher('ecole')('école'),"
                               "m.fieldMatcher('50%')('50% cover'),m.fieldMatcher('a_b')('axb')]") == [True, False, True, False]


def test_options_normalize_to_what_differs_from_the_contract_defaults(client_modules):
    defaults = s.SearchOptions().model_dump()
    out = run(client_modules, f"[m.normalSearch(null),m.normalSearch({json.dumps(defaults)}),m.normalSearch({{scope:'all',match:'partial'}}),"
                              "m.normalSearch({reference:{kind:'people',id:'p1',label:'ignored'},match:'exact',scope:'self',case_sensitive:false}),"
                              "m.searchParam({match:'partial'}),m.searchParam({match:'exact'}),"
                              "m.sameSearch(undefined,{match:'partial',scope:'all',case_sensitive:false,reference:null}),m.sameSearch({match:'exact'},null)]")
    # Defaults (and a reset) send nothing: the older plain search stays as it was, on the server too.
    assert out[:3] == [None, None, None]
    # A picked record keeps only its identity, never a label.
    assert out[3] == {'match': 'exact', 'scope': 'self', 'reference': {'kind': 'people', 'id': 'p1'}}
    assert out[4] is None and json.loads(out[5]) == {'match': 'exact'}
    assert s.SearchOptions.model_validate_json(out[5]).match == 'exact'
    assert out[6:] == [True, False]


def test_scope_reads_own_fields_related_names_or_both(client_modules):
    reads = run(client_modules, "['all','self','related',undefined].map(scope=>[m.searchReads({scope},false),m.searchReads({scope},true)])")
    assert reads == [[True, True], [True, False], [False, True], [True, True]]


def test_record_matcher_compares_each_name_on_its_own_and_picked_records_by_id(client_modules):
    record = ("{own:()=>['Dr Olivia Quinn','DEMO1'],related:()=>['Urgent Cover','Field Medicine'],id:'person-1',"
              "refs:()=>['programme-uc','tag-1']}")
    out = run(client_modules, f"(()=>{{const r={record};const t=(q,o)=>{{const f=m.recordMatcher(q,o);return f?f(r):null;}};return ["
              "t('quinn'),t('quinn',{scope:'related'}),t('urgent',{scope:'self'}),t('urgent',{scope:'related'}),"
              # Exact never matches a joined multi-name text.
              "t('Urgent Cover Field Medicine',{match:'exact'}),t('urgent cover',{match:'exact',scope:'related'}),"
              # A picked person is the record itself (self); a picked programme finds who references it (related).
              "t('anything',{reference:{kind:'people',id:'person-1'}}),t('',{reference:{kind:'people',id:'person-1'},scope:'related'}),"
              "t('',{reference:{kind:'programmes',id:'programme-uc'}}),t('',{reference:{kind:'programmes',id:'programme-uc'},scope:'self'}),"
              "t('',{reference:{kind:'tags',id:'tag-2'}}),t('',{})]})()")
    assert out == [True, False, False, True, False, True, True, False, True, False, False, None]


def test_capabilities_and_words_come_from_the_generated_registry(client_modules):
    out = run(client_modules, "[m.serverCapability('policy_lines'),m.serverCapability('people').kinds.includes('people'),"
                              "m.serverCapability('rule-tree'),m.searchWords({match:'exact',scope:'self'}),m.searchWords({match:'partial'}),"
                              "m.kindLabel('programmes')]")
    from apps.api.services.search import registry
    lines = registry()['policy_lines']
    assert out[0]['collection'] == 'policy_lines' and out[0]['scopes'] == list(lines['scopes'])
    assert sorted(out[0]['kinds']) == sorted(lines['reference_kinds'])
    # A view key is not a collection: Rules use policy_lines, never rule-tree.
    assert out[1] is True and out[2] is None
    modes = {m['value']: m['label'] for m in s.SEARCH_MODES}
    scopes = {x['value']: x['label'] for x in s.SEARCH_SCOPES}
    assert out[3] == f"{modes['exact']} · {scopes['self']}" and out[4] == ''
    assert out[5] == 'Programmes'


def test_empty_reference_query_asks_the_server_nothing(client_modules):
    assert run(client_modules, "await m.findReferences('people','programmes','   ')") == {'items': [], 'has_more': False}


def test_filtered_strip_says_what_the_search_and_filters_hide_none_left_included(client_modules):
    """collectionToolbar's strip (search.ts filteredWords): only a search or filters beyond the view's own, only when
    they hide rows; nothing left still says it (the strip's Show all is the way back); a server-applied list has no
    "of"; search option words only with a search."""
    rules = ['rule', 'rules']
    cases = [
        {'searching': False, 'filters': 0, 'count': 3, 'of': 10, 'noun': rules},
        {'searching': True, 'filters': 0, 'count': 3, 'of': 10, 'noun': rules},
        {'searching': False, 'filters': 2, 'count': 0, 'of': 10, 'noun': rules},
        {'searching': True, 'filters': 1, 'count': 10, 'of': 10, 'noun': rules},
        {'searching': True, 'filters': 0, 'count': 1, 'noun': rules},
        {'searching': False, 'filters': 1, 'count': 0, 'noun': ['person', 'people']},
        {'searching': True, 'filters': 0, 'count': 2, 'of': 5, 'noun': rules, 'options': 'Exact text'},
        {'searching': False, 'filters': 1, 'count': 2, 'of': 5, 'noun': rules, 'options': 'Exact text'},
    ]
    assert run(client_modules, f"{json.dumps(cases)}.map(m.filteredWords)") == [
        None, 'Filtered: 3 of 10 rules shown', 'Filtered: 0 of 10 rules shown', None, 'Filtered: 1 rule shown',
        'Filtered: 0 people shown', 'Filtered: 2 of 5 rules shown · Exact text', 'Filtered: 2 of 5 rules shown']
