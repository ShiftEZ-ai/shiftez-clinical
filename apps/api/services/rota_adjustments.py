"""Stored rota adjustments shared by the unified review and rejection paths."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5


def adjustment_review_items(workspace):
    items = []
    for kind, rows in (('rule_adjustment', workspace.rule_adjustments), ('target_override', workspace.target_overrides)):
        for row in rows or []:
            if row.get('inherited_from_publication_id'):
                continue
            ident = row.get('id') or str(uuid5(NAMESPACE_URL, f"{workspace.id}:{kind}:{row['policy_id']}:{row.get('person_id', '')}"))
            items.append({'id': ident, 'kind': kind, 'name': row.get('policy_name') or row['policy_id'],
                          'reason': row.get('reason'), 'source': deepcopy(row)})
    return items


def inherited_adjustments(publication, field):
    return [{**deepcopy(row), 'inherited_from_publication_id': publication.id}
            for row in publication.snapshot['workspace'].get(field, [])]


def remove_rejected_adjustments(workspace, item_ids):
    """Lifecycle owns authority, current-candidate checks, return-to-draft and ChangeSet Undo."""
    selected = set(item_ids)
    removed = [item for item in adjustment_review_items(workspace) if item['id'] in selected]
    for kind, field in (('rule_adjustment', 'rule_adjustments'), ('target_override', 'target_overrides')):
        rejected = [item['source'] for item in removed if item['kind'] == kind]
        setattr(workspace, field, [row for row in getattr(workspace, field) or [] if row not in rejected])
    return [item['id'] for item in removed]
