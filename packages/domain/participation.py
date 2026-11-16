"""One duty reservation, explicit planned presence; never actual attendance/payroll.

Off-cover gaps do not release a person for another duty, count as between-duty
rest or relax existing reserved-hours limits. Coverage and scheduled-v2 presence
hours use the same intervals. Earlier snapshots without segments remain valid.
"""
from datetime import timedelta
from functools import cache
from packages.contracts.schemas import ParticipantInput
from .time import aware


@cache
def _presence_intervals(person_id, start, end, participation, overtime, segments):
    segment_values = []
    for kind, value in segments:
        segment_values.append(dict(value) if kind == 'dict' else value)
    value = ParticipantInput.model_validate({'person_id': person_id, 'start': start, 'end': end,
                                             'participation': participation or 'staff', 'overtime': bool(overtime),
                                             'segments': segment_values})
    return tuple((part.start, part.end) for part in value.segments) or ((value.start, value.end),)


def presence_intervals(participant: dict) -> list[tuple]:
    raw_segments = participant.get('segments', [])
    segments = (tuple(('dict', tuple(segment.items())) for segment in raw_segments)
                if isinstance(raw_segments, list) and all(isinstance(segment, dict) for segment in raw_segments)
                else (('invalid', repr(raw_segments)),))
    return list(_presence_intervals(participant.get('person_id'), participant.get('start'), participant.get('end'),
                                   participant.get('participation'), participant.get('overtime'), segments))


def shifted_segments(participant: dict, offset: timedelta) -> list[dict]:
    # A missing field denotes the earlier contiguous contract, not guessed gaps.
    return [{"start": (aware(part["start"]) + offset).isoformat(),
             "end": (aware(part["end"]) + offset).isoformat()}
            for part in participant.get("segments", [])]


def replaced_participants(participants, replacements, *, redirect_supervision=True):
    """Replace occupants and redirect supervision links, optionally through a qualification filter."""
    people = {}
    for participant in participants:
        if participant['id'] not in replacements:
            continue
        old, new = participant['person_id'], replacements[participant['id']]
        if redirect_supervision is True or (callable(redirect_supervision)
                                            and redirect_supervision(participant, old, new)):
            people[old] = new
    return [{**p, 'person_id': replacements.get(p['id'], p['person_id']),
             'supervisor_person_id': people.get(p.get('supervisor_person_id'), p.get('supervisor_person_id'))}
            for p in participants]
