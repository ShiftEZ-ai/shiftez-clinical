"""Fix list wording of who is missing: a noun takes its plural, a person's name never does (sample person and rank
names are synthetic labels, not human names)."""
import json

from packages.contracts.rule_catalogue import FIX_LIST_WORDS
from packages.domain.fix_list_facts import presentation, who_words

PERSON = 'Member 001 (test)'
DATA = {'people': {'p1': {'id': 'p1', 'display_name': PERSON}}, 'categories': {'c1': {'id': 'c1', 'name': 'Fellow'}}}


def one(field, value):
    return {'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': [value]}]}


def test_a_name_is_never_inflected_and_a_noun_is():
    assert who_words(DATA, one('person_ids', 'p1')) == (PERSON, PERSON)
    assert who_words(DATA, one('category_ids', 'c1')) == ('Fellow', 'Fellows')
    # Everyone reads "person" / "people", never "persons".
    assert who_words(DATA, None) == (FIX_LIST_WORDS['person'], FIX_LIST_WORDS['people'])
    assert who_words(DATA, {'match': 'all', 'conditions': []}) == ('person', 'people')


def missing(who):
    words = who_words(DATA, who)
    context = {'sources': {json.dumps(['r1', 0, None]): {'who_label': words[0], 'who_many': words[1]}},
               'sessions': {'s1': {'assignment_type_name': 'Day duty', 'service_names': ['Clinic A'], 'weekday': 0}}}
    evidence = [{'code': 'COVERAGE_GAP', 'rule_id': 'r1', 'detail': {'line': 0, 'available': 0, 'required': 2}}]
    return presentation(context, {}, evidence, {'s1'}, set(), {'missing': '{where} need {missing}'}, level='must', family='')


def test_the_fix_list_sentence_keeps_a_persons_name_whole():
    shown, sentence = missing(one('person_ids', 'p1'))
    assert shown['detail_line'] == f'1 session · each has 0 of 2 {PERSON}'
    assert sentence.endswith(f'need 2 more {PERSON}')
    assert PERSON + 's' not in sentence + shown['detail_line']
    shown, sentence = missing(one('category_ids', 'c1'))
    assert shown['detail_line'] == '1 session · each has 0 of 2 Fellows' and sentence.endswith('need 2 more Fellows')
    shown, sentence = missing(None)
    assert sentence.endswith('need 2 more people') and 'persons' not in shown['detail_line']
