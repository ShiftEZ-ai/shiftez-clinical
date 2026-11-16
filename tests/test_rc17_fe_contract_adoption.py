"""The web client reads presentation lists from the generated contracts, never from copies of them ."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
COPIES = {  # file: (contract export it must import, a literal that only a copy would hold)
    'forms.ts': (('RULE_KINDS', 'RULE_HELP', 'RULE_NUMBER_FIELD', 'RULE_TOPICS', 'USUAL_ASSIGNEES_FIXED_DEFAULT', 'WEEKDAY_SHORT_LABELS'),
                 ("'Minimum rest','Require a break", "min_rest:'After a duty ends", "{name:'Staffing & cover'", 'USUAL_ASSIGNEES_FIXED_DEFAULT=true')),
    'findings.ts': (('FINDING_MESSAGES',), ("COVERAGE_GAP:'Fewer people",)),
    'generation_options.ts': (('SCORE_WEIGHT_LABELS', 'SCORE_WEIGHT_HELP', 'STRENGTH_LABELS'), ("['coverage','Coverage']", "['low','Low']")),
    'recurring_sessions.ts': (('WEEKDAY_SHORT_LABELS',), ("['Mon','Tue','Wed'",)),
}


def test_presentation_lists_come_from_the_generated_contracts():
    for name, (exports, literals) in COPIES.items():
        text = (SRC / name).read_text(encoding='utf-8')
        imported = ' '.join(re.findall(r"import \{([^}]*)\} from './generated\.js'", text))
        assert all(e in imported for e in exports), (name, exports)
        assert not [lit for lit in literals if lit in text], name
