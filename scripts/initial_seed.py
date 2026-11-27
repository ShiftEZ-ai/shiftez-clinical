"""Explicit, configuration-only production start; never runs at startup or deployment.

    python scripts/initial_seed.py --seed-version 1 --reason 'Owner chose the initial configuration'

Requires an empty, migrated installation on first use. Repeats preserve subsequent operator edits.
No people, sign-in accounts, role bindings or schedules are created. Clinical rule templates await approval;
the former built-in scoring defaults are visible and active.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import Field
from sqlalchemy import select
from apps.api import models as m
from apps.api import settings as deployment_settings
from apps.api.db import make_engine, transaction
from apps.api.schema import MIGRATIONS, require_current_schema
from apps.api.services.leave_types import seed as seed_leave_types
from apps.api.settings import Settings, refuse_test_flags, secret_from_env
from apps.api.serialization import record
from packages.contracts import schemas as s

try:
    from scripts import system_command
except ImportError:  # The released image can run this operator bundle mounted at /kit.
    import system_command

SEEDS = Path(__file__).resolve().parent / "seeds"


class SeedRecord(s.Schema):
    key: str = Field(min_length=1, max_length=80)
    catalogue: Literal["categories", "capabilities", "programmes", "location_types", "operating_calendars",
                       "locations", "services", "assignment_types"]
    value: dict


class Seed(s.Schema):
    version: Literal[1]
    institution: s.DepartmentSettingsInput
    starts_on: date
    catalogues: list[SeedRecord] = Field(min_length=1, max_length=100)
    rules: list[s.RuleProposalInput] = Field(min_length=1, max_length=40)


def references(value, ids):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return ids[value["$ref"]]
        return {key: references(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [references(item, ids) for item in value]
    return value


def load(engine, settings, version, reason):
    reason = s.Decision.model_validate({"reason": reason}).reason
    if type(version) is not int or version != 1:
        raise ValueError("Only the reviewed initial seed version 1 is supported")
    raw = (SEEDS / f"production-v{version}.json").read_bytes()
    seed = Seed.model_validate_json(raw)
    if seed.version != version or len({r.key for r in seed.catalogues}) != len(seed.catalogues):
        raise ValueError("Seed version/record keys must be unambiguous")
    checksum = hashlib.sha256(raw).hexdigest()
    with transaction(engine, write=True) as db:
        require_current_schema(db)
        applied = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "installation.seed"))
        if applied:
            if applied.detail.get("version") != version or applied.detail.get("sha256") != checksum:
                raise RuntimeError("Refusing a different seed on an initialized installation")
            return {"status": "already_applied", "department_id": applied.department_id, **applied.detail}
        if any(db.scalar(select(model.id).limit(1)) for model in (m.Organisation, m.Department, m.Identity, m.Person)):
            raise RuntimeError("Refusing initial seed on a nonempty installation; existing data was not changed")
        institution = seed.institution
        org = m.Organisation(name=institution.organisation_name, timezone=institution.timezone, week_start=institution.week_start)
        db.add(org)
        db.flush()
        # This operator bundle also runs inside predecessor release images: retain their own defaults.
        defaults = getattr(deployment_settings, "new_department_session_limits", dict)()
        display = getattr(deployment_settings, "new_department_display_settings", dict)()
        department = m.Department(organisation_id=org.id, name=institution.department_name,
                                  weekend_days=institution.weekend_days, display_settings=display, **defaults)
        db.add(department)
        db.flush()
        seed_leave_types(db, department.id)
        try:
            from apps.api.services.event_defaults import seed_department
        except ImportError:
            pass
        else:
            seed_department(db, department)
        identity = system_command.maintenance_identity(db, m, select)
        try:
            import apps.api.services.scoring_defaults as scoring_defaults
        except ModuleNotFoundError as exc:
            # The candidate kit also seeds predecessor images; 0109 supplies their defaults on upgrade.
            if exc.name != "apps.api.services.scoring_defaults" or (MIGRATIONS / "versions/0109_fair_share_dials.py").is_file():
                raise
            scoring_defaults = None
        ids = {}
        correlation = f"initial-seed-v{version}"
        for entry in seed.catalogues:
            command = {"kind": "catalogue.create", "payload": {"catalogue": entry.catalogue,
                       "value": {"starts_on": seed.starts_on.isoformat(), **references(entry.value, ids)}, "reason": reason}}
            result = system_command.apply(db, identity, department, [command], correlation, settings)[0]
            ids[entry.key] = result["record"]["id"]
        if scoring_defaults is not None:
            scoring_defaults.seed(db, department.id, catalogue_version=1)
        for rule in seed.rules:
            payload = rule.model_dump(mode='json')
            current = next((p for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department.id,
                m.Policy.status == 'active', m.Policy.kind == 'catalogue'))
                if p.definition['rule']['id'] == rule.catalogue_id), None)
            if current and scoring_defaults is not None:
                defaults = scoring_defaults.seeded_rows(db, [record(current)])[0]['definition']['rule']['lines']
                given = {line['id'] for line in payload['lines']}
                payload.update(expected_policy_id=current.id, expected_revision=current.revision,
                    lines=[line for line in defaults if line['id'] not in given] + payload['lines'])
            system_command.apply(db, identity, department, [{"kind": "rule.propose",
                                 "payload": {**payload, "reason": reason}}], correlation, settings)
        detail = {"version": version, "sha256": checksum, "counts": {"catalogues": len(seed.catalogues), "rule_templates": len(seed.rules)}}
        db.add(m.AuditEvent(department_id=department.id, actor_id=identity.id, action="installation.seed",
                            target_id=department.id, reason=reason, detail=detail, correlation_id=correlation))
        return {"status": "applied", "department_id": department.id, **detail}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed-version", required=True, type=int, choices=[1])
    parser.add_argument("--reason", required=True)
    parser.add_argument("--database-url", default=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"))
    args = parser.parse_args(argv)
    environment = os.getenv("ROTA_ENV", "production")
    refuse_test_flags(environment)
    if environment == "production" and not args.database_url.startswith("postgresql+psycopg://"):
        parser.error("Production initial seed requires an explicit PostgreSQL psycopg URL")
    settings = Settings(environment=environment, database_url=args.database_url)
    engine = make_engine(args.database_url)
    try:
        print(json.dumps(load(engine, settings, args.seed_version, args.reason), sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
