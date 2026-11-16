"""Read-only preparation for native portable exports, sharing the v1 archive gate.

Creation DTOs validate shape, not historical restore semantics or command authority.
This module deliberately has no database connection or command execution path.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import os
from uuid import UUID

from pydantic import ValidationError
from apps.api import models as m
from packages.contracts import schemas as s


FORMAT = 'fmuc-canonical-raw-input-seed-v2'
STAFFING_FIELDS = {'default_person_ids', 'usual_assignees_fixed', 'close_when_all_usual_unavailable'}
STAFFING_PATHS = {'recurring_series': '$.definition', 'shifts': '$.series_snapshot.definition',
                 'definition_history': '$.snapshot.definition'}
CONTRACTS = {**s.CATALOGUE_SCHEMAS, 'memberships': s.MembershipInput,
    'eligibilities': s.EligibilityInput, 'capability_grants': s.GrantInput,
    'identities': s.IdentityInput, 'role_bindings': s.RoleInput, 'policies': s.PolicyInput,
    'workspaces': s.WorkspaceInput, 'shifts': s.ShiftInput, 'availability': s.AvailabilityInput,
    'availability_patterns': s.AvailabilityPatternInput, 'table_views': s.TableViewInput,
    'self_approval_permissions': s.SelfApprovalInput, 'staff_wishes': s.StaffWishInput,
    'review_routes': s.ReviewRouteInput, 'review_delegations': s.ReviewDelegationInput,
    'planner_views': s.PlannerViewInput, 'wish_overrides': s.WishOverrideInput,
    'scheduling_facts': s.SchedulingFactInput}
CONTEXT = {
    'organisations': 'Source institution context; reconcile with the existing installation',
    'departments': 'Source settings context; current environment and approval settings require reconciliation',
    'identities': 'Source identity evidence; no sign-in creation, local account replacement or inferred staff links',
    'accounts': 'Source account metadata only; credentials and authentication are not exported',
    'role_bindings': 'Historical access evidence; source grants do not authorize target access',
    'self_approval_permissions': 'Historical permission evidence; no target exception permission inferred',
    'policies': 'Rule history/approval evidence; use target proposal and independent activation workflows',
    'policy_seed_lines': 'Rule history evidence; no target activation inferred',
    'policy_exceptions': 'Historical exception evidence; no target approval inferred',
    'rule_source_bindings': 'Historical conversion evidence; target activation lineage requires reconciliation',
    'definition_history': 'Immutable source history; never replace target history or revisions',
    'source_mappings': 'Immutable source provenance; never install source target UUIDs as target mappings',
    'workspaces': 'Source lifecycle context; missing allocations/publications prevent lifecycle restoration',
    'shifts': 'Saved session structure only; workspace context and immutable type/series history require reconciliation',
    'shift_services': 'Session-clinic link evidence; shared sessions must remain one session',
    'table_views': 'Saved source-owner view evidence; no target owner identity inferred',
    'availability_patterns': 'Historical decision evidence; target approval workflow required',
    'availability': 'Saved availability evidence; decisions, retractions and omitted request lineage require reconciliation',
    'staff_wishes': 'Saved wish evidence; source decisions and omitted publication lineage require reconciliation',
    'tag_members': 'Typed member relationships need target ID mapping and the shared tag command',
    'rotation_attachments': 'Historical attachment relationships need target ID mapping',
    'location_closures': 'Closure relationships need target ID mapping',
    'event_occurrences': 'Saved event occurrence context; no attendance or target lifecycle inferred',
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def inspect_package(archive, manifest, archive_hash, decode):
    """Validate native column inventory and provenance before producing candidates."""
    documents = {f['path']: decode(archive.read(f['path'])) for f in manifest['files']
                 if f['path'].endswith('.json')}
    raw, spec = documents['raw_inputs.json'], documents['READ_ONLY_DEV_EXPORT_SPEC.json']
    if manifest.get('version') != 2 or raw.get('format') != 'fmuc-canonical-raw-inputs-v2':
        raise ValueError('Unsupported native portable schema')
    for document in documents.values():
        if isinstance(document, dict):
            for field in ('source_pin', 'snapshot_id'):
                if field in document and document[field] != manifest[field]:
                    raise ValueError('Native provenance differs: ' + field)
    if raw['source_pin'] != spec['target']['source_pin'] or spec['version'] != 2:
        raise ValueError('Native specification provenance differs')
    tables = raw['tables']
    selected = {t['table']: t for t in spec['selected_tables']}
    if (len(selected) != len(spec['selected_tables']) or set(tables) != set(selected)
            or set(tables) != set(manifest['selected_counts'])
            or manifest['selected_counts'] != documents['snapshot_receipt.json']['selected_counts']):
        raise ValueError('Native table inventory differs')
    metadata = documents['COLUMN_METADATA.json']
    columns = {(c['table_name'], c['column_name']): c for c in metadata}
    if len(columns) != len(metadata) or set(columns) != {
            (t, col) for t, entry in selected.items() for col in entry['columns']}:
        raise ValueError('Native column metadata differs')
    indexed = {}
    for table, rows in tables.items():
        if table not in CONTRACTS and table not in CONTEXT:
            raise ValueError('Unsupported native table: ' + table)
        target = m.Base.metadata.tables.get(table)
        if target is None or not set(selected[table]['columns']) <= set(target.columns.keys()):
            raise ValueError('Unrecognized target columns: ' + table)
        if not isinstance(rows, list) or len(rows) != manifest['selected_counts'][table]:
            raise ValueError('Native table count differs: ' + table)
        indexed[table] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(selected[table]['columns']):
                raise ValueError('Native row columns differ: ' + table)
            for col, value in row.items():
                meta = columns[table, col]
                if value is None:
                    if meta['is_nullable'] == 'NO':
                        raise ValueError('Missing non-null native field: ' + table + '.' + col)
                    continue
                kind = meta['data_type']
                valid = (type(value) is bool if kind == 'boolean' else
                         type(value) is int if kind in {'integer', 'bigint'} else
                         isinstance(value, str) if kind in {'character varying', 'text', 'date', 'timestamp with time zone'} else
                         type(value) in (int, float) if kind in {'double precision', 'numeric'} else
                         True if kind in {'json', 'jsonb'} else False)
                if not valid:
                    raise ValueError('Unsupported native field type: ' + table + '.' + col)
                if kind == 'date':
                    date.fromisoformat(value)
                if kind == 'timestamp with time zone' and datetime.fromisoformat(value).utcoffset() is None:
                    raise ValueError('Native timestamp needs timezone')
            if 'id' in row:
                UUID(row['id'])
                if row['id'] in indexed[table]:
                    raise ValueError('Duplicate native UUID: ' + table)
                indexed[table][row['id']] = row
    if sum(map(len, tables.values())) != manifest['raw_record_count']:
        raise ValueError('Native total count differs')
    checked = 0
    for table, rows in tables.items():
        for col in m.Base.metadata.tables[table].columns:
            for fk in col.foreign_keys:
                parent, field = fk.target_fullname.split('.')
                for row in rows:
                    if row.get(col.name) is not None:
                        if field != 'id' or row[col.name] not in indexed.get(parent, {}):
                            raise ValueError('Unresolved native reference: ' + table + '.' + col.name)
                        checked += 1
    return {'format': FORMAT, 'archive_sha256': archive_hash, 'package_manifest': manifest,
            'dataset': raw, 'documents': documents, 'declared_reference_values_checked': checked}


def recombine(package):
    """Recombine saved fields at exact paths on a copy; retain original evidence unchanged."""
    tables = deepcopy(package['dataset']['tables'])
    indexed = {t: {r['id']: r for r in rows if 'id' in r} for t, rows in tables.items()}
    document = package['documents']['staffing_defaults.json']
    entries = document['entries']
    if len(entries) != package['package_manifest']['separate_staffing_default_fragments']:
        raise ValueError('Staffing fragment count differs')
    seen = set()
    for entry in entries:
        table, key, path = entry['table'], entry['row_id'], entry['path']
        if STAFFING_PATHS.get(table) != path or (table, key, path) in seen:
            raise ValueError('Unsupported or duplicate staffing path')
        seen.add((table, key, path))
        values, presence = entry['values'], entry['saved_field_presence']
        if (set(values) != set(presence) or len(presence) != len(set(presence))
                or not set(values) <= STAFFING_FIELDS):
            raise ValueError('Staffing field presence differs')
        try:
            target = indexed[table][key]
            for part in path[2:].split('.'):
                target = target[part]
        except (KeyError, TypeError):
            raise ValueError('Missing staffing target path') from None
        if not isinstance(target, dict) or set(target) & set(values):
            raise ValueError('Staffing fragments would overwrite captured fields')
        if any(person not in indexed['people'] for person in values.get('default_person_ids', [])):
            raise ValueError('Unknown staffing person UUID')
        target.update(deepcopy(values))
    return tables


def creation_plan(package):
    """Account for every source row; candidate means DTO-valid, never command-qualified."""
    if package['format'] != FORMAT:
        raise ValueError('Read-only preparation expects native V2')
    from apps.api.services.catalogue import rank_code
    tables = recombine(package)
    links = defaultdict(list)
    for row in tables['shift_services']:
        links[row['shift_id']].append(row['service_id'])
    codes = Counter(rank_code(row) for row in tables['categories'])
    conflicts = {row['id'] for row in tables['categories'] if rank_code(row) and codes[rank_code(row)] > 1}
    records, failures = [], []
    for table, rows in tables.items():
        cls = CONTRACTS.get(table)
        for index, row in enumerate(rows):
            key = table + '/' + str(row.get('id', index))
            reason = CONTEXT.get(table, 'DTO candidate only; reconcile target UUIDs, current revisions and command-level constraints')
            value = {k: v for k, v in row.items() if cls and k in cls.model_fields}
            if cls and 'reason' in cls.model_fields:
                value['reason'] = 'Offline validation only; no target action authorized by this candidate'
            if table == 'shifts':
                value.update(service_ids=links[row['id']], participants=[])
            errors = []
            def validate(contract, payload, context):
                try:
                    contract.model_validate(payload)
                except ValidationError as exc:
                    # Values/messages may contain private data; retain only contract locations/types.
                    errors.extend({'context': context, 'field': '.'.join(map(str, e['loc'])), 'type': e['type']}
                                  for e in exc.errors(include_input=False))
            if cls:
                validate(cls, value, cls.__name__)
            for field, contract in [('type_snapshot', s.AssignmentTypeInput), ('series_snapshot', s.SeriesInput)]:
                if row.get(field):
                    validate(contract, {k: v for k, v in row[field].items() if k in contract.model_fields}, field)
            if table == 'definition_history':
                contract = s.PolicyInput if row['kind'] == 'policy' else CONTRACTS.get(row['kind'])
                if contract:
                    validate(contract, {k: v for k, v in row['snapshot'].items() if k in contract.model_fields}, 'snapshot')
            if table == 'categories' and row['id'] in conflicts:
                reason = 'Conflicting canonical rank code; retain both pending without renaming or choosing a winner'
            if table == 'memberships' and row['category_id'] in conflicts:
                reason = 'Parent rank has a conflicting canonical code; no substitute rank inferred'
            pending = table in CONTEXT or bool(errors) or (
                table == 'categories' and row['id'] in conflicts or table == 'memberships' and row['category_id'] in conflicts)
            item = {'path': 'tables/' + table, 'index': index, 'source_key': key,
                    'source_fingerprint': fingerprint(package['dataset']['tables'][table][index]),
                    'recombined_fingerprint': fingerprint(row),
                    'state': 'pending' if pending else 'candidate', 'reason': reason,
                    'contract': cls.__name__ if cls else None, 'validation_errors': errors}
            if cls and not pending:
                item['creation_candidate'] = value
            records.append(item)
            if errors:
                failures.append({'source_key': key, 'errors': errors})
    return {'format': FORMAT, 'archive_sha256': package['archive_sha256'],
            'source_pin': package['dataset']['source_pin'], 'snapshot_id': package['dataset']['snapshot_id'],
            'target_contract_sha256': hashlib.sha256(open(s.__file__, 'rb').read()).hexdigest(),
            'files': package['package_manifest']['files'], 'counts': dict(Counter(r['state'] for r in records)),
            'source_rows': len(records), 'imported': 0, 'records': records,
            'validation_failures': failures, 'declared_reference_values_checked': package['declared_reference_values_checked'],
            'limits': ['No database opened, imported, replaced or qualified',
                'DTO candidates still require ID mapping, nested-reference and command-level validation',
                'Source approvals are evidence, not target approval or access authority',
                'Baseline lineage, operator edits, version history, auth state and data keys must be preserved',
                'Snapshot/history UUIDs must be remapped without substituting current definitions']}


def prepare(package, report):
    result = creation_plan(package)
    report.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        json.dump(result, out, indent=2)
    return {k: result[k] for k in ('archive_sha256', 'counts', 'source_rows', 'imported')}
