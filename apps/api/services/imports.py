import csv
import hashlib
import io
from datetime import date, timedelta
from sqlalchemy import select
from packages.contracts import schemas as s
from packages.domain.time import local_stamp
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import workspace_content
from .common import scoped, expected, require_import_unapplied
from .planning import draft, write_shift, flush_shift_batch, prime_shift_batch, changed
from .planning_groups import require_change_authority

REQUIRED_COLUMNS = {"assignment_type", "location", "service", "person", "start", "end", "overtime"}
import json
from pathlib import Path

# Deployment-owned source mappings are optional; public CSV imports need none.
_mapping_path = Path(__file__).resolve().parents[3] / "config" / "imports-mapping.json"
try:
    _IMPORT_MAPPING = json.loads(_mapping_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    _IMPORT_MAPPING = {}
if not isinstance(_IMPORT_MAPPING, dict):
    raise ValueError("Import mapping configuration must be an object")
if _IMPORT_MAPPING and (
    not isinstance(_IMPORT_MAPPING.get("literals"), list)
    or not all(isinstance(value, str) for value in _IMPORT_MAPPING["literals"])
    or not isinstance(_IMPORT_MAPPING.get("reference_only_exports"), dict)
    or not isinstance(_IMPORT_MAPPING.get("reference_lookups"), dict)
):
    raise ValueError("Invalid import mapping configuration")
REFERENCE_ONLY_EXPORTS = {
    path: set(names) for path, names in _IMPORT_MAPPING.get("reference_only_exports", {}).items()
}
REFERENCE_LOOKUPS = _IMPORT_MAPPING.get("reference_lookups", {})

def reference_receipt(db, source):
    if not _IMPORT_MAPPING:
        files = source.manifest.get('files', [])
        rows = [{'path': item.get('path', ''), 'key': item.get('path', ''), 'label': item.get('path', ''), 'status': 'unresolved', 'target_kind': None, 'target_id': None, 'reason': 'Source mapping configuration is not installed.'} for item in files]
        return {'id': source.id, 'archive_sha256': source.archive_sha256, 'source_name': source.source_name, 'created_at': source.created_at.isoformat(), 'counts': {'imported': 0, 'historical_evidence': 0, 'unresolved': len(rows)}, 'total': len(rows), 'records': rows, 'source_updates': [], 'notice': 'Install source mapping configuration to reconcile reference records.'}
    from collections import Counter
    mappings = {r.source_key: r for r in db.scalars(select(m.SourceMapping).where(m.SourceMapping.department_id == source.department_id, m.SourceMapping.source_name == source.source_name))}
    if source.manifest.get(_IMPORT_MAPPING['literals'][0]) in {_IMPORT_MAPPING['literals'][1], _IMPORT_MAPPING['literals'][2]}:
        rows = []
        for entry in source.manifest[_IMPORT_MAPPING['literals'][3]]:
            value = source.manifest[_IMPORT_MAPPING['literals'][4]]
            for part in entry[_IMPORT_MAPPING['literals'][5]].split(_IMPORT_MAPPING['literals'][6]):
                value = value[part]
            value = value[entry[_IMPORT_MAPPING['literals'][7]]]
            mapping = mappings.get(entry[_IMPORT_MAPPING['literals'][8]])
            rows.append({_IMPORT_MAPPING['literals'][5]: entry[_IMPORT_MAPPING['literals'][5]], _IMPORT_MAPPING['literals'][8]: entry[_IMPORT_MAPPING['literals'][8]], _IMPORT_MAPPING['literals'][9]: str(value.get(_IMPORT_MAPPING['literals'][14]) or value.get(_IMPORT_MAPPING['literals'][15]) or value.get(_IMPORT_MAPPING['literals'][16]) or entry[_IMPORT_MAPPING['literals'][8]]), _IMPORT_MAPPING['literals'][10]: _IMPORT_MAPPING['literals'][17] if mapping else _IMPORT_MAPPING['literals'][19] if entry[_IMPORT_MAPPING['literals'][5]].startswith(_IMPORT_MAPPING['literals'][18]) else _IMPORT_MAPPING['literals'][20], _IMPORT_MAPPING['literals'][11]: mapping.target_kind if mapping else None, _IMPORT_MAPPING['literals'][12]: mapping.target_id if mapping else None, _IMPORT_MAPPING['literals'][13]: entry[_IMPORT_MAPPING['literals'][13]]})
        counts = Counter((r[_IMPORT_MAPPING['literals'][10]] for r in rows))
        return {_IMPORT_MAPPING['literals'][21]: source.id, _IMPORT_MAPPING['literals'][22]: source.archive_sha256, _IMPORT_MAPPING['literals'][23]: source.source_name, _IMPORT_MAPPING['literals'][24]: source.created_at.isoformat(), _IMPORT_MAPPING['literals'][25]: {key: counts[key] for key in (_IMPORT_MAPPING['literals'][17], _IMPORT_MAPPING['literals'][19], _IMPORT_MAPPING['literals'][20])}, _IMPORT_MAPPING['literals'][26]: len(rows), _IMPORT_MAPPING['literals'][27]: rows, _IMPORT_MAPPING['literals'][28]: [], _IMPORT_MAPPING['literals'][29]: _IMPORT_MAPPING['literals'][30] if source.manifest[_IMPORT_MAPPING['literals'][0]] == _IMPORT_MAPPING['literals'][2] else _IMPORT_MAPPING['literals'][31]}
    staff = next((f.get(_IMPORT_MAPPING['literals'][32], {}).get(_IMPORT_MAPPING['literals'][33], []) for f in source.manifest[_IMPORT_MAPPING['literals'][34]] if f[_IMPORT_MAPPING['literals'][5]] == _IMPORT_MAPPING['literals'][35]), [])
    directory_order_ready = bool(staff) and all((_IMPORT_MAPPING['literals'][36] + p[_IMPORT_MAPPING['literals'][21]] in mappings for p in staff))
    ranks_ready = bool(staff) and all((_IMPORT_MAPPING['literals'][37] + p[_IMPORT_MAPPING['literals'][21]] in mappings for p in staff))
    exports = {r[_IMPORT_MAPPING['literals'][14]]: r[_IMPORT_MAPPING['literals'][32]] for f in source.manifest[_IMPORT_MAPPING['literals'][34]] for r in f.get(_IMPORT_MAPPING['literals'][27], []) if r[_IMPORT_MAPPING['literals'][38]] == _IMPORT_MAPPING['literals'][39]}
    dependencies = {(_IMPORT_MAPPING['literals'][40], _IMPORT_MAPPING['literals'][41]): [_IMPORT_MAPPING['literals'][47]], **{(_IMPORT_MAPPING['literals'][42], key): [_IMPORT_MAPPING['literals'][47]] for key in (_IMPORT_MAPPING['literals'][48], _IMPORT_MAPPING['literals'][49], _IMPORT_MAPPING['literals'][50], _IMPORT_MAPPING['literals'][51])}, (_IMPORT_MAPPING['literals'][42], _IMPORT_MAPPING['literals'][43]): [_IMPORT_MAPPING['literals'][52] + str(i) for i, _ in enumerate(exports.get(_IMPORT_MAPPING['literals'][53], []))], **{(_IMPORT_MAPPING['literals'][44], key): [_IMPORT_MAPPING['literals'][54]] for key in (_IMPORT_MAPPING['literals'][55], _IMPORT_MAPPING['literals'][56], _IMPORT_MAPPING['literals'][57], _IMPORT_MAPPING['literals'][58], _IMPORT_MAPPING['literals'][59])}, (_IMPORT_MAPPING['literals'][44], _IMPORT_MAPPING['literals'][45]): [_IMPORT_MAPPING['literals'][60]], (_IMPORT_MAPPING['literals'][44], _IMPORT_MAPPING['literals'][46]): [_IMPORT_MAPPING['literals'][61]]}
    residency_notes = {_IMPORT_MAPPING['literals'][62]: [_IMPORT_MAPPING['literals'][66], _IMPORT_MAPPING['literals'][67]], _IMPORT_MAPPING['literals'][63]: [_IMPORT_MAPPING['literals'][68]], _IMPORT_MAPPING['literals'][64]: [_IMPORT_MAPPING['literals'][69]], _IMPORT_MAPPING['literals'][65]: [_IMPORT_MAPPING['literals'][70], _IMPORT_MAPPING['literals'][71], _IMPORT_MAPPING['literals'][72]]}
    clinics = {r[_IMPORT_MAPPING['literals'][21]]: index for index, r in enumerate(exports.get(_IMPORT_MAPPING['literals'][73], [])) if isinstance(r, dict) and isinstance(r.get(_IMPORT_MAPPING['literals'][21]), str)}
    eligibility = {(r.person_id, r.service_id) for r in db.scalars(select(m.Eligibility).where(m.Eligibility.department_id == source.department_id))}

    def operational_reason(name, value):
        if name == _IMPORT_MAPPING['literals'][74] and isinstance(value, dict):
            return _IMPORT_MAPPING['literals'][75] + value.get(_IMPORT_MAPPING['literals'][76], _IMPORT_MAPPING['literals'][77]) + _IMPORT_MAPPING['literals'][78]
        if name == _IMPORT_MAPPING['literals'][79] and isinstance(value, dict):
            additional = sorted(set(value) - {_IMPORT_MAPPING['literals'][80], _IMPORT_MAPPING['literals'][81], _IMPORT_MAPPING['literals'][82]})
            return _IMPORT_MAPPING['literals'][83] + _IMPORT_MAPPING['literals'][84].join(additional) + _IMPORT_MAPPING['literals'][85] if additional else _IMPORT_MAPPING['literals'][86]
        if name in {_IMPORT_MAPPING['literals'][87], _IMPORT_MAPPING['literals'][88], _IMPORT_MAPPING['literals'][89], _IMPORT_MAPPING['literals'][90]} and isinstance(value, dict):
            issues = []
            clinic_key = _IMPORT_MAPPING['literals'][91] + str(clinics.get(value.get(_IMPORT_MAPPING['literals'][92]), _IMPORT_MAPPING['literals'][93]))
            clinic = mappings.get(clinic_key)
            if not clinic:
                issues.append(_IMPORT_MAPPING['literals'][94])
            people = [mappings.get(_IMPORT_MAPPING['literals'][95] + pid) for pid in value.get(_IMPORT_MAPPING['literals'][96], [value[_IMPORT_MAPPING['literals'][80]]] if value.get(_IMPORT_MAPPING['literals'][80]) else [])]
            people += [mappings.get(_IMPORT_MAPPING['literals'][97] + _IMPORT_MAPPING['literals'][98].join(alias.split()).casefold()) for alias in value.get(_IMPORT_MAPPING['literals'][99], [])]
            if not people or any((p is None for p in people)):
                issues.append(_IMPORT_MAPPING['literals'][100])
            if clinic and any((p and (p.target_id, clinic.target_id) not in eligibility for p in people)):
                issues.append(_IMPORT_MAPPING['literals'][101])
            if value.get(_IMPORT_MAPPING['literals'][102]) not in exports.get(_IMPORT_MAPPING['literals'][103], {}):
                issues.append(_IMPORT_MAPPING['literals'][104])
            issues.append(_IMPORT_MAPPING['literals'][105])
            if not value.get(_IMPORT_MAPPING['literals'][106]) and (not value.get(_IMPORT_MAPPING['literals'][107])) and (not exports.get(_IMPORT_MAPPING['literals'][40], {}).get(_IMPORT_MAPPING['literals'][41])):
                issues.append(_IMPORT_MAPPING['literals'][108])
            return _IMPORT_MAPPING['literals'][109].join(issues) + _IMPORT_MAPPING['literals'][110]
        return {_IMPORT_MAPPING['literals'][111]: _IMPORT_MAPPING['literals'][115], _IMPORT_MAPPING['literals'][112]: _IMPORT_MAPPING['literals'][116], _IMPORT_MAPPING['literals'][113]: _IMPORT_MAPPING['literals'][117], _IMPORT_MAPPING['literals'][74]: _IMPORT_MAPPING['literals'][118], _IMPORT_MAPPING['literals'][79]: _IMPORT_MAPPING['literals'][119], _IMPORT_MAPPING['literals'][114]: _IMPORT_MAPPING['literals'][120]}.get(name)
    rows = []

    def add(path, key, value, mapping_key=None, historical=False, reason=None, unresolved_reason=None):
        mapping = mappings.get(mapping_key)
        if mapping and mapping.target_kind == _IMPORT_MAPPING['literals'][121]:
            reason = _IMPORT_MAPPING['literals'][122]
            if mapping_key.startswith(_IMPORT_MAPPING['literals'][52]):
                reason = _IMPORT_MAPPING['literals'][123]
            if mapping_key == _IMPORT_MAPPING['literals'][124]:
                reason += _IMPORT_MAPPING['literals'][125]
        if mapping and mapping_key.startswith(_IMPORT_MAPPING['literals'][126]) and (mapping_key + _IMPORT_MAPPING['literals'][127] in mappings):
            reason = _IMPORT_MAPPING['literals'][128]
        if mapping and mapping.target_kind == _IMPORT_MAPPING['literals'][129]:
            reason = _IMPORT_MAPPING['literals'][130]
        if mapping and mapping.target_kind == _IMPORT_MAPPING['literals'][131]:
            reason = _IMPORT_MAPPING['literals'][132]
        if mapping and mapping.target_kind == _IMPORT_MAPPING['literals'][133]:
            reason = _IMPORT_MAPPING['literals'][134]
        if mapping and mapping.target_kind in {_IMPORT_MAPPING['literals'][135], _IMPORT_MAPPING['literals'][136]}:
            reason = _IMPORT_MAPPING['literals'][137]
        rows.append({_IMPORT_MAPPING['literals'][5]: path, _IMPORT_MAPPING['literals'][8]: key, _IMPORT_MAPPING['literals'][9]: next((str(value[k]) for k in (_IMPORT_MAPPING['literals'][138], _IMPORT_MAPPING['literals'][14], _IMPORT_MAPPING['literals'][139], _IMPORT_MAPPING['literals'][9], _IMPORT_MAPPING['literals'][80], _IMPORT_MAPPING['literals'][21]) if value.get(k)), key) if isinstance(value, dict) else key, _IMPORT_MAPPING['literals'][10]: _IMPORT_MAPPING['literals'][20] if unresolved_reason else _IMPORT_MAPPING['literals'][17] if mapping else _IMPORT_MAPPING['literals'][19] if historical else _IMPORT_MAPPING['literals'][20], _IMPORT_MAPPING['literals'][11]: mapping.target_kind if mapping else None, _IMPORT_MAPPING['literals'][12]: mapping.target_id if mapping else None, _IMPORT_MAPPING['literals'][13]: unresolved_reason or reason or (_IMPORT_MAPPING['literals'][140] if mapping else _IMPORT_MAPPING['literals'][141])})
    for file in source.manifest[_IMPORT_MAPPING['literals'][34]]:
        path = file[_IMPORT_MAPPING['literals'][5]]
        if _IMPORT_MAPPING['literals'][32] in file:
            for collection, values in file[_IMPORT_MAPPING['literals'][32]].items():
                entries = enumerate(values) if isinstance(values, list) else [(collection, values)]
                for index, value in entries:
                    mapping_key = _IMPORT_MAPPING['literals'][95] + value[_IMPORT_MAPPING['literals'][21]] if path == _IMPORT_MAPPING['literals'][35] else _IMPORT_MAPPING['literals'][143] + str(index) if path == _IMPORT_MAPPING['literals'][142] else _IMPORT_MAPPING['literals'][144] + path + _IMPORT_MAPPING['literals'][145] + str(index)
                    add(path, ''.join([format(collection, ''), _IMPORT_MAPPING['literals'][6], format(index, '')]), value, mapping_key)
        for export in file.get(_IMPORT_MAPPING['literals'][27], []):
            name = export[_IMPORT_MAPPING['literals'][14]]
            lookup = REFERENCE_LOOKUPS.get(path, {}).get(name)
            reference_only = name in REFERENCE_ONLY_EXPORTS.get(path, set()) or bool(lookup)
            retained_reason = _IMPORT_MAPPING['literals'][146] + lookup if lookup else _IMPORT_MAPPING['literals'][147]
            if directory_order_ready and path == _IMPORT_MAPPING['literals'][148] and (name in {_IMPORT_MAPPING['literals'][149], _IMPORT_MAPPING['literals'][150], _IMPORT_MAPPING['literals'][151]}):
                reference_only = True
                retained_reason = _IMPORT_MAPPING['literals'][152]
            if directory_order_ready and path == _IMPORT_MAPPING['literals'][153] and (name == _IMPORT_MAPPING['literals'][154]):
                reference_only = True
                retained_reason = _IMPORT_MAPPING['literals'][155]
            if ranks_ready and path == _IMPORT_MAPPING['literals'][153] and (name == _IMPORT_MAPPING['literals'][156]):
                reference_only = True
                retained_reason = _IMPORT_MAPPING['literals'][157]
            if export[_IMPORT_MAPPING['literals'][38]] != _IMPORT_MAPPING['literals'][39]:
                add(path, name, None, historical=reference_only, reason=retained_reason if reference_only else export.get(_IMPORT_MAPPING['literals'][13], _IMPORT_MAPPING['literals'][158]))
                continue
            value = export[_IMPORT_MAPPING['literals'][32]]
            entries = enumerate(value) if isinstance(value, list) else value.items() if isinstance(value, dict) else [(name, value)]
            for key, row in entries:
                if name == _IMPORT_MAPPING['literals'][159]:
                    for index, entry in enumerate(row[_IMPORT_MAPPING['literals'][160]]):
                        add(path, ''.join([format(name, ''), _IMPORT_MAPPING['literals'][6], format(key, ''), _IMPORT_MAPPING['literals'][161], format(index, '')]), entry, historical=True, reason=_IMPORT_MAPPING['literals'][162])
                else:
                    unresolved_reason = None
                    item_historical, item_reason = (reference_only, retained_reason)
                    required = dependencies.get((name, key), [])
                    if name == _IMPORT_MAPPING['literals'][163] and isinstance(row, str):
                        required = [_IMPORT_MAPPING['literals'][164] + field for field in residency_notes.get(row, [])]
                        if row == _IMPORT_MAPPING['literals'][165]:
                            required = [_IMPORT_MAPPING['literals'][52] + str(i) for i, roster in enumerate(exports.get(_IMPORT_MAPPING['literals'][53], [])) if roster.get(_IMPORT_MAPPING['literals'][166])]
                        if row == _IMPORT_MAPPING['literals'][167]:
                            required = [_IMPORT_MAPPING['literals'][168]]
                    if name == _IMPORT_MAPPING['literals'][169] and row == {_IMPORT_MAPPING['literals'][170]: _IMPORT_MAPPING['literals'][171]}:
                        required = [_IMPORT_MAPPING['literals'][172] + str(i) for i, placement in enumerate(exports.get(_IMPORT_MAPPING['literals'][173], [])) if placement.get(_IMPORT_MAPPING['literals'][174]) == key]
                    if required and all((k in mappings and mappings[k].target_kind == _IMPORT_MAPPING['literals'][121] for k in required)):
                        item_historical = True
                        item_reason = _IMPORT_MAPPING['literals'][175]
                        if name == _IMPORT_MAPPING['literals'][163] and row == _IMPORT_MAPPING['literals'][165]:
                            item_reason = _IMPORT_MAPPING['literals'][176]
                        if name == _IMPORT_MAPPING['literals'][163] and row == _IMPORT_MAPPING['literals'][167]:
                            item_reason = _IMPORT_MAPPING['literals'][177]
                    if name == _IMPORT_MAPPING['literals'][163] and row == _IMPORT_MAPPING['literals'][178]:
                        sports = [_IMPORT_MAPPING['literals'][179] + export_name + _IMPORT_MAPPING['literals'][145] + str(i) for export_name in (_IMPORT_MAPPING['literals'][87], _IMPORT_MAPPING['literals'][88]) for i, duty in enumerate(exports.get(export_name, [])) if duty.get(_IMPORT_MAPPING['literals'][92]) == _IMPORT_MAPPING['literals'][180]]
                        if sports and all((k in mappings and mappings[k].target_kind == _IMPORT_MAPPING['literals'][131] for k in sports)):
                            item_historical = True
                            item_reason = _IMPORT_MAPPING['literals'][181]
                    if path == _IMPORT_MAPPING['literals'][182] and name == _IMPORT_MAPPING['literals'][183] and ranks_ready:
                        item_historical = True
                        item_reason = _IMPORT_MAPPING['literals'][184]
                    if path == _IMPORT_MAPPING['literals'][182] and name == _IMPORT_MAPPING['literals'][185] and isinstance(row, dict):
                        slots = row.get(_IMPORT_MAPPING['literals'][82], [])
                        fixed = [slot for slot in exports.get(_IMPORT_MAPPING['literals'][88], []) if slot.get(_IMPORT_MAPPING['literals'][80]) == row.get(_IMPORT_MAPPING['literals'][80]) and slot.get(_IMPORT_MAPPING['literals'][92]) == _IMPORT_MAPPING['literals'][186]]
                        if set(row) == {_IMPORT_MAPPING['literals'][80], _IMPORT_MAPPING['literals'][82]} and isinstance(slots, list) and slots and all((isinstance(slot, dict) and set(slot) == {_IMPORT_MAPPING['literals'][187], _IMPORT_MAPPING['literals'][102]} for slot in slots)) and (sorted(((slot[_IMPORT_MAPPING['literals'][187]], slot[_IMPORT_MAPPING['literals'][102]]) for slot in slots)) == sorted(((slot[_IMPORT_MAPPING['literals'][187]], slot[_IMPORT_MAPPING['literals'][102]]) for slot in fixed))):
                            item_historical = True
                            item_reason = _IMPORT_MAPPING['literals'][188]
                    if name == _IMPORT_MAPPING['literals'][40] and path == _IMPORT_MAPPING['literals'][189]:
                        explanations = {_IMPORT_MAPPING['literals'][14]: _IMPORT_MAPPING['literals'][199], _IMPORT_MAPPING['literals'][190]: _IMPORT_MAPPING['literals'][200], _IMPORT_MAPPING['literals'][191]: _IMPORT_MAPPING['literals'][201], _IMPORT_MAPPING['literals'][192]: _IMPORT_MAPPING['literals'][202], _IMPORT_MAPPING['literals'][193]: _IMPORT_MAPPING['literals'][203], _IMPORT_MAPPING['literals'][194]: _IMPORT_MAPPING['literals'][203], _IMPORT_MAPPING['literals'][195]: _IMPORT_MAPPING['literals'][204], _IMPORT_MAPPING['literals'][196]: _IMPORT_MAPPING['literals'][205], _IMPORT_MAPPING['literals'][197]: _IMPORT_MAPPING['literals'][206], _IMPORT_MAPPING['literals'][198]: _IMPORT_MAPPING['literals'][207]}
                        if key in explanations:
                            item_historical, item_reason = (True, explanations[key])
                    if name == _IMPORT_MAPPING['literals'][208] and isinstance(row, dict) and row.get(_IMPORT_MAPPING['literals'][209]) and (_IMPORT_MAPPING['literals'][126] + str(key) + _IMPORT_MAPPING['literals'][127] not in mappings):
                        unresolved_reason = _IMPORT_MAPPING['literals'][210]
                    if name == _IMPORT_MAPPING['literals'][169] and isinstance(row, dict) and (row.get(_IMPORT_MAPPING['literals'][170]) == _IMPORT_MAPPING['literals'][211]) and any((f.get(_IMPORT_MAPPING['literals'][174]) == key and f.get(_IMPORT_MAPPING['literals'][209]) and (_IMPORT_MAPPING['literals'][126] + str(i) + _IMPORT_MAPPING['literals'][127] not in mappings) for i, f in enumerate(exports.get(_IMPORT_MAPPING['literals'][208], [])))):
                        unresolved_reason = _IMPORT_MAPPING['literals'][212]
                    add(path, ''.join([format(name, ''), _IMPORT_MAPPING['literals'][6], format(key, '')]), row, _IMPORT_MAPPING['literals'][179] + name + _IMPORT_MAPPING['literals'][145] + str(key), historical=item_historical, reason=item_reason if item_historical else operational_reason(name, row) if _IMPORT_MAPPING['literals'][179] + name + _IMPORT_MAPPING['literals'][145] + str(key) not in mappings else None, unresolved_reason=unresolved_reason)
    counts = Counter((r[_IMPORT_MAPPING['literals'][10]] for r in rows))
    config = next((r[_IMPORT_MAPPING['literals'][32]] for f in source.manifest[_IMPORT_MAPPING['literals'][34]] for r in f.get(_IMPORT_MAPPING['literals'][27], []) if r[_IMPORT_MAPPING['literals'][14]] == _IMPORT_MAPPING['literals'][40] and r[_IMPORT_MAPPING['literals'][38]] == _IMPORT_MAPPING['literals'][39]), {})
    return {_IMPORT_MAPPING['literals'][21]: source.id, _IMPORT_MAPPING['literals'][22]: source.archive_sha256, _IMPORT_MAPPING['literals'][23]: source.source_name, _IMPORT_MAPPING['literals'][24]: source.created_at.isoformat(), _IMPORT_MAPPING['literals'][25]: {key: counts[key] for key in (_IMPORT_MAPPING['literals'][17], _IMPORT_MAPPING['literals'][19], _IMPORT_MAPPING['literals'][20])}, _IMPORT_MAPPING['literals'][26]: len(rows), _IMPORT_MAPPING['literals'][27]: rows, _IMPORT_MAPPING['literals'][28]: config.get(_IMPORT_MAPPING['literals'][197], []), _IMPORT_MAPPING['literals'][29]: _IMPORT_MAPPING['literals'][213]}


def preview_import(db, actor, command):
    require_role(actor, "coordinator")
    value = s.ImportPreview.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    require_change_authority(db, actor, workspace, workspace_content(db, workspace), session_definitions=True)
    require("\x00" not in value.csv_text, "INVALID_FILE", "NUL bytes are not allowed")
    source_hash = hashlib.sha256(value.csv_text.encode("utf-8")).hexdigest()
    require(db.scalar(select(m.ImportBatch).where(m.ImportBatch.workspace_id == workspace.id, m.ImportBatch.source_hash == source_hash)) is None,
            "DUPLICATE_IMPORT", "This source content already has a batch in this workspace", 409)
    reader = csv.DictReader(io.StringIO(value.csv_text.lstrip("\ufeff"), newline=""))
    headers = reader.fieldnames or []
    require(len(headers) == len(set(headers)) and REQUIRED_COLUMNS <= set(headers), "IMPORT_HEADERS", "Use the required CSV column names", 422, {"required": sorted(REQUIRED_COLUMNS)})
    allowed_metadata = {"publication_id", "publication_sequence", "metric_version", "source_state", "timezone", "shift_id", "team_lead", "source_note"}
    require(all(h in REQUIRED_COLUMNS or h in allowed_metadata or h.startswith("field_") for h in headers),
            "UNKNOWN_COLUMN", "Unknown columns are not silently ignored")
    rows = []
    for index, data in enumerate(reader, 2):
        require(index <= 1001, "IMPORT_TOO_LARGE", "CSV imports are limited to 1,000 rows")
        require(None not in data and all(v is not None for v in data.values()), "MALFORMED_ROW", f"Row {index} has an incorrect column count")
        errors = []
        try:
            s.InstantInterval.model_validate({"start": data["start"], "end": data["end"]})
        except ValueError:
            errors.append("start/end must be ordered ISO timestamps with an explicit UTC offset")
        if data["overtime"].lower() not in {"true", "false"}:
            errors.append("overtime must be true or false on each person's row")
        if 'team_lead' in data and data['team_lead'].lower() not in {'true', 'false'}:
            errors.append("team_lead must be true or false on each person's row")
        rows.append({"row_number": index, "data": data, "errors": errors})
    require(rows, "EMPTY_IMPORT", "The file contains no data rows")
    batch = m.ImportBatch(department_id=actor.department_id, workspace_id=workspace.id,
                          source_name=value.source_name, source_hash=source_hash, rows=rows,
                          created_by=actor.identity_id, workspace_revision=workspace.revision)
    db.add(batch)
    db.flush()
    return {"batch": record(batch), "message": "Preview only. Every identity/type mapping must be explicitly confirmed before saving a draft."}


def reference_rota_rows(db, source, starts_on, ends_on):
    if not _IMPORT_MAPPING:
        return []
    exports = {r[_IMPORT_MAPPING['literals'][14]]: r[_IMPORT_MAPPING['literals'][32]] for f in source.manifest[_IMPORT_MAPPING['literals'][34]] for r in f.get(_IMPORT_MAPPING['literals'][27], []) if r[_IMPORT_MAPPING['literals'][38]] == _IMPORT_MAPPING['literals'][39]}
    mappings = {r.source_key: r for r in db.scalars(select(m.SourceMapping).where(m.SourceMapping.department_id == source.department_id, m.SourceMapping.source_name == source.source_name))}
    clinics = {r[_IMPORT_MAPPING['literals'][21]]: i for i, r in enumerate(exports.get(_IMPORT_MAPPING['literals'][73], []))}
    zone = exports.get(_IMPORT_MAPPING['literals'][40], {}).get(_IMPORT_MAPPING['literals'][214])
    require(bool(zone), _IMPORT_MAPPING['literals'][215], _IMPORT_MAPPING['literals'][216])
    labels = {}

    def label(key, model, fallback):
        if key in labels:
            return labels[key]
        mapping = mappings.get(key)
        row = db.get(model, mapping.target_id) if mapping else None
        labels[key] = getattr(row, _IMPORT_MAPPING['literals'][15], None) or row.name if row else fallback
        return labels[key]
    rows = []
    for index, duty in enumerate(exports.get(_IMPORT_MAPPING['literals'][90], [])):
        key = _IMPORT_MAPPING['literals'][217] + str(index)
        require(isinstance(duty, dict), _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][220]]))
        try:
            day = date.fromisoformat(duty[_IMPORT_MAPPING['literals'][106]])
        except (KeyError, TypeError, ValueError):
            require(False, _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][221]]))
        if not starts_on <= day < ends_on or key in mappings:
            continue
        require(not set(duty) - {_IMPORT_MAPPING['literals'][106], _IMPORT_MAPPING['literals'][92], _IMPORT_MAPPING['literals'][102], _IMPORT_MAPPING['literals'][222], _IMPORT_MAPPING['literals'][223], _IMPORT_MAPPING['literals'][96], _IMPORT_MAPPING['literals'][224], _IMPORT_MAPPING['literals'][225], _IMPORT_MAPPING['literals'][76]}, _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][226]]))
        require(all((isinstance(duty.get(field), str) and duty[field] for field in (_IMPORT_MAPPING['literals'][92], _IMPORT_MAPPING['literals'][102], _IMPORT_MAPPING['literals'][222], _IMPORT_MAPPING['literals'][223]))) and isinstance(duty.get(_IMPORT_MAPPING['literals'][76], _IMPORT_MAPPING['literals'][227]), str), _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][228]]))
        people = duty.get(_IMPORT_MAPPING['literals'][96], [])
        require(isinstance(people, list) and people and all((isinstance(p, str) for p in people)) and (len(people) == len(set(people))), _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][229]]))
        for field in (_IMPORT_MAPPING['literals'][224], _IMPORT_MAPPING['literals'][225]):
            values = duty.get(field, [])
            require(isinstance(values, list) and all((isinstance(p, str) for p in values)) and (set(values) <= set(people)), _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][230], format(field, ''), _IMPORT_MAPPING['literals'][231]]))
        try:
            start = local_stamp(day, duty[_IMPORT_MAPPING['literals'][222]], zone)
            end = local_stamp(day, duty[_IMPORT_MAPPING['literals'][223]], zone)
            if end <= start:
                end = local_stamp(day + timedelta(days=1), duty[_IMPORT_MAPPING['literals'][223]], zone)
            s.InstantInterval(start=start, end=end)
        except (KeyError, TypeError, ValueError):
            require(False, _IMPORT_MAPPING['literals'][218], ''.join([_IMPORT_MAPPING['literals'][219], format(index, ''), _IMPORT_MAPPING['literals'][232]]))
        for person in people:
            rows.append({_IMPORT_MAPPING['literals'][233]: source.id, _IMPORT_MAPPING['literals'][234]: key, _IMPORT_MAPPING['literals'][235]: {_IMPORT_MAPPING['literals'][236]: label(_IMPORT_MAPPING['literals'][244] + duty[_IMPORT_MAPPING['literals'][102]], m.AssignmentType, duty[_IMPORT_MAPPING['literals'][102]]), _IMPORT_MAPPING['literals'][237]: _IMPORT_MAPPING['literals'][245], _IMPORT_MAPPING['literals'][238]: label(_IMPORT_MAPPING['literals'][91] + str(clinics.get(duty[_IMPORT_MAPPING['literals'][92]])), m.Service, duty[_IMPORT_MAPPING['literals'][92]]), _IMPORT_MAPPING['literals'][239]: label(_IMPORT_MAPPING['literals'][95] + person, m.Person, person), _IMPORT_MAPPING['literals'][240]: start.isoformat(), _IMPORT_MAPPING['literals'][241]: end.isoformat(), _IMPORT_MAPPING['literals'][242]: str(person in duty.get(_IMPORT_MAPPING['literals'][224], [])).lower(), _IMPORT_MAPPING['literals'][225]: str(person in duty.get(_IMPORT_MAPPING['literals'][225], [])).lower(), _IMPORT_MAPPING['literals'][243]: duty.get(_IMPORT_MAPPING['literals'][76], _IMPORT_MAPPING['literals'][227])}})
    return rows


def preview_reference_rota(db, actor, command):
    value = s.ReferenceRotaPreview.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    require_change_authority(db, actor, workspace, workspace_content(db, workspace), session_definitions=True)
    source = scoped(db, m.ReferenceImport, value.reference_import_id, actor)
    prepared = reference_rota_rows(db, source, workspace.starts_on, workspace.ends_on)
    require(prepared, 'NO_SOURCE_DUTIES', 'No unimported dated duties start within this draft period')
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(prepared[0]['data']))
    writer.writeheader(); writer.writerows(row['data'] for row in prepared)
    result = preview_import(db, actor, command.model_copy(update={'payload': {
        'workspace_id': workspace.id, 'source_name': (source.source_name + ' dated duties')[:160], 'csv_text': out.getvalue()}}))
    batch = db.get(m.ImportBatch, result['batch']['id'])
    batch.rows = [{**row, 'reference_import_id': origin['reference_import_id'], 'reference_key': origin['reference_key']}
                  for row, origin in zip(batch.rows, prepared, strict=True)]
    db.flush()
    result['batch'] = record(batch)
    return result


def refresh_import(db, actor, command):
    """Explicitly rebase a preview, retaining original source rows and batch identity."""
    value = s.ImportRefresh.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    require_change_authority(db, actor, workspace, workspace_content(db, workspace), session_definitions=True)
    batch = scoped(db, m.ImportBatch, value.batch_id, actor)
    require(batch.workspace_id == workspace.id and batch.status == "preview", "IMPORT_STATE",
            "Only an unapplied preview in this workspace can be refreshed", 409)
    batch.workspace_revision = workspace.revision
    batch.revision += 1
    db.flush()
    return {"batch": record(batch), "message": "Preview refreshed; reconfirm every row before saving."}


