"""Private departmental feedback, explicit triage and a conservative GitHub preview."""
import base64
import binascii
from dataclasses import dataclass, field, replace
from datetime import timedelta
import hashlib
import json
from math import ceil
import re
from urllib.parse import unquote
import zlib

import httpx
from sqlalchemy import func, or_, select
from packages.contracts import schemas as s
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import device_label, held_roles, require_permission
from apps.api.errors import Problem, require
from apps.api.serialization import record
from apps.api.settings import valid_feedback_github_repo
from .common import expected, outbox, scoped


TRIAGE_FIELDS = ("status", "duplicate_of", "resolved_in", "triage_note")
GITHUB_UNCERTAIN_MESSAGE = ("GitHub issue creation could not be confirmed. Check the configured repository before "
                            "exporting again; your feedback is still saved.")


def route_path(route):
    """The browser uses #page; /page and #/page name that same route."""
    return "/" + route.removeprefix("#").split("?", 1)[0].split("#", 1)[0].lstrip("/")


def fingerprint(value):
    error = value.recent_errors[0].message if value.recent_errors else ""
    content = json.dumps([value.kind, route_path(value.route), error], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_report(db, actor, report_id):
    row = scoped(db, m.FeedbackReport, report_id, actor)
    require(actor.permits(*s.PERMISSIONS["feedback.read_all"]) or row.reporter_identity_id == actor.identity_id,
            "NOT_FOUND", "Feedback report not found", 404)
    return row


def _duplicate_counts(db, actor, report_ids, *, mine=False):
    F = m.FeedbackReport
    query = select(F.duplicate_of, func.count()).where(
        F.department_id == actor.department_id, F.duplicate_of.in_(report_ids))
    if mine or not actor.permits(*s.PERMISSIONS["feedback.read_all"]):
        query = query.where(F.reporter_identity_id == actor.identity_id)
    return dict(db.execute(query.group_by(F.duplicate_of)).all())


def report_record(db, actor, row, has_screenshot=None, reporter_name=None, duplicates_count=None):
    if has_screenshot is None:
        has_screenshot = db.scalar(select(m.FeedbackReport.screenshot.is_not(None)).where(m.FeedbackReport.id == row.id))
    if reporter_name is None:
        reporter_name = db.get(m.Identity, row.reporter_identity_id).display_name
    if duplicates_count is None:
        duplicates_count = _duplicate_counts(db, actor, [row.id]).get(row.id, 0)
    return {**record(row, exclude=("department_id", "screenshot", "screenshot_media_type", "github_export_repo")),
            "has_screenshot": bool(has_screenshot), "reporter_name": reporter_name,
            "duplicates_count": duplicates_count}


def _valid_png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    at, saw_header, saw_pixels = 8, False, False
    while at + 12 <= len(data):
        size = int.from_bytes(data[at:at + 4], "big")
        end = at + 12 + size
        if end > len(data):
            return False
        kind, content = data[at + 4:at + 8], data[at + 8:end - 4]
        if zlib.crc32(data[at + 4:end - 4]) != int.from_bytes(data[end - 4:end], "big"):
            return False
        if not saw_header:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = int.from_bytes(content[:4], "big"), int.from_bytes(content[4:8], "big")
            if not 1 <= width <= 32768 or not 1 <= height <= 32768:
                return False
            saw_header = True
        elif kind == b"IHDR":
            return False
        elif kind == b"IDAT":
            saw_pixels = saw_pixels or size > 0
        elif kind == b"IEND":
            return size == 0 and saw_pixels and end == len(data)
        at = end
    return False


def _valid_webp(data):
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP" \
            or int.from_bytes(data[4:8], "little") + 8 != len(data):
        return False
    at, saw_pixels = 12, False
    while at + 8 <= len(data):
        kind, size = data[at:at + 4], int.from_bytes(data[at + 4:at + 8], "little")
        end = at + 8 + size + size % 2
        if end > len(data):
            return False
        if kind == b"VP8 ":
            if size < 10 or data[at + 11:at + 14] != b"\x9d\x01\x2a":
                return False
            saw_pixels = True
        elif kind == b"VP8L":
            if size < 5 or data[at + 8] != 0x2f:
                return False
            saw_pixels = True
        elif kind == b"VP8X" and size != 10:
            return False
        at = end
    return at == len(data) and saw_pixels


def _image(value):
    if value is None:
        return None, None
    try:
        data = base64.b64decode(value.data, validate=True)
    except (ValueError, binascii.Error):
        raise Problem(422, "INVALID_SCREENSHOT", "The screenshot must be valid base64 PNG or WebP") from None
    require(0 < len(data) <= s.FEEDBACK_LIMITS["screenshot_bytes"], "SCREENSHOT_TOO_LARGE",
            "The screenshot may contain at most 2 MB")
    require((value.media_type == "image/png" and _valid_png(data))
            or (value.media_type == "image/webp" and _valid_webp(data)),
            "INVALID_SCREENSHOT", "The screenshot must match its PNG or WebP media type")
    return data, value.media_type


def _notify(db, actor, row, *, undo_of=()):
    outbox(db, actor, f"feedback-status:{row.id}:{row.revision}", "feedback",
           {"feedback_id": row.id, "feedback_report_id": row.id, "reporter_identity_id": row.reporter_identity_id,
            "person_id": row.reporter_person_id, "status": row.status, "resolved_in": row.resolved_in},
           undoable=True, undo_of=undo_of)


def create(db, actor, value, settings):
    """Called in the shared write transaction: count, match and increment are one atomic write."""
    value = s.FeedbackCreate.model_validate(value)
    route = _safe_route(_redact_credentials(value.route))
    recent_routes = [_safe_route(route) for route in value.recent_routes]
    require(all(len(path) <= s.FEEDBACK_LIMITS["route"] for path in [route, *recent_routes]), "FEEDBACK_ROUTE",
            f"Feedback route paths may contain at most {s.FEEDBACK_LIMITS['route']} characters after redaction")
    F, moment = m.FeedbackReport, m.now()
    who = F.reporter_person_id == actor.person_id if actor.person_id else F.reporter_identity_id == actor.identity_id
    count, first = db.execute(select(func.count(), func.min(F.created_at)).where(
        who, F.created_at > moment - timedelta(hours=1))).one()
    if count >= s.FEEDBACK_LIMITS["reports_per_hour"]:
        wait = min(3600, max(1, ceil((aware(first) + timedelta(hours=1) - moment).total_seconds())))
        raise Problem(429, "RATE_LIMITED", "You can send up to ten feedback reports per hour; try again later",
                      {"retry_after": wait})
    image, media_type = _image(value.screenshot)
    require(value.mark is None or image is not None, "SCREENSHOT_REQUIRED", "A marked area needs a screenshot")
    hashed = fingerprint(value)
    match = db.scalar(select(F).where(F.department_id == actor.department_id, F.fingerprint == hashed,
                      F.created_at >= moment - timedelta(days=s.FEEDBACK_LIMITS["duplicate_days"]))
                      .order_by(F.created_at, F.seq).limit(1))
    if match:
        # Recent linked occurrences also resurface the original report after its own 30-day window.
        visited = set()
        while match.duplicate_of and match.id not in visited:
            visited.add(match.id)
            match = scoped(db, F, match.duplicate_of, actor)
        match.occurrences += 1
        match.updated_at, match.revision = moment, match.revision + 1
        if match.status in ("fixed", "wont_fix"):
            match.status, match.resolved_in = "new", None
            _notify(db, actor, match)
    context = value.model_dump(mode="json", exclude={"screenshot", "kind", "message", "expected"})
    context["route"] = route
    context["recent_routes"] = recent_routes
    context["recent_errors"] = [{**error, "message": _safe_error(_redact_credentials(error["message"])),
        "source": error["source"] if error["source"] in {
            "console", "error", "unhandledrejection", "uncaught error", "unhandled rejection", "network", "notice", "message shown"}
            else "unknown"}
        for error in context["recent_errors"]]
    # Authority and deployment facts come from the authenticated session, never client claims.
    context["role_names"] = sorted(held_roles(db, actor.identity_id, actor.department_id))
    context["environment"] = "production" if settings.deployment == "production" else "dev"
    row = F(department_id=actor.department_id, reporter_person_id=actor.person_id,
            reporter_identity_id=actor.identity_id, kind=value.kind, message=value.message, expected=value.expected,
            **context, screenshot=image, screenshot_media_type=media_type, fingerprint=hashed,
            status="duplicate" if match else "new", duplicate_of=match.id if match else None,
            created_at=moment, updated_at=moment)
    db.add(row)
    db.flush()
    return {"id": row.id, "matched_report_id": match.id if match else None}


def list_reports(db, actor, *, mine=False, status=None, kind=None, duplicate_of=None, q="", page=1, page_size=None, search_options=None):
    page_size = s.FEEDBACK_LIMITS["page_size"] if page_size is None else page_size
    require(page >= 1 and 1 <= page_size <= 100, "FEEDBACK_PAGE", "Choose a valid feedback page and page size")
    require(status is None or status in s.FEEDBACK_STATUSES, "FEEDBACK_STATUS", "Choose a valid feedback status")
    require(kind is None or kind in s.FEEDBACK_KINDS, "FEEDBACK_KIND", "Choose a valid feedback kind")
    require(len(q) <= 300, "FEEDBACK_SEARCH", "Feedback search may contain at most 300 characters")
    F = m.FeedbackReport
    filters = [F.department_id == actor.department_id]
    if mine or not actor.permits(*s.PERMISSIONS["feedback.read_all"]):
        filters.append(F.reporter_identity_id == actor.identity_id)
    if status:
        filters.append(F.status == status)
    if kind:
        filters.append(F.kind == kind)
    if duplicate_of is not None:
        filters.append(F.duplicate_of == duplicate_of)
    if search_options is not None:
        from .search import predicate
        filters.append(predicate(db, actor, 'feedback', q, search_options))
    elif q.strip():
        filters.append(or_(*(func.lower(column).contains(q.strip().lower(), autoescape=True)
                            for column in (F.message, F.expected, F.triage_note, F.route))))
    total = db.scalar(select(func.count()).select_from(F).where(*filters))
    rows = db.execute(select(F, F.screenshot.is_not(None), m.Identity.display_name)
                      .join(m.Identity, m.Identity.id == F.reporter_identity_id).where(*filters)
                      .order_by(F.created_at.desc(), F.seq.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    counts = _duplicate_counts(db, actor, [row.id for row, _, _ in rows], mine=mine) if rows else {}
    return {"rows": [report_record(db, actor, row, present, name, counts.get(row.id, 0))
                     for row, present, name in rows],
            "total": total, "page": page, "page_size": page_size}


def detail(db, actor, report_id):
    return report_record(db, actor, get_report(db, actor, report_id))


def screenshot(db, actor, report_id):
    row = get_report(db, actor, report_id)
    require(row.screenshot is not None, "NOT_FOUND", "Screenshot not found or its retention period has ended", 404)
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                       action="feedback.screenshot.read", target_id=row.id, reason="Private feedback screenshot read",
                       detail={}, correlation_id=db.info.get("correlation_id") or "feedback"))
    return row.screenshot, row.screenshot_media_type


def _update(db, actor, value, *, undo_of=()):
    row = scoped(db, m.FeedbackReport, value.id, actor)
    expected(row, value.revision)
    previous = {key: getattr(row, key) for key in TRIAGE_FIELDS}
    changes = value.model_dump(exclude_unset=True, exclude={"id", "revision"})
    require(bool(changes), "FEEDBACK_CHANGE_REQUIRED", "Choose a status or add a triage note")
    require(changes.get("status", row.status) is not None, "FEEDBACK_STATUS", "Choose a valid feedback status")
    status = changes.get("status", row.status)
    if "status" in changes and status != "duplicate" and "duplicate_of" not in changes:
        changes["duplicate_of"] = None
    duplicate_of = changes.get("duplicate_of", row.duplicate_of)
    require((status == "duplicate") == bool(duplicate_of), "FEEDBACK_DUPLICATE_REQUIRED",
            "A duplicate status needs a linked report, and only a duplicate may have that link")
    if duplicate_of:
        visited = {row.id}
        target = scoped(db, m.FeedbackReport, duplicate_of, actor)
        while True:
            require(target.id not in visited, "FEEDBACK_DUPLICATE_CYCLE", "Duplicate links must not form a cycle")
            visited.add(target.id)
            if not target.duplicate_of:
                break
            target = scoped(db, m.FeedbackReport, target.duplicate_of, actor)
    for key, item in changes.items():
        setattr(row, key, item)
    row.updated_at, row.revision = m.now(), row.revision + 1
    if row.status != previous["status"]:
        _notify(db, actor, row, undo_of=undo_of)
    db.flush()
    return {"report": report_record(db, actor, row), "previous_feedback": previous}


def update(db, actor, command):
    require_permission(actor, "feedback.triage")
    value = s.FeedbackUpdate.model_validate(command.payload)
    return _update(db, actor, value)


def undo(db, actor, command):
    require_permission(actor, "feedback.triage")
    value = s.FeedbackUndo.model_validate(command.payload)
    row = scoped(db, m.FeedbackReport, value.id, actor)
    expected(row, value.revision)
    receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.department_id == actor.department_id,
                        m.CommandReceipt.command_kind == "feedback.update",
                        m.CommandReceipt.result["report"]["id"].as_string() == row.id,
                        m.CommandReceipt.result["report"]["revision"].as_integer() == row.revision).limit(1))
    require(receipt is not None and bool(receipt.result.get("previous_feedback")), "NO_UNDO",
            "No immediately preceding feedback edit is available to undo", 409)
    require(receipt.actor_id == actor.identity_id, "FORBIDDEN", "Only the Developer who changed this feedback can undo it", 403)
    restored = s.FeedbackUpdate(id=row.id, revision=row.revision, **receipt.result["previous_feedback"])
    result = _update(db, actor, restored, undo_of=f"feedback-status:{row.id}:{row.revision}")
    result.pop("previous_feedback")
    return result


_EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_SECRET = re.compile(r"(?i)\b(?:authorization|password|passwd|secret|token|api[_-]?key|cookie|mrn|patient[_ -]?id|staff[_ -]?number)\b[\"']?\s*[:=]\s*(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)")
# PATs can be next to prose or already escaped in a historical Markdown preview.
_TOKEN = re.compile(r"ghs\\?_[A-Za-z0-9]+\\?_eyJ(?:[A-Za-z0-9-]|\\?_)*"
                    r"\.(?:[A-Za-z0-9-]|\\?_)+\.(?:[A-Za-z0-9-]|\\?_)+"
                    r"|(?:gh[pousr]\\?_|github\\?_pat\\?_)(?:[A-Za-z0-9]|\\?_)+"
                    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>@#!])")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_ROUTE_PARTS = frozenset(s.APP_ROUTE_PARTS)
_SAFE_ERRORS = frozenset(("Failed to fetch", "NetworkError when attempting to fetch resource.",
                          "Load failed", "Failed to save", "Save failed", "Request failed", "Network request failed"))
_ERROR_FAMILY = re.compile(r"^(TypeError|ReferenceError|SyntaxError|RangeError|NetworkError|AbortError|TimeoutError|Error)(?::|\b)")


def _escape_markdown(text):
    return _MARKDOWN.sub(r"\\\1", text)


def _redact_credentials(text, *tokens, markdown=False):
    marker = r"\[redacted\]" if markdown else "[redacted]"
    for token in tokens:
        if token:
            for form in (token, _escape_markdown(token)):
                text = text.replace(form, marker)
    return _TOKEN.sub(lambda match: marker, text)


def _credential_token(integration, settings, *, required=False):
    if integration is None:
        return settings.feedback_github_token
    from . import github_integrations
    try:
        return github_integrations.decrypt(integration, settings)
    except Problem as error:
        # Readable previews survive a lost key; a new external write always requires decryption.
        if required or error.code not in {"INTEGRATION_KEY_MISSING", "INTEGRATION_KEY_INVALID"}:
            raise
        return ""


def redact_export_result(db, actor, result, settings):
    """Project historic outcomes without rewriting evidence, Markdown or the report link."""
    from . import github_integrations
    token = _credential_token(github_integrations.credential(db, actor.department_id), settings)
    return {**result, **{key: _redact_credentials(result[key], token, settings.feedback_github_token,
                                                 markdown=key == "body")
                         for key in ("title", "body") if isinstance(result.get(key), str)}}


def _sanitizer(db, actor, *tokens):
    from packages.domain.people import person_aliases
    from .people import imported_aliases
    imported = imported_aliases(db, actor)
    names = {name for person in db.scalars(select(m.Person).where(m.Person.organisation_id == actor.organisation_id))
             for name in (person.display_name, *person_aliases(record(person), imported.get(person.id, ())))}
    identities = select(m.RoleBinding.identity_id).where(m.RoleBinding.department_id == actor.department_id)
    names.update(db.scalars(select(m.Identity.display_name).where(m.Identity.id.in_(identities))))
    names = sorted({part for name in names for part in (name, *re.findall(r"[^\W\d_]{3,}", name))}, key=len, reverse=True)

    def clean(text):
        text = _redact_credentials(str(text or ""), *tokens)
        text = _URL.sub("[link redacted]", text)
        for pattern in (_EMAIL, _SECRET, _UUID, _PHONE):
            text = pattern.sub("[redacted]", text)
        for name in names:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[redacted]", text, flags=re.I)
        # Escape Markdown so opt-in text cannot introduce mentions, external images or links.
        return _escape_markdown("".join(c for c in text if ord(c) >= 32 or c == "\n"))
    return clean


def _safe_route(route):
    parts = unquote(route_path(route)).split("?", 1)[0].split("#", 1)[0].split("/")
    return "/" + "/".join(part if part in _ROUTE_PARTS else "[record]" for part in parts if part)


def _safe_error(message):
    if message in _SAFE_ERRORS:
        return message
    family = _ERROR_FAMILY.match(message)
    return f"{family.group(1)}: [details redacted]" if family else "[free-text error redacted]"


def create_github_issue(repo, token, title, body):
    """Only the explicitly requested export calls this seam; tests replace it or httpx.post."""
    require(valid_feedback_github_repo(repo), "FEEDBACK_GITHUB_REPO",
            "Configure a GitHub owner/repository before creating an issue", 409)
    try:
        from .github_integrations import github_headers
        response = httpx.post(f"https://api.github.com/repos/{repo}/issues", timeout=15,
                              headers=github_headers(token), json={"title": title, "body": body})
        require(response.status_code != 403, "FEEDBACK_GITHUB_FORBIDDEN",
                "GitHub refused issue creation. Check that the token has Issues write permission on this repository.", 422)
        require(not 400 <= response.status_code < 500, "FEEDBACK_GITHUB_REJECTED",
                "GitHub did not create the issue. Check the token, repository access and issue details, then try again.", 422)
        response.raise_for_status()
        url = response.json().get("html_url", "")
        require(response.status_code == 201 and isinstance(url, str)
                and bool(re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/issues/[1-9][0-9]*", url, re.I)),
                "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE, 502)
        return url
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        # Upstream responses and request headers may carry credentials or private repository details.
        raise Problem(502, "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE) from None


def github_marker(report_id):
    return f"<!-- fmuc-feedback-export:{report_id} -->"


def find_github_issue(repo, token, report_id):
    """Recover a possibly completed POST; absence never authorizes another POST."""
    require(valid_feedback_github_repo(repo), "FEEDBACK_GITHUB_REPO",
            "Configure a GitHub owner/repository before creating an issue", 409)
    from .github_integrations import github_headers
    try:
        # shortcut: inspect the latest 1,000 issues; add bounded date-window lookup if recovery needs older issues.
        for page in range(1, 11):
            response = httpx.get(f"https://api.github.com/repos/{repo}/issues", timeout=15,
                                 headers=github_headers(token),
                                 params={"state": "all", "sort": "created", "direction": "desc", "per_page": 100, "page": page})
            response.raise_for_status()
            rows = response.json()
            require(response.status_code == 200 and isinstance(rows, list),
                    "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE, 502)
            for row in rows:
                if "pull_request" not in row and github_marker(report_id) in (row.get("body") or ""):
                    url = row.get("html_url")
                    require(isinstance(url, str) and bool(re.fullmatch(
                        r"https://github\.com/" + re.escape(repo) + r"/issues/[1-9][0-9]*", url, re.I)),
                        "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE, 502)
                    return url
            if len(rows) < 100:
                break
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise Problem(502, "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE) from None
    raise Problem(502, "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE)


@dataclass(frozen=True)
class GithubExport:
    identity_id: str
    department_id: str
    report_id: str
    repo: str
    token: str = field(repr=False)
    title: str
    body: str
    integration_id: str | None
    integration_revision: int | None
    create: bool
    url: str | None = None


def prepare_github_export(db, actor, command, settings):
    """The caller commits this reservation before any remote write, including the first attempt."""
    preview = export_github(db, actor, command.model_copy(update={
        "payload": {**command.payload, "create_issue": False}}), settings)
    if preview["github_issue_url"] or not preview["configured"]:
        return None
    value = s.FeedbackExport.model_validate(command.payload)
    row = scoped(db, m.FeedbackReport, value.id, actor)
    from . import github_integrations
    integration = github_integrations.credential(db, actor.department_id, include_token=True)
    repo = integration.repo if integration else settings.feedback_github_repo
    token = _credential_token(integration, settings, required=True)
    require(row.github_export_repo is None or row.github_export_repo.lower() == repo.lower(),
            "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE, 502)
    create = row.github_export_repo is None
    row.github_export_repo = repo
    return GithubExport(actor.identity_id, actor.department_id, row.id, repo, token,
                        preview["title"], preview["body"], integration.id if integration else None,
                        integration.revision if integration else None, create)


def send_github_export(prepared):
    try:
        url = (create_github_issue(prepared.repo, prepared.token, prepared.title, prepared.body) if prepared.create else
               find_github_issue(prepared.repo, prepared.token, prepared.report_id))
    except httpx.HTTPError:
        raise Problem(502, "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE) from None
    return replace(prepared, url=url)


def export_github(db, actor, command, settings, *, exported=None):
    require_permission(actor, "feedback.export_github")
    value = s.FeedbackExport.model_validate(command.payload)
    row = scoped(db, m.FeedbackReport, value.id, actor)
    from . import github_integrations
    integration = github_integrations.credential(db, actor.department_id)
    repo = integration.repo if integration else settings.feedback_github_repo
    configured = integration is not None or bool(settings.feedback_github_token and repo)
    new_issue = value.create_issue and (configured or exported is not None) and not row.github_issue_url
    token = _credential_token(integration, settings)
    clean = _sanitizer(db, actor, token, settings.feedback_github_token, exported.token if exported else "")
    path = _safe_route(row.route)
    title = f"Rota {row.kind}: {path}"[:200]
    # These client fields are free text: preserve technical values, never export arbitrary clinical prose.
    version = row.app_version or "Not captured"
    commit = row.build_commit or "Not captured"
    if row.app_version and not re.fullmatch(
            r"v?\d+\.\d+\.\d+(?:-(?:alpha|beta|rc|dev)(?:\.\d+)?)?(?:\+[0-9a-f]{7,40})?|development|local", row.app_version):
        version = "[redacted]"
    if row.build_commit and not re.fullmatch(r"[0-9a-fA-F]{7,40}|local-build|development", row.build_commit):
        commit = "[redacted]"
    lines = [f"Kind: {row.kind}", f"Route: {path}", f"Version: {version}", f"Build: {commit}",
             f"Environment: {row.environment}", f"Browser: {device_label(row.user_agent) or 'Unknown browser'}",
             f"Viewport: {row.viewport['w']} × {row.viewport['h']}", "", "Recent routes:"]
    lines.extend("- " + _safe_route(route) for route in row.recent_routes)
    if not row.recent_routes:
        lines.append("- None captured")
    lines.extend(["", "Errors:"])
    lines.extend("- " + _safe_error(error.get("message", "")) for error in row.recent_errors)
    if not row.recent_errors:
        lines.append("- None captured")
    lines.extend(["", "Request IDs:"])
    lines.extend("- " + request_id if re.fullmatch(r"[0-9a-fA-F]{16,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request_id)
                 else "- [redacted]" for request_id in row.request_ids)
    if not row.request_ids:
        lines.append("- None captured")
    if value.include_message:
        lines.extend(["", "Message (explicitly included by a Developer):", clean(row.message)])
    lines.extend(["", f"Rota report: {settings.public_origin}/#system/feedback?report={row.id}",
                  "", "Names, screenshots, navigation values and unstructured error details are excluded.",
                  "", github_marker(row.id)])
    body = "\n".join(lines)
    if new_issue:
        require(exported is not None and exported.url is not None, "FEEDBACK_GITHUB_PREPARE_REQUIRED",
                "Create GitHub issues through the authenticated command endpoint", 409)
        require((exported.identity_id, exported.department_id, exported.report_id) ==
                (actor.identity_id, actor.department_id, row.id) and row.github_export_repo == exported.repo,
                "INTEGRATION_CONTEXT_CHANGED", "The signed-in department changed; export again", 409)
        row.github_issue_url = exported.url
        if integration and (integration.id, integration.revision) == (exported.integration_id, exported.integration_revision):
            integration.last_used_at = m.now()
        row.updated_at, row.revision = m.now(), row.revision + 1
        db.flush()
    if exported is not None:
        title, body = exported.title, exported.body
    return {"title": title, "body": body, "github_issue_url": row.github_issue_url, "configured": configured}
