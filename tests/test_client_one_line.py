"""People › Where is everyone's line (apps/web/src/timeline.ts `layoutRow`, one line), run in Node from the compiled
client (docs/design/people-timeline-marks.md): a stretch too narrow for any word folds into the stretch it touches
(Short stretches off) or is drawn thin (on); a stretch that continues the one before it (only published/draft
differs) joins it and the run says its words once."""
from .test_client_impact_and_drag import run_with_listeners

# January 2027 over 310 px: 10 px a day.
LAYOUT = ("(()=>{const d=m.dayNumber('2027-01-01');return m.layoutRow(input.row,[d,d+31],310,false,true,input.fold)"
          ".placed.map(p=>[p.bar.id,p.bar.start,p.bar.end,p.mode,!!p.joined,p.bar.aria])})()")


def bar(bid, start, end, label, style='block', **extra):
    return {'id': bid, 'start': start, 'end': end, 'label': label, 'short': label, 'style': style, 'hue': 'teal' if style == 'block' else None,
            'aria': f'{label} {start}', **extra}


def layout(client_modules, bars, fold):
    return run_with_listeners(client_modules, 'timeline.js', LAYOUT, {'row': {'id': 'p', 'name': 'P', 'bars': bars}, 'fold': fold})


WORK = [bar('w1', '2027-01-01', '2027-01-20', 'UCC'), bar('sick', '2027-01-20', '2027-01-21', 'Sick leave', 'leave', code='SL'),
        bar('w2', '2027-01-21', '2027-02-01', 'UCC')]


def test_a_one_day_leave_folds_into_the_stretch_before_and_that_stretch_joins_the_next(client_modules):
    placed = layout(client_modules, WORK, True)
    assert [(p[0], p[1], p[2], p[3], p[4]) for p in placed] == [
        ('w1', '2027-01-01', '2027-01-21', 'in', False), ('w2', '2027-01-21', '2027-02-01', 'quiet', True)]
    assert 'also Sick leave 2027-01-20' in placed[0][5]  # said, and listed in its card


def test_short_stretches_on_draws_it_thin_and_nothing_joins_across_it(client_modules):
    placed = layout(client_modules, WORK, False)
    assert [(p[0], p[3], p[4]) for p in placed] == [('w1', 'in', False), ('sick', 'thin', False), ('w2', 'in', False)]


def test_published_then_draft_of_the_same_area_is_one_joined_run_with_its_words_once(client_modules):
    bars = [bar('pub', '2027-01-01', '2027-01-15', 'FM'), bar('draft', '2027-01-15', '2027-02-01', 'FM', draft=True)]
    placed = layout(client_modules, bars, True)
    assert [(p[0], p[3], p[4]) for p in placed] == [('pub', 'in', False), ('draft', 'quiet', True)]


def test_a_lone_sliver_with_nothing_beside_it_is_left_out_when_folding(client_modules):
    placed = layout(client_modules, [bar('sick', '2027-01-10', '2027-01-11', 'Sick leave', 'leave', code='SL')], True)
    assert placed == []
