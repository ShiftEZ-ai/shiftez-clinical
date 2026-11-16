"""The auto-schedule live view's logic (apps/web/src/autoschedule_motion.ts), run in Node from the compiled client:
how changes between two schedules pair up, how Hybrid classifies them, the frame batching and its cost at scale."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {'mode': 'hybrid', 'big_move': {'min_row_distance': 4, 'swaps': True, 'fills_from_open': True, 'cross_session': True},
            'big_style': 'fly', 'small_style': 'roll', 'max_concurrent_flights': 16, 'max_updates_per_second': 3,
            'step_change': 'roll', 'speed': 1}


@pytest.fixture(scope='module')
def motion(client_modules):
    node = shutil.which('node')
    folder = client_modules

    def run(body, payload=None):
        script = (f"const m=await import({json.dumps((folder / 'autoschedule_motion.js').as_uri())});"
                  f"const input=JSON.parse(process.argv[1]);const out=(()=>{{{body}}})();console.log(JSON.stringify(out));")
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(payload)],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return run


def ops(motion, shown, nxt, days=None):
    """Pairs as 'shift|person'; every shift is on day 1 unless `days` says otherwise."""
    return motion("return m.diff(new Set(input.shown),new Set(input.next),s=>(input.days||{})[s]||'d1');",
                  {'shown': shown, 'next': nxt, 'days': days})


def test_changes_pair_into_moves_not_a_leave_and_a_fill(motion):
    assert ops(motion, ['s1|a', 's2|b'], ['s1|b', 's2|a']) == [{'t': 'swap', 's1': 's1', 's2': 's2', 'a': 'a', 'b': 'b'}]
    assert ops(motion, ['s1|a'], ['s2|a']) == [{'t': 'shift', 'p': 'a', 'from': 's1', 'to': 's2'}]
    assert ops(motion, ['s1|a'], ['s1|b']) == [{'t': 'person', 's': 's1', 'from': 'a', 'to': 'b'}]
    assert ops(motion, [], ['s1|a']) == [{'t': 'fill', 's': 's1', 'p': 'a'}]
    assert ops(motion, ['s1|a'], []) == [{'t': 'leave', 's': 's1', 'p': 'a'}]
    assert ops(motion, ['s1|a'], ['s1|a']) == []


def test_only_the_same_day_pairs(motion):
    # a person works one place a day: moving to another day's session is a leave and a fill
    days = {'mon': 'd1', 'tue': 'd2'}
    assert sorted(o['t'] for o in ops(motion, ['mon|a'], ['tue|a'], days)) == ['fill', 'leave']
    assert sorted(o['t'] for o in ops(motion, ['mon|a', 'tue|b'], ['mon|b', 'tue|a'], days)) == ['person', 'person']


def test_every_change_is_accounted_for_once(motion):
    shown = [f's{i}|p{i}' for i in range(40)]
    nxt = [f's{i}|p{(i * 7 + 3) % 45}' for i in range(40)] + ['s99|p1']
    result = motion("""const shown=new Set(input.shown),next=new Set(input.next),ops=m.diff(shown,next,()=>'d');
      const after=new Set(shown);
      for(const o of ops){
        if(o.t==='swap'){after.delete(m.key(o.s1,o.a));after.delete(m.key(o.s2,o.b));after.add(m.key(o.s1,o.b));after.add(m.key(o.s2,o.a));}
        else if(o.t==='shift'){after.delete(m.key(o.from,o.p));after.add(m.key(o.to,o.p));}
        else if(o.t==='person'){after.delete(m.key(o.s,o.from));after.add(m.key(o.s,o.to));}
        else if(o.t==='fill')after.add(m.key(o.s,o.p));else after.delete(m.key(o.s,o.p));}
      return {same:[...after].sort().join()===[...next].sort().join()};""", {'shown': shown, 'next': nxt})
    assert result['same']


@pytest.mark.parametrize('settings,op,view,rows,reduced,style', [
    ({}, {'t': 'swap', 's1': 'x', 's2': 'y', 'a': 'a', 'b': 'b'}, 'people', 0, False, 'fly'),
    ({}, {'t': 'fill', 's': 'x', 'p': 'a'}, 'people', 0, False, 'fly'),
    ({}, {'t': 'person', 's': 'x', 'from': 'a', 'to': 'b'}, 'people', 4, False, 'fly'),     # at the row distance: big
    ({}, {'t': 'person', 's': 'x', 'from': 'a', 'to': 'b'}, 'people', 3, False, 'roll'),    # nearer: in place
    ({}, {'t': 'leave', 's': 'x', 'p': 'a'}, 'people', 0, False, 'roll'),
    ({}, {'t': 'shift', 'p': 'a', 'from': 'x', 'to': 'y'}, 'people', 0, False, 'roll'),     # same cell in People
    ({}, {'t': 'shift', 'p': 'a', 'from': 'x', 'to': 'y'}, 'week', 0, False, 'fly'),        # another row in Week
    ({}, {'t': 'fill', 's': 'x', 'p': 'a'}, 'week', 0, False, 'roll'),
    ({'big_move': {'min_row_distance': 4, 'swaps': False, 'fills_from_open': True, 'cross_session': True}},
     {'t': 'swap', 's1': 'x', 's2': 'y', 'a': 'a', 'b': 'b'}, 'people', 0, False, 'roll'),
    ({'big_move': {'min_row_distance': 4, 'swaps': True, 'fills_from_open': False, 'cross_session': True}},
     {'t': 'fill', 's': 'x', 'p': 'a'}, 'people', 0, False, 'roll'),
    ({'big_style': 'fade', 'small_style': 'retype'}, {'t': 'fill', 's': 'x', 'p': 'a'}, 'people', 0, False, 'fade'),
    ({'big_style': 'fade', 'small_style': 'retype'}, {'t': 'leave', 's': 'x', 'p': 'a'}, 'people', 0, False, 'retype'),
    ({'mode': 'fly'}, {'t': 'person', 's': 'x', 'from': 'a', 'to': 'b'}, 'people', 1, False, 'fly'),
    ({'mode': 'fly'}, {'t': 'leave', 's': 'x', 'p': 'a'}, 'people', 0, False, 'fade'),
    ({'mode': 'retype'}, {'t': 'swap', 's1': 'x', 's2': 'y', 'a': 'a', 'b': 'b'}, 'people', 0, False, 'retype'),
    ({'mode': 'off'}, {'t': 'fill', 's': 'x', 'p': 'a'}, 'people', 0, False, 'none'),
    ({}, {'t': 'swap', 's1': 'x', 's2': 'y', 'a': 'a', 'b': 'b'}, 'people', 0, True, 'none'),  # reduced motion: settles at once, never moves
    ({'mode': 'fly'}, {'t': 'fill', 's': 'x', 'p': 'a'}, 'people', 0, True, 'none'),
])
def test_hybrid_classification_follows_the_settings(motion, settings, op, view, rows, reduced, style):
    s = {**DEFAULTS, **settings}
    assert motion('return m.styleOf(input.op,{settings:input.s,view:input.view,rows:input.rows,reduced:input.reduced});',
                  {'op': op, 's': s, 'view': view, 'rows': rows, 'reduced': reduced}) == style
    if style == 'fly':
        assert motion('return m.fallback(input);', s) in ('roll', 'fade', 'retype')


def test_one_frame_reads_all_then_writes_all(motion):
    out = motion("""const log=[],frames=[];const f=m.frameBatcher(run=>frames.push(run));
      for(const n of ['a','b','c'])f.add({first:()=>log.push('first '+n),write:()=>log.push('write '+n),last:()=>log.push('last '+n),play:()=>log.push('play '+n)});
      f.add({write:()=>log.push('write d')});
      const queued=frames.length,pending=f.pending;frames.shift()();
      f.add({write:()=>log.push('next')});
      return {queued,pending,log,again:frames.length};""")
    assert out['queued'] == 1 and out['pending'] == 4  # one animation frame for the whole batch
    assert out['log'] == ['first a', 'first b', 'first c', 'write a', 'write b', 'write c', 'write d',
                          'last a', 'last b', 'last c', 'play a', 'play b', 'play c']
    assert out['again'] == 1  # work added after the frame waits for the next one


def test_pairing_at_department_scale_is_fast(motion):
    # 150 people × 500 sessions a month; a better schedule that moves 200 places (the event cap) — well inside a frame.
    out = motion("""const shown=new Set(),next=new Set();
      for(let s=0;s<500;s++)for(let k=0;k<3;k++){shown.add(m.key('s'+s,'p'+((s*3+k)%150)));}
      for(const x of shown)next.add(x);let i=0;
      for(const x of [...shown].slice(0,200)){next.delete(x);next.add(x.split('|')[0]+'|p'+((i++*11)%150));}
      const t=performance.now();let ops;for(let r=0;r<20;r++)ops=m.diff(shown,next,s=>'d'+(Number(s.slice(1))%30));
      return {ms:(performance.now()-t)/20,count:ops.length};""")
    assert out['count'] > 0
    assert out['ms'] < 16, out


def test_arc(motion):
    arc = motion("const a=m.arcFrames(-200,80,1,1.5);return {n:a.frames.length,first:a.frames[0].transform,last:a.frames.at(-1).transform,d:a.duration,"
                 "keys:[...new Set(a.frames.flatMap(f=>Object.keys(f)))]};")
    assert arc['n'] == 15 and arc['keys'] == ['transform']  # transform only
    assert arc['first'].startswith('translate(-200.0px,80.0px)') and arc['last'].startswith('translate(0.0px,0.0px)')
    assert 480 * 1.5 <= arc['d'] <= 900 * 1.5


def test_a_frame_plays_the_leading_changes_on_different_sessions(motion):
    """The live stream plays changes from several better schedules in order; one on a session an earlier change in the
    same frame touches waits for the next frame (each change finds its chips before the frame writes)."""
    out = motion("const f=(s,p)=>({t:'fill',s,p}),l=(s,p)=>({t:'leave',s,p});"
                 "return [m.runOf([f('s1','a'),f('s2','b'),{t:'swap',s1:'s3',s2:'s4',a:'c',b:'d'}]),"
                 "m.runOf([f('s1','a'),{t:'person',s:'s2',from:'b',to:'c'},l('s1','a'),f('s3','e')]),"
                 "m.runOf([{t:'shift',p:'a',from:'s1',to:'s2'},f('s2','b')]),m.runOf([])];")
    assert out == [3, 2, 1, 0]
