"""One application-wide presentation owner, independent of sign-in and clinical inputs."""
import hashlib
import io
import warnings
from types import SimpleNamespace
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import defer
from packages.contracts import schemas as s
from packages.web_assets import asset_url
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import Problem, require
from apps.api.serialization import record
from apps.api.schema import require_current_schema
from .common import expected, history
MAX_FILE_BYTES = s.BRANDING_IMAGE_LIMITS['file_bytes']
MAX_IMAGE_PIXELS = s.BRANDING_IMAGE_LIMITS['image_pixels']

SINGLETON = 'application'
IMAGE_FORMATS = s.BRANDING_IMAGE_FORMATS


def can_edit(actor):
    return actor.permits(*s.PERMISSIONS['branding.configure'])


def require_editor(actor):
    require(can_edit(actor), 'APPLICATION_ADMIN_REQUIRED',
            f"Ask an {s.ROLE_LABELS[s.INSTALLATION_ADMIN_ROLE]} to change the name and logo", 403)


def settings(db):
    row = db.get(m.ApplicationBranding, SINGLETON)
    return s.BrandingSettings.model_validate(row.data if row else {}).model_dump(mode='json')


def image_url(ident):
    return '/api/branding/images/' + ident


def public(db):
    row = db.get(m.ApplicationBranding, SINGLETON)
    value = settings(db)
    urls = {}
    for kind in ('emblem', 'wordmark'):
        key = kind + '_url'
        default = s.BRANDING_DEFAULTS[key]
        image_id = value[kind + '_image_id']
        urls[key] = image_url(image_id) if image_id else default and asset_url(default)
    return s.BrandingPublic(**{key: value[key] for key in (
        'short_name', 'app_name', 'institution_name', 'emblem_alt', 'wordmark_alt')},
        **urls, revision=row.revision if row else 1).model_dump(mode='json')


def status(db, actor):
    permitted = can_edit(actor)
    if not permitted:
        require_role(actor, 'admin')
    row = db.get(m.ApplicationBranding, SINGLETON)
    return {'branding': public(db), 'settings': settings(db), 'can_edit': permitted,
            'can_undo': permitted and bool(row and row.previous is not None)}


def change(db, actor, command):
    require_editor(actor)
    require(command.target_id is None, 'BRANDING_TARGET', 'Branding is application-wide')
    row = db.get(m.ApplicationBranding, SINGLETON)
    expected(row or SimpleNamespace(revision=1), command.expected_revision)
    previous = settings(db)
    if command.kind == 'branding.undo':
        value = s.Decision.model_validate(command.payload)
        require(row is not None and row.previous is not None, 'NOTHING_TO_UNDO', 'No branding change to undo', 409)
        updated = s.BrandingSettings.model_validate(row.previous).model_dump(mode='json')
    else:
        value = s.BrandingInput.model_validate(command.payload)
        updated = {**previous, **value.model_dump(mode='json', exclude={'reason'}, exclude_unset=True)}
        ids = {updated[key] for key in ('emblem_image_id', 'wordmark_image_id') if updated[key]}
        found = set(db.scalars(select(m.BrandingImage.id).where(m.BrandingImage.id.in_(ids)))) if ids else set()
        require(ids == found, 'BRANDING_IMAGE_NOT_FOUND', 'Upload the selected branding images first', 404)
    if updated != previous:
        if row is None:
            row = m.ApplicationBranding(id=SINGLETON, data=previous, revision=1)
            db.add(row)
            history(db, actor, 'application_branding', row, value.reason)
        row.data, row.previous = updated, previous
        row.revision += 1
        history(db, actor, 'application_branding', row, value.reason)
    from .pwa import current_icons
    current_icons(db, render_missing=True)
    return status(db, actor)


def versions(db, actor, limit, offset):
    require_editor(actor)
    rows = db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'application_branding',
        m.DefinitionHistory.object_id == SINGLETON).order_by(m.DefinitionHistory.object_revision).limit(limit).offset(offset))
    return {'versions': [record(row) for row in rows]}


def prepare_image(content, media_type):
    require(media_type in IMAGE_FORMATS, 'BRANDING_IMAGE_TYPE', 'Use a PNG, JPEG or WebP image', 415)
    require(content and len(content) <= MAX_FILE_BYTES, 'BRANDING_IMAGE_LIMIT', 'Image exceeds the supported size', 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=list(IMAGE_FORMATS.values())) as source:
                require(source.format == IMAGE_FORMATS[media_type] and getattr(source, 'n_frames', 1) == 1,
                        'BRANDING_IMAGE_INVALID', 'Use one raster image matching its content type')
                require(source.width * source.height <= MAX_IMAGE_PIXELS,
                        'BRANDING_IMAGE_LIMIT', 'Image exceeds the supported pixel size', 413)
                source.load()
                # A fresh pixel-only image strips EXIF, comments and profiles while keeping logo transparency.
                with source.convert('RGBA') as pixels, Image.new('RGBA', source.size) as clean:
                    clean.paste(pixels)
                    output = io.BytesIO()
                    clean.save(output, format='PNG')
        result = output.getvalue()
        require(len(result) <= MAX_FILE_BYTES, 'BRANDING_IMAGE_LIMIT', 'Image exceeds the supported size', 413)
        return result
    except Problem:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise Problem(413, 'BRANDING_IMAGE_LIMIT', 'Image exceeds the supported pixel size') from None
    except (OSError, ValueError, SyntaxError):
        raise Problem(422, 'BRANDING_IMAGE_INVALID', 'Image could not be decoded') from None


def store_image(db, actor, content, correlation_id):
    require_current_schema(db)
    require_editor(actor)
    sha = hashlib.sha256(content).hexdigest()
    row = db.scalar(select(m.BrandingImage).where(m.BrandingImage.sha256 == sha))
    if row is None:
        row = m.BrandingImage(content=content, sha256=sha)
        db.add(row)
        db.flush()
        db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
            action='branding.image_upload', target_id=row.id, reason='Upload a branding image',
            detail={'sha256': sha}, correlation_id=correlation_id))
    return {'image_id': row.id, 'url': image_url(row.id), 'sha256': row.sha256}


def published_image(db, image_id, *, include_content=True):
    # Staged uploads stay private until a branding change publishes their local reference.
    referenced = select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == 'application_branding',
        m.DefinitionHistory.object_id == SINGLETON,
        (m.DefinitionHistory.snapshot['data']['emblem_image_id'].as_string() == image_id)
        | (m.DefinitionHistory.snapshot['data']['wordmark_image_id'].as_string() == image_id)).exists()
    query = select(m.BrandingImage).where(m.BrandingImage.id == image_id, referenced)
    row = db.scalar(query if include_content else query.options(defer(m.BrandingImage.content)))
    require(row is not None, 'NOT_FOUND', 'Branding image not found', 404)
    return row
