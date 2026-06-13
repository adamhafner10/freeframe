"""HLS proxy for secure video streaming.

Rewrites m3u8 manifests so that:
- Variant playlist URLs go through this proxy (with token auth)
- Segment (.ts) URLs become presigned S3 URLs (direct to S3)

This eliminates the need for a public bucket policy on processed/*.
"""

import logging
import posixpath
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services.s3_service import generate_presigned_get_url, get_s3_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["streaming"])

# HLS tokens are deliberately short-lived. A leaked stream URL must NOT replay
# segments for hours or survive share revocation/expiry/password rotation — bind
# the token to the share link (re-checked at serve-time) and keep the TTL tight.
HLS_TOKEN_TTL_MINUTES = 45


def create_hls_token(
    s3_prefix: str,
    expires_hours: Optional[int] = None,
    *,
    share_link_id: Optional[object] = None,
    user_id: Optional[object] = None,
    expires_minutes: int = HLS_TOKEN_TTL_MINUTES,
) -> str:
    """Create a short-lived JWT for HLS proxy access.

    Bind the token to the originating share link (``share_link_id``) and/or the
    authenticated user (``user_id``) so a leaked manifest/stream URL can't be
    replayed independently of the share's lifecycle. Share-link-bound tokens are
    re-validated against the live share link at serve time (see ``hls_proxy``).

    ``expires_hours`` is retained for backward compatibility; when provided it
    overrides the default minute-based TTL.
    """
    if expires_hours is not None:
        ttl = timedelta(hours=expires_hours)
    else:
        ttl = timedelta(minutes=expires_minutes)
    payload = {
        "sub": "hls",
        "pfx": s3_prefix,
        "exp": datetime.now(timezone.utc) + ttl,
    }
    if share_link_id is not None:
        payload["sl"] = str(share_link_id)
    if user_id is not None:
        payload["uid"] = str(user_id)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _verify_hls_token(token: str) -> str:
    """Verify HLS token and return s3_prefix (back-compat helper)."""
    return _decode_hls_token(token)["pfx"]


def _decode_hls_token(token: str) -> dict:
    """Verify an HLS token and return its full claims payload."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if payload.get("sub") != "hls":
            raise HTTPException(status_code=403, detail="Invalid token type")
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _assert_share_link_still_valid(db: Optional[Session], share_link_id: str) -> None:
    """Re-check that a share-link-bound HLS token's link is still live.

    A share-link-bound token must stop working the moment the link is revoked,
    expired, or deleted — even though the JWT itself hasn't expired yet. Member
    (non-share) tokens carry no ``sl`` claim and skip this entirely.
    """
    if not share_link_id:
        return
    if db is None:
        # No session available (e.g. a unit-test direct call). A share-bound
        # token without a DB to re-check against cannot be trusted to serve
        # segments, so fail closed.
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Imported lazily to avoid a circular import (share router imports this one).
    from ..models.share import ShareLink

    try:
        link_uuid = _uuid.UUID(str(share_link_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    link = db.query(ShareLink).filter(
        ShareLink.id == link_uuid,
        ShareLink.deleted_at.is_(None),
    ).first()
    if not link or not link.is_enabled:
        raise HTTPException(status_code=403, detail="Share link is no longer available")
    if link.expires_at and link.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Share link has expired")


def _rewrite_manifest(content: str, s3_prefix: str, manifest_path: str, token: str) -> str:
    """Rewrite URLs in an m3u8 manifest.

    - .m3u8 references -> proxy URLs with token (appended as query param)
    - .ts references -> presigned S3 URLs
    """
    manifest_dir = posixpath.dirname(manifest_path)
    lines = content.split("\n")
    result = []

    for line in lines:
        stripped = line.strip()

        # Pass through comments/tags and empty lines
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        # Resolve segment/playlist path relative to current manifest directory
        if manifest_dir:
            relative_key = f"{manifest_dir}/{stripped}"
        else:
            relative_key = stripped

        if stripped.endswith(".m3u8"):
            # Variant playlist -> proxy URL with token
            result.append(f"{relative_key}?token={token}")
        elif stripped.endswith(".ts"):
            # Segment -> presigned S3 URL (direct to S3). Keep the segment URL
            # TTL aligned with the (short) HLS token so a leaked manifest can't
            # outlive the token that produced it.
            s3_key = f"{s3_prefix}/{relative_key}"
            result.append(generate_presigned_get_url(s3_key, expires_in=HLS_TOKEN_TTL_MINUTES * 60))
        else:
            result.append(line)

    return "\n".join(result)


@router.get("/hls/{path:path}")
def hls_proxy(path: str, token: str = Query(...), db: Session = Depends(get_db)):
    """Proxy HLS manifests with URL rewriting for secure streaming."""
    claims = _decode_hls_token(token)
    s3_prefix = claims["pfx"]

    # Share-link-bound tokens must die with their link: re-check the live share
    # link before signing any segments, so revocation/expiry takes effect
    # immediately even while the JWT itself is still within its TTL. Member
    # (non-share) tokens carry no "sl" claim and skip this.
    _assert_share_link_still_valid(db, claims.get("sl"))

    # Only proxy m3u8 manifests
    if not path.endswith(".m3u8"):
        raise HTTPException(status_code=400, detail="Only .m3u8 files are proxied")

    # Prevent directory traversal
    normalised = posixpath.normpath(path)
    if normalised.startswith("..") or normalised.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Defense-in-depth: verify resolved key stays within the token's prefix
    s3_key = f"{s3_prefix}/{normalised}"
    if not s3_key.startswith(s3_prefix + "/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Fetch manifest from S3
    s3 = get_s3_client()
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        content = obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        raise HTTPException(status_code=404, detail="Manifest not found")
    except Exception as e:
        logger.error("Failed to fetch HLS manifest %s: %s", s3_key, e)
        raise HTTPException(status_code=404, detail="Manifest not found")

    rewritten = _rewrite_manifest(content, s3_prefix, normalised, token)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )
