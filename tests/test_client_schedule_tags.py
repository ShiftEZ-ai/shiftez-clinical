"""Person tags on the Schedule, from the real compiled schedule_dimensions.ts in Node (logic only): the membership index
is built once from tag records and member links (active people tags only), and Colour by tag takes the first tag in the
viewer's chosen order that the person holds — never a tag they happen to have first, neutral when none matches."""
import json
import subprocess


def run(client_modules, body, data):
    script = (f"const m=await import({json.dumps((client_modules/'schedule_dimensions.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


CATALOGUES = {
    'tags': [{'id': 'night', 'name': 'Night team', 'labels': ['people']}, {'id': 'acls', 'name': 'ACLS', 'labels': ['people', 'services']},
             {'id': 'old', 'name': 'Old', 'labels': ['people'], 'active': False}, {'id': 'clinic', 'name': 'Clinic', 'labels': ['services']}],
    'tag_members': [{'tag_id': 'night', 'kind': 'people', 'member_id': 'p1'}, {'tag_id': 'acls', 'kind': 'people', 'member_id': 'p1'},
                    {'tag_id': 'acls', 'kind': 'people', 'member_id': 'p1'}, {'tag_id': 'old', 'kind': 'people', 'member_id': 'p2'},
                    {'tag_id': 'clinic', 'kind': 'services', 'member_id': 'p1'}, {'tag_id': 'acls', 'kind': 'services', 'member_id': 'p3'}],
}


def test_index_holds_active_people_tags_once_in_name_order(client_modules):
    got = run(client_modules, "Object.fromEntries([...m.personTags(i)].map(([k,v])=>[k,v.map(t=>t.id)]))", CATALOGUES)
    # Archived and non-people tags are left out; a repeated link counts once; a service member is not a person.
    assert got == {'p1': ['acls', 'night']}
    assert run(client_modules, "m.personTags({}).size", {}) == 0  # staff hold no lists: nothing, never an error


def test_colour_tag_is_the_first_chosen_one_held(client_modules):
    body = "(()=>{const held=m.personTags(i.c).get('p1');return i.orders.map(o=>m.priorityTag(held,o)?.id??null);})()"
    got = run(client_modules, body, {'c': CATALOGUES, 'orders': [['night', 'acls'], ['acls', 'night'], ['missing', 'night'], ['clinic'], []]})
    # The viewer's order decides; an unknown or unheld id is skipped; nothing chosen or held is neutral (null).
    assert got == ['night', 'acls', 'night', None, None]
    assert run(client_modules, "m.priorityTag(undefined,['night'])??null", {}) is None
