"""maintenance (ME-26, ME-28): an amendment sent from a published change list says, where the approver decides, each changed
session's change and the coordinator's reason (change_diff.sessions[].changes), and the approver's Home names it as an
amendment with its changes and reasons."""
import pytest

from .test_published_changes import change, issued

pytestmark = pytest.mark.postgres


@pytest.fixture
def h(pg):
    return pg


def sent(h):
    pub, shifts = issued(h)
    made = change(h, pub, shifts[0], absence_reason='Sick')
    return h.command('coordinator', 'publication.changes.submit', {'reason': 'Sick cover for Monday'},
                     target=pub, revision=made['workspace']['revision'])


def test_each_changed_session_names_its_change_and_reason_for_the_approver(h):
    result = sent(h)
    review = h.get('approver', '/api/workspaces/' + result['workspace']['id'])['change_diff']
    assert [row['changes'] for row in review['sessions']] == [[{'kind': 'replace', 'absence_reason': 'sick'}]]
    # The coordinator's hidden list (not yet sent) names its own unsent change the same way.
    assert all('changes' in row for row in result['change_diff']['sessions'])


def test_the_approvers_home_says_the_rota_to_review_is_an_amendment_with_its_reasons(h):
    sent(h)
    item = next(i for i in h.get('approver', '/api/home')['attention'] if i['key'] == 'reviews')
    assert item['amendment'] == {'changes': 1, 'reasons': ['sick']}
