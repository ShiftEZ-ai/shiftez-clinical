"""System › Imports: a saved session import's source evidence reads as Yes/No and a status, not raw source values."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def test_saved_import_evidence_reads_yes_no_and_mapped(browser, live_server, h):
    from .test_operations import csv_source, batch
    typ = h.configure('assignment_types', {'name': 'Evidence import duty', 'starts_on': '2020-01-01',
                                              'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    w = h.workspace()
    batch(h, w, csv_source([['source type', 'source location', 'Physical', 'Person A',
                             '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', 'false']]))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'system/imports')
        page.get_by_role('tab', name='Rota sessions', exact=True).click()
        page.get_by_role('button', name='Review row mappings', exact=True).click()
        dialog = page.get_by_role('dialog')
        for field, value in {'assignment type': typ['id'], 'location': h.seed['locations'][0],
                             'service': h.seed['services'][0], 'person': h.seed['people'][0]}.items():
            dialog.get_by_label('Row 2 ' + field, exact=True).select_option(value)
        dialog.get_by_label('Reconciliation reason', exact=True).fill('Reviewed evidence import')
        dialog.get_by_role('button', name='Save reconciled draft', exact=True).click()
        expect(dialog).not_to_be_visible()
        page.get_by_role('button', name='Inspect source evidence', exact=True).click()
        evidence = page.get_by_role('dialog', name='Import source evidence')
        cells = evidence.locator('tbody td')
        expect(evidence.locator('th').filter(has_text=re.compile('^Status$'))).to_have_count(1)
        expect(evidence.locator('tbody .badge')).to_have_text('Mapped')
        expect(cells.filter(has_text=re.compile('^(Yes|No)$'))).to_have_count(2)
        expect(cells.filter(has_text=re.compile('^(true|false)$'))).to_have_count(0)
        expect(evidence).not_to_contain_text('Mapped to a draft session')
    finally:
        context.close()
