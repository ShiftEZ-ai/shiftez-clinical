"""Conditional JSON reads, after the caller has authorized and projected the current rows."""
import hashlib
import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


def revision_etag(kind, *values):
    """Hash only bounded revision metadata for a representation validator."""
    return '"' + hashlib.sha256(json.dumps([kind, *values], default=str, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()[:32] + '"'


def conditional_response(request, tag, *, cache_control="private, no-cache"):
    """Return a cheap 304 response when a revision-backed representation is unchanged.

    Callers compute ``tag`` from the small revision stamp for their resource before doing
    any large projection work.  The body hash helper below remains for projections whose
    stamp is not available until after their response has been built.
    """
    headers = {"ETag": tag, "Cache-Control": cache_control}
    known = ",".join(request.headers.getlist("if-none-match"))
    if known.strip() == "*" or any(part.strip().removeprefix("W/") == tag.removeprefix("W/")
                                  for part in known.split(",")):
        return Response(status_code=304, headers=headers)
    return headers


def conditional_json(request, value, actor, *, tag=None):
    response = JSONResponse(jsonable_encoder(value))
    if tag is None:
        digest = hashlib.sha256(json.dumps([actor.identity_id, actor.department_id, actor.person_id,
                                           sorted(actor.roles)], separators=(",", ":")).encode())
        digest.update(response.body)
        # Weak validators identify the same JSON before middleware chooses gzip or identity encoding.
        tag = 'W/"' + digest.hexdigest() + '"'
    headers = {"ETag": tag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Accept-Encoding"}
    known = ",".join(request.headers.getlist("if-none-match"))
    if known.strip() == "*" or any(part.strip().removeprefix("W/") == tag.removeprefix("W/")
                                  for part in known.split(",")):
        return Response(status_code=304 if request.method in {"GET", "HEAD"} else 412, headers=headers)
    response.headers.update(headers)
    return response
