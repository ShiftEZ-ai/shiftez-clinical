"""Real compiled client helpers in Node (logic only): every name format, initial and code reads one parser under the
department's title and particle rules, and a shown label that is not the full name keeps it in the spoken name (D75).
Fictional names only."""
from tests.test_client_codes_and_credentials import run


def test_one_parser_splits_title_given_and_family_with_its_particle(client_modules):
    got = run(client_modules, 'dom.js', 'input.map(n=>m.nameWords(n))',
              ['Dr Keiko Al Cooper', 'Prof. Layla Haddad', 'د. قرنفل البحري', 'Nico', 'rota-coordinator'])
    assert got[0] == {'title': 'Dr', 'bare': 'Keiko Al Cooper', 'given': ['Keiko'], 'family': 'Al Cooper'}
    assert got[1]['title'] == 'Prof.' and got[1]['given'] == ['Layla'] and got[1]['family'] == 'Haddad'
    assert got[2]['title'] == 'د.' and got[2]['given'] == ['قرنفل'] and got[2]['family'] == 'البحري'
    assert got[3] == {'title': '', 'bare': 'Nico', 'given': [], 'family': 'Nico'}
    assert got[4]['given'] == [] and got[4]['family'] == 'rota-coordinator'


def test_initials_and_codes_follow_the_department_lists(client_modules):
    expression = ("(()=>{const before=[m.initials('Dr Keiko Al Cooper'),m.autoCode('Dr Keiko Al Cooper'),m.autoCode('Pavel AlTurner'),"
                  "m.initials('Mr Kopofiku Orrin')];m.codeRules.titles=['Rev'];m.codeRules.particles=['Ter'];"
                  "const after=[m.initials('Rev Iris Al Walker'),m.autoCode('Iris Al Walker'),m.nameWords('Iris Al Walker').family,"
                  "m.initials('Dr Keiko Al Cooper')];return [before,after];})()")
    before, after = run(client_modules, 'dom.js', expression)
    assert before == ['KA', 'KCO', 'PTU', 'KO']
    assert after == ['IA', 'IAW', 'Walker', 'DK']


def test_label_parts_use_the_particle_and_spoken_names_keep_the_full_name(client_modules):
    expression = ("(()=>{const p={id:'p1',revision:1,display_name:'Dr Keiko Al Cooper',pager:'0000',staff_number:'00000'};"
                  "const f={level:'R2',code:'ZZQ'};return {"
                  "short:m.personLabel(p,{parts:['short','level']},f),bare:m.personLabel(p,{parts:['short'],honorific:'none'},f),"
                  "code:m.personLabel(p,{parts:['code']},f),comma:m.personLabel(p,{parts:['full','pager'],separator:'comma'},f),"
                  "spokenCode:m.spokenName('ZZQ','Dr Keiko Al Cooper'),spokenFull:m.spokenName('Dr Keiko Al Cooper · R2','Dr Keiko Al Cooper'),"
                  "known:m.personName('missing','Dr Nico Miller',{parts:['short'],honorific:'none'})};})()")
    got = run(client_modules, 'person_label.js', expression)
    assert got == {'short': 'Dr K. Al Cooper · R2', 'bare': 'K. Al Cooper', 'code': 'ZZQ', 'comma': 'Dr Keiko Al Cooper, 0000', 'spokenCode': 'ZZQ, Dr Keiko Al Cooper', 'spokenFull': 'Dr Keiko Al Cooper · R2', 'known': 'N. Miller'}


def test_only_labels_that_clash_in_a_view_expand_first_to_given_and_family_then_to_the_full_name(client_modules):
    expression = ("(()=>{const P=(id,n)=>({id,revision:1,display_name:n});"
                  "const a=P('a','Dr Keiko Al Cooper'),b=P('b','Dr Kopofiku Al Cooper'),c=P('c','Keiko Iris Al Cooper'),"
                  "d=P('d','Keiko Nico Al Cooper'),e=P('e','Nico Miller');"
                  "const view=m.distinctNames([a,b,c,d,e,e,undefined],{parts:['short']}),alone=m.distinctNames([a,e],{parts:['short']}),"
                  "full=m.distinctNames([a,b],{parts:['full']});"
                  "return {view:[a,b,c,d,e].map(view),alone:[a,b,e].map(alone),full:[a,b].map(full),nobody:view(undefined)};})()")
    got = run(client_modules, 'person_label.js', expression)
    # a and b share "Dr K. Al Cooper": given and family name tell them apart. c and d still share "Keiko Al Cooper"
    # after that, so they say their full names. e is listed twice but is one person, so it never clashes with itself.
    assert got['view'] == ['Dr Keiko Al Cooper', 'Dr Kopofiku Al Cooper', 'Keiko Iris Al Cooper', 'Keiko Nico Al Cooper', 'N. Miller']
    # Only people in the view count: b outside it keeps the short label, as does a without b.
    assert got['alone'] == ['Dr K. Al Cooper', 'Dr K. Al Cooper', 'N. Miller']
    assert got['full'] == ['Dr Keiko Al Cooper', 'Dr Kopofiku Al Cooper']  # a label without the short part is unchanged
    assert got['nobody'] == 'Unavailable record'