def parse_custom_fields(typ, raw):
    definition = s.TypeDefinition.model_validate(typ.definition)
    fields = {f.key: f for f in definition.fields}
    values = {}
    for key, value in raw.items():
        if not key.startswith("field_") or value == "":
            continue
        name = key.removeprefix("field_")
        require(name in fields, "UNKNOWN_CUSTOM_FIELD", "Import contains an unknown field for this assignment type")
        f = fields[name]
        if f.kind == "number":
            try:
                value = float(value)
            except ValueError:
                require(False, "CUSTOM_FIELD_INVALID", f"{f.label}: numeric value required")
        elif f.kind == "boolean":
            require(value.lower() in {"true", "false"}, "CUSTOM_FIELD_INVALID", f"{f.label}: true or false required")
            value = value.lower() == "true"
        elif f.kind == "multi_choice":
            # Round-trip representation is JSON, not evaluation of Python/JavaScript expressions.
            import json
            try:
                value = json.loads(value)
            except ValueError:
                require(False, "CUSTOM_FIELD_INVALID", f"{f.label}: JSON string array required")
        values[name] = value
    s.validate_attributes(definition, values)
    return values


def apply_import(db, actor, command):
    value = s.ImportApply.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    before = workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, before, session_definitions=True)
    batch = scoped(db, m.ImportBatch, value.batch_id, actor)
    require_import_unapplied(batch, "saved_draft")
    require(batch.workspace_id == workspace.id and batch.status == "preview", "IMPORT_STATE", "Batch is not an unapplied preview for this draft", 409)
    require(batch.workspace_revision == workspace.revision, "STALE_IMPORT", "Draft changed after the import preview; produce a new reconciliation preview", 409)
    require(all(not row["errors"] for row in batch.rows), "IMPORT_ROW_ERRORS", "Fix all row errors before saving", 422, batch.rows)
    mappings = {r.row_number: r for r in value.mappings}
    require(len(mappings) == len(value.mappings) and set(mappings) == {r["row_number"] for r in batch.rows},
            "INCOMPLETE_MAPPING", "Confirm exactly one mapping for every source row")
    groups = {}
    source_groups = {}
    for row in batch.rows:
        mapping, raw = mappings[row["row_number"]], row["data"]
        typ = scoped(db, m.AssignmentType, mapping.assignment_type_id, actor)
        attrs = parse_custom_fields(typ, raw)
        services = tuple(sorted([mapping.service_id, *mapping.additional_service_ids]))
        require(' | ' not in raw['service'] or len(services) == len(raw['service'].split(' | ')),
                'INCOMPLETE_SERVICE_MAPPING', 'Map every source clinic to the same shared duty')
        key = (mapping.assignment_type_id, mapping.location_id, services, raw["start"], raw["end"])
        if row.get('reference_key'):
            source = scoped(db, m.ReferenceImport, row['reference_import_id'], actor)
            source_key = (source.source_name, row['reference_key'])
            if source_key not in source_groups:
                require(not db.scalar(select(m.SourceMapping.id).where(m.SourceMapping.department_id == actor.department_id,
                    m.SourceMapping.source_name == source.source_name, m.SourceMapping.source_key == row['reference_key'])),
                    'SOURCE_ALREADY_IMPORTED', 'A source duty in this preview has already been imported', 409)
            require(source_groups.setdefault(source_key, key) == key, 'CONFLICTING_SOURCE_DUTY',
                    'All employees from one source duty must map to the same type, location and covered clinics')
        group = groups.setdefault(key, {"attributes": attrs, "participants": [], "rows": []})
        require(group["attributes"] == attrs, "CONFLICTING_FIELDS", "Rows for one session disagree on its custom fields")
        group["participants"].append({"person_id": mapping.person_id, "start": raw["start"], "end": raw["end"],
                                       "overtime": raw["overtime"].lower() == "true",
                                       "team_lead": raw.get('team_lead', 'false').lower() == 'true'})
        group["rows"].append(row["row_number"])
    links, shift_batch = {}, {}
    prime_shift_batch(db, actor, shift_batch, (
        (m.AssignmentType, {key[0] for key in groups}),
        (m.Location, {key[1] for key in groups}),
        (m.Service, {sid for key in groups for sid in key[2]}),
        (m.Person, {p['person_id'] for group in groups.values() for p in group['participants']})))
    for (typ, loc, services, start, end), group in groups.items():
        value_shift = s.ShiftInput(assignment_type_id=typ, location_id=loc, service_ids=list(services), start=start, end=end,
                                   attributes=group["attributes"], participants=group["participants"])
        shift = write_shift(db, actor, workspace, value_shift, source='import', batch=shift_batch)
        for (source_name, source_key), group_key in source_groups.items():
            if group_key == (typ, loc, services, start, end):
                db.add(m.SourceMapping(department_id=actor.department_id, source_name=source_name,
                    source_key=source_key, target_kind='shifts', target_id=shift.id))
        for row_no in group["rows"]:
            links[row_no] = shift.id
    flush_shift_batch(db, shift_batch)
    batch.rows = [{**row, "resolved": value.mappings[[x.row_number for x in value.mappings].index(row["row_number"])].model_dump(),
                   "shift_id": links[row["row_number"]]} for row in batch.rows]
    batch.status = "saved_draft"
    batch.revision += 1
    result = changed(db, actor, workspace, before, value.reason)
    result["batch_id"] = batch.id
    result["source_rows"] = len(batch.rows)
    return result
