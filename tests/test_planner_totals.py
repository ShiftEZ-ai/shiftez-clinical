"""People › Totals: the saved list (PlannerViewSettings.totals) and, from the real compiled client in Node, what each
total counts and which person quota in the server's quota report it shows against (planner_totals.ts)."""
import json
import subprocess

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s

TYPE = '0b7e1a52-3c4d-4e5f-8a9b-0c1d2e3f4a5b'


def test_totals_are_optional_known_each_once_and_bounded():
    assert 'totals' not in s.PlannerViewSettings.model_validate({'mode': 'person'}).model_dump(mode='json')
    kept = s.PlannerViewSettings.model_validate({'mode': 'person', 'totals': [
        {'key': 'duties'}, {'key': 'hours', 'on': False}, {'key': 'time:N'}, {'key': 'type:' + TYPE}]})
    assert kept.model_dump(mode='json')['totals'] == [{'key': 'duties', 'on': True}, {'key': 'hours', 'on': False},
                                                      {'key': 'time:N', 'on': True}, {'key': 'type:' + TYPE, 'on': True}]
    for bad in ([{'key': 'hours'}, {'key': 'hours', 'on': False}],  # each once
                [{'key': 'minutes'}], [{'key': 'time:night'}], [{'key': 'type:t1'}], [{'key': 'hours', 'on': 'yes'}],
                [{'key': 'hours', 'colour': 'teal'}],
                [{'key': f'type:{TYPE[:-4]}{n:04x}'} for n in range(s.PLANNER_TOTALS_LIMIT + 1)]):
        with pytest.raises(ValidationError):
            s.PlannerViewSettings.model_validate({'mode': 'person', 'totals': bad})


def run(client_modules, body, data=None):
    script = (f"const t=await import({json.dumps((client_modules/'planner_totals.js').as_uri())});"
              f"const g=await import({json.dumps((client_modules/'generated.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data or {})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


SRC = "{times:g.TIMES_OF_DAY_DEFAULTS,types:[{id:'t1',name:'Ward round'}]}"


def duty(day, start, end, typ='t1', cancelled=False):
    a, b = f'{day}T{start}:00Z', f'{day}T{end}:00Z'
    return {'shift': {'id': day + start, 'start': a, 'end': b, 'assignment_type_id': typ, 'cancelled': cancelled},
            'p': {'start': a, 'end': b}}


# Fri 2 Oct and Sat 3 Oct are the default weekend; Sun 4 Oct is not. Two duties on the Saturday count one weekend day.
DUTIES = [duty('2026-10-02', '08:00', '12:00'), duty('2026-10-03', '08:00', '12:00', 't2'),
          duty('2026-10-03', '22:00', '23:30'), duty('2026-10-04', '13:00', '17:00'),
          duty('2026-10-05', '08:00', '20:00', cancelled=True)]


def test_saved_list_names_and_choices(client_modules):
    got = run(client_modules, f"""({{none:t.totalsOf(null),empty:t.totalsOf([]),kept:t.totalsOf([{{key:'duties'}},{{key:'hours',on:false}}]),
      names:['hours','weekend_days','time:N','time:Z','type:t1','type:gone'].map(k=>t.totalName(k,{SRC})),
      choices:t.totalChoices({SRC}).map(c=>[c.value,c.label,c.group])}})""")
    assert got['none'] == got['empty'] == [{'key': 'hours', 'on': True}]  # Hours alone, as before
    assert got['kept'] == [{'key': 'duties', 'on': True}, {'key': 'hours', 'on': False}]
    assert got['names'] == ['Hours', 'Weekend days', 'Night', '', 'Ward round', '']  # gone: kept, not drawn
    assert got['choices'] == [['hours', 'Hours', 'Measures'], ['duties', 'Duties', 'Measures'],
                              ['weekend_days', 'Weekend days', 'Measures'], ['time:M', 'Morning', 'Times of day'],
                              ['time:A', 'Afternoon', 'Times of day'], ['time:E', 'Evening', 'Times of day'],
                              ['time:N', 'Night', 'Times of day'], ['type:t1', 'Ward round', 'Duty types']]


def test_counts_skip_cancelled_duties(client_modules):
    got = run(client_modules, "Object.fromEntries(['hours','duties','weekend_days','time:M','time:A','time:N','type:t1','type:t2']"
              ".map(k=>[k,t.totalCount(k,i.duties,'UTC',g.TIMES_OF_DAY_DEFAULTS)]))", {'duties': DUTIES})
    assert got == {'hours': 13.5, 'duties': 4, 'weekend_days': 2, 'time:M': 2, 'time:A': 1, 'time:N': 1, 'type:t1': 3, 'type:t2': 1}


def quota(**over):
    return {'person_id': 'p1', 'metric': 'scheduled_hours', 'comparison': 'maximum', 'amount': 40, 'actual': 12,
            'starts_on': '2026-10-01', 'ends_on': '2026-10-08', 'status': 'within', **over}


DAYS = [f'2026-10-0{d}' for d in range(1, 8)]


def test_a_total_shows_the_quota_of_its_measure_over_the_view_days(client_modules):
    rows = [quota(person_id='p2', amount=1), quota(starts_on='2026-09-24', amount=2), quota(ends_on='2026-10-15', amount=3),
            quota(comparison='minimum', amount=4), quota(amount=5), quota(metric='duties', amount=6, actual=7, status='above_maximum'),
            quota(metric='duties', assignment_type_ids=['t1'], amount=8, status='above_target'),
            quota(metric='duties', assignment_type_ids=['t1', 't2'], amount=9), quota(metric='workload_credit', amount=10)]
    got = run(client_modules, """Object.fromEntries(['hours','duties','type:t1','type:t2','weekend_days','time:M']
      .map(k=>{const q=t.quotaOf(k,i.rows,'p1',i.days);return [k,q?[q.amount,t.quotaOver(q)]:null];}))""",
              {'rows': rows, 'days': DAYS})
    # Hours: this person, exactly these days, a maximum before a minimum. Two-type and other metrics measure no total.
    assert got == {'hours': [5, False], 'duties': [6, True], 'type:t1': [8, True], 'type:t2': None,
                   'weekend_days': None, 'time:M': None}
    assert run(client_modules, "t.quotaOf('hours',i.rows,'p1',i.days)?.amount", {'rows': [quota(comparison='minimum', amount=4)], 'days': DAYS}) == 4
    assert run(client_modules, "t.quotaOf('hours',i.rows,'p1',[])??null", {'rows': rows}) is None


def test_cell_words(client_modules):
    assert run(client_modules, "[t.totalText('hours',12.4),t.totalText('hours',12.6,40),t.totalText('duties',3),"
               "t.totalText('duties',3,4),t.totalText('type:t1',2.5,3)]") == ['12 h', '13 / 40 h', '3', '3 / 4', '2.5 / 3']
