"""The one-line day precedence and period area selection, shared by all timeline consumers."""
from collections import Counter, defaultdict
from datetime import date, timedelta
import re

DAY = timedelta(days=1)


def short_label(name, explicit='', *, academic=False):
    if explicit and explicit.strip():
        return explicit.strip()
    words = re.findall(r'\w+', name, flags=re.UNICODE)
    if not words:
        return name
    if len(words) > 1 and not academic:
        return ''.join(word[0].upper() for word in words)
    # One word stays whole up to 8 letters (Annual, Business); academic words keep their 4-letter form (Acad).
    return words[0] if len(words[0]) <= (5 if academic else 8) else words[0][:4]


def period(day, bucket):
    if bucket == 'day':
        return day
    if bucket == 'week':
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def project(start, end, bucket, work, spans, areas, clinics, roster, *, show_work=True, area_share=25, max_areas=2):
    """Bounded in-memory day sweep after SQL aggregation; no database calls or raw assignment expansion.

    Work fills open days of its period. Precedence masks days without moving counts to other days.
    Identity excludes counts and the rota: one stretch spans the rotas it crosses; `rotas` names them in order and
    `rota` stays the first (rota names may themselves contain " · ", so they are never joined into one string).
    `area_share` (percent of a period's duties) and `max_areas` are the department's (WhereaboutsSettings).
    """
    buckets = defaultdict(lambda: {'areas': Counter(), 'clinics': Counter(), 'duties': 0})
    totals = Counter()
    for day, row in work.items():
        current = buckets[period(day, bucket)]
        current['areas'].update(row['areas'])
        current['clinics'].update(row['clinics'])
        current['duties'] += row.get('duties', sum(row['areas'].values()))
        totals.update(row['areas'])
    selected = {}
    day = start
    while day < end:
        key = period(day, bucket)
        if key not in selected:
            stop = day
            states = Counter()
            rotas = defaultdict(Counter)
            while stop < end and period(stop, bucket) == key:
                source, rota = roster.get(stop, ('usual', None))
                states[source] += 1
                rotas[source][rota] += 1
                stop += DAY
            # A strict published majority; draft wins a tie. Usual only fills rota-free months.
            source = ('published' if states['published'] * 2 > sum(states.values()) else
                      'draft' if states['draft'] else 'usual' if states['usual'] else 'published')
            rota = rotas[source].most_common(1)[0][0] if rotas[source] else None
            counts = buckets[key]['areas']
            ranked = sorted(counts, key=lambda identifier: (-counts[identifier], identifier))
            denominator = buckets[key]['duties']
            top = [identifier for identifier in ranked if counts[identifier] * 100 >= denominator * area_share][:max_areas]
            if ranked and not top:
                top = ranked[:1]
            # Which areas: by share. Their order: the department's area order (name order, as the timeline's hues and key),
            # so the same two areas always read "AA · ZZ", never "ZZ · AA" in a month where ZZ led reviewed.
            top = sorted(top, key=lambda identifier: (areas[identifier]['short_name'].casefold(), areas[identifier]['label'].casefold(), identifier))
            selected[key] = (top, source, rota)
        day += DAY
    by_day = defaultdict(dict)
    order = {'outside': 0, 'leave': 1, 'academic': 2, 'protected': 3, 'away': 4, 'work': 5, 'home': 6}
    for span in sorted(spans, key=lambda row: (order[row['kind']], row['id'])):
        day = max(start, span['starts_on'])
        while day < min(end, span['ends_on']):
            by_day[day].setdefault(span['kind'], {k: v for k, v in span.items() if k not in {'starts_on', 'ends_on'}})
            day += DAY
    result = []
    day = start
    previous_identity = None
    while day < end:
        top, source, rota = selected[period(day, bucket)]
        options = by_day.get(day, {})
        row = next((options[kind] for kind in ('outside', 'leave', 'academic', 'protected', 'away') if kind in options), None)
        if row is None and top and show_work:
            chosen = [areas[identifier] for identifier in top]
            row = {'kind': 'work', 'label': ' · '.join(a['label'] for a in chosen),
                   'short_label': ' · '.join(a['short_name'] for a in chosen), 'areas': chosen,
                   'source': source, 'rota': rota}
        if row is None:
            row = options.get('home')
        if row is not None:
            row = dict(row)
            row.setdefault('source', source)
            row.setdefault('rota', rota)
            identity = repr(sorted((k, v) for k, v in row.items() if k not in {'id', 'rota'}))
            if result and previous_identity == identity and result[-1]['ends_on'] == day:
                segment = result[-1]
                segment['ends_on'] = day + DAY
                if row['rota'] and row['rota'] not in segment['rotas']:
                    segment['rotas'].append(row['rota'])
            else:
                segment = {**row, 'id': f"{row['kind']}:{day.isoformat()}", 'starts_on': day, 'ends_on': day + DAY,
                           'clinics': [], 'rotas': [row['rota']] if row['rota'] else []}
                result.append(segment)
            if row['kind'] == 'work':
                counts = {r['id']: r['count'] for r in segment['clinics']}
                for identifier, count in work.get(day, {}).get('clinics', {}).items():
                    counts[identifier] = counts.get(identifier, 0) + count
                segment['clinics'] = [{'id': identifier, 'label': clinics[identifier]['label'], 'count': count}
                                      for identifier, count in sorted(counts.items())]
            previous_identity = identity
        else:
            previous_identity = None
        day += DAY
    window = [{k: areas[identifier][k] for k in ('id', 'label', 'short_name')} | {'count': count}
              for identifier, count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))]
    return {'segments': result, 'areas': window}
