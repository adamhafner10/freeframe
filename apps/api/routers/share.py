import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt

from fastapi import APIRouter, Depends, HTTPException, Query, status
import sqlalchemy
from sqlalchemy import func as sa_func, case
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware.auth import get_current_user, get_optional_user
from ..middleware.rate_limit import rate_limit
from ..models.user import User
from ..models.asset import Asset
from ..models.folder import Folder
from ..models.share import AssetShare, ShareLink, ShareLinkItem, SharePermission, ShareLinkActivity, ShareActivityAction
from ..models.activity import ActivityLog, ActivityAction, Notification, NotificationType
from ..models.approval import Approval, ApprovalStatus
from ..models.branding import ProjectBranding, WatermarkSettings, WatermarkContent
from ..models.asset import AssetVersion, AssetType, MediaFile, ProcessingStatus, HLSStatus
from ..models.comment import Comment
from ..schemas.share import (
    DirectShareCreate,
    DirectShareResponse,
    FolderShareAssetItem,
    FolderShareAssetsResponse,
    FolderShareSubfolder,
    MultiShareCreate,
    ShareLinkActivityResponse,
    ShareLinkCreate,
    ShareLinkListItem,
    ShareLinkResponse,
    ShareLinkUpdate,
    ShareLinkValidateResponse,
)
from ..services.permissions import get_project_member, require_project_role, validate_share_link, validate_share_link_with_session, validate_asset_in_share
from ..services.redis_service import (
    create_share_session,
    check_share_password_lockout,
    register_share_password_failure,
    reset_share_password_attempts,
)
from ..services.s3_service import generate_presigned_get_url
from ..routers.hls_proxy import create_hls_token
from ..services.crypto_service import encrypt_password, decrypt_password
from ..models.project import Project, ProjectRole
from ..schemas.approval import GuestApprovalCreate
from ..tasks.email_tasks import send_share_email, send_approval_email
from ..tasks.celery_app import send_task_safe
from ..config import settings

router = APIRouter(tags=["sharing"])


def _escape_like(s: str) -> str:
    """Escape special LIKE pattern characters to prevent injection."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Raw/processed master URLs served for inline VIEWING (not download) get a short
# TTL and an inline content-disposition so the browser plays them in place rather
# than handing the viewer a saveable full-res file.
_INLINE_STREAM_TTL_SECONDS = 1800


def _presigned_inline_stream_url(s3_key: str, expires_in: int = _INLINE_STREAM_TTL_SECONDS) -> str:
    """Presign a GET that the browser plays inline (Content-Disposition: inline),
    with a short TTL. Used for the raw/processed master fallback so a no-download
    share never hands out a long-lived, saveable full-res URL."""
    from ..services.s3_service import _get_presign_client

    s3 = _get_presign_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": s3_key,
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=expires_in,
    )


def _get_asset(db: Session, asset_id: uuid.UUID) -> Asset:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.deleted_at.is_(None)).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def _get_folder(db: Session, folder_id: uuid.UUID) -> Folder:
    folder = db.query(Folder).filter(Folder.id == folder_id, Folder.deleted_at.is_(None)).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


def _validate_asset_in_share(db: Session, link: ShareLink, asset: Asset) -> None:
    """Validate that an asset belongs to a share link (folder, asset, project, or multi-share)."""
    if link.folder_id:
        if asset.folder_id != link.folder_id:
            if not asset.folder_id or not _is_descendant_of(db, asset.folder_id, link.folder_id):
                raise HTTPException(status_code=403, detail="Asset is not within the shared folder")
    elif link.asset_id:
        if asset.id != link.asset_id:
            raise HTTPException(status_code=403, detail="Asset does not match share link")
    elif link.project_id:
        if asset.project_id != link.project_id:
            raise HTTPException(status_code=403, detail="Asset is not within the shared project")
        # For multi-share links, also check ShareLinkItem entries
        multi_items = db.query(ShareLinkItem).filter(ShareLinkItem.share_link_id == link.id).all()
        if multi_items:
            multi_asset_ids = {item.asset_id for item in multi_items if item.asset_id}
            multi_folder_ids = {item.folder_id for item in multi_items if item.folder_id}
            if asset.id not in multi_asset_ids:
                # Check if asset is in one of the shared folders
                if not any(asset.folder_id == fid or (asset.folder_id and _is_descendant_of(db, asset.folder_id, fid)) for fid in multi_folder_ids):
                    raise HTTPException(status_code=403, detail="Asset is not in the shared items")
    else:
        raise HTTPException(status_code=400, detail="Invalid share link")


def _get_project_id_from_link(db: Session, link: ShareLink) -> uuid.UUID:
    if link.project_id:
        return link.project_id
    if link.asset_id:
        asset = _get_asset(db, link.asset_id)
        return asset.project_id
    elif link.folder_id:
        folder = db.query(Folder).filter(Folder.id == link.folder_id, Folder.deleted_at.is_(None)).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Shared folder not found")
        return folder.project_id
    raise HTTPException(status_code=400, detail="Invalid share link")


def _log_share_activity(
    db: Session,
    share_link_id: uuid.UUID,
    action: ShareActivityAction,
    actor_email: str,
    actor_name: Optional[str] = None,
    asset_id: Optional[uuid.UUID] = None,
    asset_name: Optional[str] = None,
    dedup_seconds: int = 30,
):
    """Log share activity, skipping duplicates within dedup_seconds window."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=dedup_seconds)
        existing = db.query(ShareLinkActivity).filter(
            ShareLinkActivity.share_link_id == share_link_id,
            ShareLinkActivity.action == action,
            ShareLinkActivity.actor_email == actor_email,
            ShareLinkActivity.asset_id == asset_id,
            ShareLinkActivity.created_at >= cutoff,
        ).first()
        if existing:
            return
        activity = ShareLinkActivity(
            share_link_id=share_link_id,
            action=action,
            actor_email=actor_email,
            actor_name=actor_name,
            asset_id=asset_id,
            asset_name=asset_name,
        )
        db.add(activity)
        db.commit()
    except Exception:
        db.rollback()


def _is_descendant_of(db: Session, folder_id: uuid.UUID, ancestor_id: uuid.UUID) -> bool:
    """Check if folder_id is a descendant of ancestor_id via parent chain traversal."""
    current_id = folder_id
    visited = set()
    while current_id and current_id not in visited:
        if current_id == ancestor_id:
            return True
        visited.add(current_id)
        folder = db.query(Folder.parent_id).filter(Folder.id == current_id).first()
        current_id = folder.parent_id if folder else None
    return False


def _get_latest_media_file(db: Session, asset_id: uuid.UUID) -> Optional[MediaFile]:
    """Get the first media file from the latest ready version of an asset."""
    version = db.query(AssetVersion).filter(
        AssetVersion.asset_id == asset_id,
        AssetVersion.deleted_at.is_(None),
        AssetVersion.processing_status == ProcessingStatus.ready,
    ).order_by(AssetVersion.version_number.desc()).first()
    if not version:
        return None
    return db.query(MediaFile).filter(MediaFile.version_id == version.id).first()


def _bulk_latest_media_files(db: Session, asset_ids: list[uuid.UUID]) -> dict:
    """Map asset_id -> first MediaFile of its latest *ready* version, in bulk.

    Mirrors _get_latest_media_file's semantics (latest ready version, first media
    file) but resolves the whole page in 2 queries instead of ~2 per asset. Used
    to kill the N+1 on the folder/project share asset listing that external
    reviewers hit."""
    if not asset_ids:
        return {}

    # Latest READY version number per asset.
    latest_ready_subq = (
        db.query(
            AssetVersion.asset_id.label("asset_id"),
            sa_func.max(AssetVersion.version_number).label("max_version"),
        )
        .filter(
            AssetVersion.asset_id.in_(asset_ids),
            AssetVersion.deleted_at.is_(None),
            AssetVersion.processing_status == ProcessingStatus.ready,
        )
        .group_by(AssetVersion.asset_id)
        .subquery()
    )
    latest_versions = (
        db.query(AssetVersion)
        .join(
            latest_ready_subq,
            (AssetVersion.asset_id == latest_ready_subq.c.asset_id)
            & (AssetVersion.version_number == latest_ready_subq.c.max_version),
        )
        .all()
    )
    version_to_asset = {v.id: v.asset_id for v in latest_versions}
    version_ids = list(version_to_asset.keys())
    if not version_ids:
        return {}

    all_files = db.query(MediaFile).filter(MediaFile.version_id.in_(version_ids)).all()
    # First media file per version (preserve query order, matching .first()).
    media_by_asset: dict = {}
    for f in all_files:
        asset_id = version_to_asset.get(f.version_id)
        if asset_id is not None and asset_id not in media_by_asset:
            media_by_asset[asset_id] = f
    return media_by_asset


def _bulk_comment_counts(db: Session, asset_ids: list[uuid.UUID]) -> dict:
    """Map asset_id -> active comment count, in one grouped query."""
    if not asset_ids:
        return {}
    rows = (
        db.query(Comment.asset_id, sa_func.count(Comment.id))
        .filter(Comment.asset_id.in_(asset_ids), Comment.deleted_at.is_(None))
        .group_by(Comment.asset_id)
        .all()
    )
    return {asset_id: count for asset_id, count in rows}


def _bulk_creator_names(db: Session, creator_ids: list[uuid.UUID]) -> dict:
    """Map user_id -> name for a set of asset creators, in one query."""
    ids = [cid for cid in creator_ids if cid]
    if not ids:
        return {}
    rows = db.query(User.id, User.name).filter(User.id.in_(ids)).all()
    return {uid: name for uid, name in rows}


def _hls_is_ready(db: Session, media_file: Optional[MediaFile]) -> bool:
    """True only if the media file's version has a fully-completed HLS transcode.
    Guards against serving a stale/partial HLS prefix during the self-heal/retry
    window — fall back to the raw mp4 for playback when this is False."""
    if not media_file or not media_file.s3_key_processed:
        return False
    version = db.query(AssetVersion).filter(AssetVersion.id == media_file.version_id).first()
    return bool(version and version.hls_status == HLSStatus.ready)


def _project_watermark(db: Session, project_id: uuid.UUID) -> Optional[WatermarkSettings]:
    """Return the enabled project-level (share_link_id IS NULL) watermark settings,
    or None when watermarking isn't configured/enabled for the project."""
    wm = db.query(WatermarkSettings).filter(
        WatermarkSettings.project_id == project_id,
        WatermarkSettings.share_link_id.is_(None),
    ).first()
    if not wm or not wm.enabled:
        return None
    return wm


def _resolve_watermark_text(wm: WatermarkSettings, current_user: Optional[User]) -> str:
    """Resolve the static watermark text from project settings.

    Mirrors branding.apply_watermark_to_asset. Static design only — when the
    content type references the viewer but there's no authenticated user (public
    share), fall back to the custom text / empty string rather than inventing a
    per-viewer dynamic watermark."""
    if wm.content == WatermarkContent.email:
        return current_user.email if current_user else (wm.custom_text or "")
    if wm.content == WatermarkContent.name:
        if current_user:
            return current_user.name or current_user.email
        return wm.custom_text or ""
    return wm.custom_text or ""


def _enqueue_watermark(db: Session, asset_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Best-effort enqueue of the burn-watermark job for an asset, if the project
    has watermarking enabled. Idempotent at the worker (skips when the burned
    output already exists), so re-enqueueing on every share create/update is safe."""
    wm = _project_watermark(db, project_id)
    if not wm:
        return
    watermark_text = _resolve_watermark_text(wm, None)
    from ..tasks.watermark_tasks import apply_watermark
    send_task_safe(
        apply_watermark,
        str(asset_id),
        watermark_text,
        wm.position.value if hasattr(wm.position, "value") else wm.position,
        wm.opacity,
        None,  # image_key not stored in model
    )


def _enqueue_watermark_for_link(db: Session, link: ShareLink) -> None:
    """Enqueue watermark burns for every asset reachable through a share link
    when that link has show_watermark=True. Covers single-asset, folder, project,
    and multi-item links. No-op when the project has no enabled watermark."""
    if not link.show_watermark:
        return
    try:
        project_id = _get_project_id_from_link(db, link)
    except HTTPException:
        return
    wm = _project_watermark(db, project_id)
    if not wm:
        return

    asset_ids: set[uuid.UUID] = set()
    if link.asset_id:
        asset_ids.add(link.asset_id)
    # Folder / project / multi-item links: resolve member assets so each gets a burn.
    folder_ids: list[uuid.UUID] = []
    if link.folder_id:
        folder_ids.append(link.folder_id)
    items = db.query(ShareLinkItem).filter(ShareLinkItem.share_link_id == link.id).all()
    for item in items:
        if item.asset_id:
            asset_ids.add(item.asset_id)
        if item.folder_id:
            folder_ids.append(item.folder_id)
    if folder_ids:
        folder_assets = db.query(Asset.id).filter(
            Asset.folder_id.in_(folder_ids),
            Asset.deleted_at.is_(None),
            Asset.asset_type == AssetType.video,
        ).all()
        asset_ids.update(a.id for a in folder_assets)
    elif link.project_id and not link.asset_id and not items:
        # Whole-project share: burn every video asset in the project.
        project_assets = db.query(Asset.id).filter(
            Asset.project_id == link.project_id,
            Asset.deleted_at.is_(None),
            Asset.asset_type == AssetType.video,
        ).all()
        asset_ids.update(a.id for a in project_assets)

    for aid in asset_ids:
        _enqueue_watermark(db, aid, project_id)


def _watermarked_url(db: Session, media_file: Optional[MediaFile]) -> Optional[str]:
    """Presigned URL for the burned-watermark output if it exists, else None.

    Watermark output is a single burned mp4 (not HLS), served via direct presigned
    GET. None means the burn isn't ready yet — callers fall back to the clean source
    only after confirming the share doesn't require a watermark."""
    if not media_file or not media_file.s3_key_watermarked:
        return None
    return generate_presigned_get_url(media_file.s3_key_watermarked)


# ── Share links ───────────────────────────────────────────────────────────────

@router.post("/assets/{asset_id}/share", response_model=ShareLinkResponse, status_code=status.HTTP_201_CREATED)
def create_share_link(
    asset_id: uuid.UUID,
    body: ShareLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    require_project_role(db, asset.project_id, current_user, ProjectRole.editor)

    token = secrets.token_urlsafe(32)
    if body.password:
        pwd_bytes = body.password[:72].encode('utf-8')
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')
        password_encrypted = encrypt_password(body.password)
    else:
        password_hash = None
        password_encrypted = None

    link = ShareLink(
        asset_id=asset_id,
        token=token,
        created_by=current_user.id,
        title=body.title if body.title else asset.name,
        description=body.description,
        expires_at=body.expires_at,
        password_hash=password_hash,
        password_encrypted=password_encrypted,
        permission=body.permission,
        allow_download=body.allow_download,
        show_versions=body.show_versions,
        show_watermark=body.show_watermark,
        appearance=body.appearance.model_dump(),
    )
    db.add(link)
    db.add(ActivityLog(user_id=current_user.id, asset_id=asset_id, action=ActivityAction.shared))
    db.commit()
    db.refresh(link)
    _enqueue_watermark_for_link(db, link)
    return link


@router.get("/assets/{asset_id}/shares", response_model=list[ShareLinkResponse])
def list_share_links(
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    require_project_role(db, asset.project_id, current_user, ProjectRole.editor)
    return db.query(ShareLink).filter(
        ShareLink.asset_id == asset_id,
        ShareLink.deleted_at.is_(None),
    ).all()


@router.get("/share/{token}", response_model=ShareLinkValidateResponse, dependencies=[Depends(rate_limit("share_validate", 30, 60))])
def validate_share_link_endpoint(
    token: str,
    password: Optional[str] = None,
    log_open: bool = False,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. For secure links, requires authenticated user."""
    link = validate_share_link(db, token)

    # Check secure visibility — requires authenticated user
    if link.visibility == "secure":
        if not current_user:
            return ShareLinkValidateResponse(
                requires_auth=True,
                requires_password=False,
                title=link.title,
                permission=link.permission,
                visibility=link.visibility,
            )

    # Resolve folder name if this is a folder share
    folder_name = None
    project_name = None
    if link.folder_id:
        folder = db.query(Folder).filter(Folder.id == link.folder_id, Folder.deleted_at.is_(None)).first()
        if folder:
            folder_name = folder.name
    if link.project_id:
        project = db.query(Project).filter(Project.id == link.project_id, Project.deleted_at.is_(None)).first()
        if project:
            project_name = project.name

    session_id = None
    if link.password_hash:
        if not password:
            return ShareLinkValidateResponse(
                requires_password=True,
                title=link.title,
                permission=link.permission,
            )
        # Per-share-link brute-force lockout (independent of IP). Block before
        # spending a bcrypt check once too many wrong passwords have been tried.
        locked_out, retry_after = check_share_password_lockout(str(link.id))
        if locked_out:
            raise HTTPException(
                status_code=429,
                detail="Too many incorrect password attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        bad_password = False
        try:
            plain_bytes = password[:72].encode('utf-8')
            hashed_bytes = link.password_hash.encode('utf-8')
            if not bcrypt.checkpw(plain_bytes, hashed_bytes):
                bad_password = True
        except ValueError:
            bad_password = True
        if bad_password:
            register_share_password_failure(str(link.id))
            raise HTTPException(status_code=403, detail="Incorrect password")
        # Password verified — reset the failure counter and create a session so
        # subsequent requests skip re-verification.
        reset_share_password_attempts(str(link.id))
        session_id = secrets.token_urlsafe(32)
        create_share_session(token, session_id)

    if log_open:
        actor_email = current_user.email if current_user else "anonymous"
        actor_name = current_user.name if current_user else None
        _log_share_activity(db, link.id, ShareActivityAction.opened, actor_email=actor_email, actor_name=actor_name)

    # Build asset details for asset shares
    asset_data = None
    branding_data = None
    if link.asset_id:
        asset = _get_asset(db, link.asset_id)
        # Get thumbnail URL
        media_file = _get_latest_media_file(db, asset.id)
        thumbnail_url = None
        if media_file and media_file.s3_key_thumbnail:
            thumbnail_url = generate_presigned_get_url(media_file.s3_key_thumbnail)
        # Get stream URL — route HLS through our proxy so variant playlists +
        # segment URLs in the manifest stay signed; fall back to the original
        # file until HLS transcoding finishes.
        stream_url = None
        if media_file:
            is_video = asset.asset_type.value == "video"
            wm_url = None
            if link.show_watermark:
                # Watermarked share: serve the burned mp4 when it's ready. If the
                # burn isn't done yet, make sure the job is enqueued and fall back
                # to the clean source (existing safest behavior) until it lands.
                wm_url = _watermarked_url(db, media_file)
                if not wm_url:
                    _enqueue_watermark(db, asset.id, asset.project_id)
            if wm_url:
                stream_url = wm_url
            elif is_video and _hls_is_ready(db, media_file):
                token = create_hls_token(
                    media_file.s3_key_processed,
                    share_link_id=link.id,
                    user_id=current_user.id if current_user else None,
                )
                stream_url = f"{settings.frontend_url.rstrip('/')}/api/stream/hls/master.m3u8?token={token}"
            elif is_video and not link.allow_download:
                # HLS isn't ready AND downloads are disabled: do NOT hand out a
                # presigned GET to the full-res raw/processed master — that would
                # leak a saveable full-quality file from a no-download share. Leave
                # stream_url None; the viewer shows the hls_status processing state.
                stream_url = None
            elif media_file.s3_key_raw:
                # Download-enabled (or non-video) share: raw mp4 stays playable
                # inline while HLS is pending/failed so a transcode hiccup never
                # bricks playback. Short TTL + inline disposition.
                stream_url = _presigned_inline_stream_url(media_file.s3_key_raw)
            elif media_file.s3_key_processed:
                stream_url = _presigned_inline_stream_url(media_file.s3_key_processed)

        # Surface HLS transcode status so the share viewer can show a
        # "Processing…" state instead of a broken player when video isn't
        # playable yet. Falls back to None for non-video / no-version assets.
        hls_status_value = None
        if media_file:
            version = db.query(AssetVersion).filter(
                AssetVersion.id == media_file.version_id
            ).first()
            if version and version.hls_status is not None:
                hls_status_value = (
                    version.hls_status.value
                    if hasattr(version.hls_status, "value")
                    else str(version.hls_status)
                )

        asset_data = {
            "id": str(asset.id),
            "name": asset.name,
            "asset_type": asset.asset_type.value if hasattr(asset.asset_type, 'value') else str(asset.asset_type),
            "description": asset.description,
            "thumbnail_url": thumbnail_url,
            "stream_url": stream_url,
            "hls_status": hls_status_value,
        }
        # Get project branding
        branding = db.query(ProjectBranding).filter(
            ProjectBranding.project_id == asset.project_id
        ).first()
        if branding:
            branding_data = {
                "logo_url": branding.logo_s3_key,
                "primary_color": branding.primary_color,
                "custom_title": branding.custom_title,
                "custom_footer": branding.custom_footer,
            }

    # Resolve creator name
    creator = db.query(User).filter(User.id == link.created_by).first()
    created_by_name = creator.name if creator else None

    return ShareLinkValidateResponse(
        asset_id=link.asset_id,
        folder_id=link.folder_id,
        project_id=link.project_id,
        folder_name=folder_name,
        project_name=project_name,
        title=link.title,
        description=link.description,
        permission=link.permission,
        visibility=link.visibility,
        allow_download=link.allow_download,
        show_versions=link.show_versions,
        show_watermark=link.show_watermark,
        appearance=link.appearance,
        requires_password=False,
        created_by_name=created_by_name,
        viewer_name=current_user.name if current_user else None,
        viewer_email=current_user.email if current_user else None,
        asset=asset_data,
        branding=branding_data,
        share_session=session_id,
    )


def _can_reveal_share_password(db: Session, link: ShareLink, current_user: User) -> bool:
    """Plaintext share passwords may only be revealed to the link creator or to
    project owners/editors. Viewers/reviewers (and anyone reaching this via a
    public/secure share flow) must never see the decrypted password."""
    if link.created_by == current_user.id:
        return True
    project_id = _get_project_id_from_link(db, link)
    member = get_project_member(db, project_id, current_user.id)
    return bool(member and member.role in (ProjectRole.owner, ProjectRole.editor))


def _share_link_response(
    link: ShareLink,
    db: Optional[Session] = None,
    current_user: Optional[User] = None,
) -> ShareLinkResponse:
    """Build ShareLinkResponse from ORM model, computing has_password.

    The decrypted plaintext password (``password_value``) is ONLY populated when
    a privileged caller is supplied (the link creator or a project owner/editor).
    It is never returned to viewers/reviewers or via public/secure share flows."""
    response = ShareLinkResponse.model_validate(link)
    response.has_password = link.password_hash is not None and link.password_hash != ''
    response.password_value = None
    if (
        link.password_encrypted
        and db is not None
        and current_user is not None
        and _can_reveal_share_password(db, link, current_user)
    ):
        try:
            response.password_value = decrypt_password(link.password_encrypted)
        except Exception:
            response.password_value = None
    return response


# ── Authenticated share link details (for settings panel) ────────────────────

@router.get("/share/{token}/details", response_model=ShareLinkResponse)
def get_share_link_details(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authenticated endpoint returning full share link details for the settings panel."""
    link = db.query(ShareLink).filter(
        ShareLink.token == token,
        ShareLink.deleted_at.is_(None),
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    project_id = _get_project_id_from_link(db, link)
    require_project_role(db, project_id, current_user, ProjectRole.viewer)
    return _share_link_response(link, db=db, current_user=current_user)


# ── PATCH share link ─────────────────────────────────────────────────────────

@router.patch("/share/{token}", response_model=ShareLinkResponse)
def update_share_link(
    token: str,
    body: ShareLinkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    link = db.query(ShareLink).filter(ShareLink.token == token, ShareLink.deleted_at.is_(None)).first()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    project_id = _get_project_id_from_link(db, link)
    require_project_role(db, project_id, current_user, ProjectRole.editor)

    updates = body.model_dump(exclude_unset=True)

    # Handle password separately — hash + encrypt for reversible admin display
    if "password" in updates:
        raw_password = updates.pop("password")
        if raw_password:
            pwd_bytes = raw_password[:72].encode('utf-8')
            salt = bcrypt.gensalt()
            link.password_hash = bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')
            link.password_encrypted = encrypt_password(raw_password)
        else:
            link.password_hash = None
            link.password_encrypted = None

    # Convert appearance Pydantic model to dict
    if "appearance" in updates and updates["appearance"] is not None:
        updates["appearance"] = body.appearance.model_dump()

    for key, value in updates.items():
        setattr(link, key, value)

    db.commit()
    db.refresh(link)
    # If the link is (now) watermarked, make sure burns are enqueued for its assets.
    # Idempotent at the worker, so this is safe whether or not show_watermark changed.
    _enqueue_watermark_for_link(db, link)
    return _share_link_response(link, db=db, current_user=current_user)


@router.delete("/share/{token}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_share_link(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    link = db.query(ShareLink).filter(ShareLink.token == token, ShareLink.deleted_at.is_(None)).first()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    project_id = _get_project_id_from_link(db, link)
    require_project_role(db, project_id, current_user, ProjectRole.editor)
    link.deleted_at = datetime.now(timezone.utc)
    db.commit()


# ── Folder share links ───────────────────────────────────────────────────────

@router.post("/folders/{folder_id}/share", response_model=ShareLinkResponse, status_code=status.HTTP_201_CREATED)
def create_folder_share_link(
    folder_id: uuid.UUID,
    body: ShareLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.editor)

    token = secrets.token_urlsafe(32)
    if body.password:
        pwd_bytes = body.password[:72].encode('utf-8')
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')
        password_encrypted = encrypt_password(body.password)
    else:
        password_hash = None
        password_encrypted = None

    link = ShareLink(
        folder_id=folder_id,
        token=token,
        created_by=current_user.id,
        title=body.title if body.title else folder.name,
        description=body.description,
        expires_at=body.expires_at,
        password_hash=password_hash,
        password_encrypted=password_encrypted,
        permission=body.permission,
        allow_download=body.allow_download,
        show_versions=body.show_versions,
        show_watermark=body.show_watermark,
        appearance=body.appearance.model_dump(),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    _enqueue_watermark_for_link(db, link)
    return link


@router.post("/projects/{project_id}/share", response_model=ShareLinkResponse, status_code=status.HTTP_201_CREATED)
def create_project_share_link(
    project_id: uuid.UUID,
    body: ShareLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a share link for the project root (all root-level folders and assets)."""
    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    require_project_role(db, project_id, current_user, ProjectRole.editor)

    token = secrets.token_urlsafe(32)
    if body.password:
        pwd_bytes = body.password[:72].encode('utf-8')
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')
        password_encrypted = encrypt_password(body.password)
    else:
        password_hash = None
        password_encrypted = None

    link = ShareLink(
        project_id=project_id,
        token=token,
        created_by=current_user.id,
        title=body.title if body.title else project.name,
        description=body.description,
        expires_at=body.expires_at,
        password_hash=password_hash,
        password_encrypted=password_encrypted,
        permission=body.permission,
        allow_download=body.allow_download,
        show_versions=body.show_versions,
        show_watermark=body.show_watermark,
        appearance=body.appearance.model_dump(),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    _enqueue_watermark_for_link(db, link)
    return link


@router.post("/projects/{project_id}/share/user", response_model=DirectShareResponse, status_code=status.HTTP_201_CREATED)
def share_project_with_user(
    project_id: uuid.UUID,
    body: DirectShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Share entire project with a user by email or user_id. Sends notification email."""
    user_id = body.user_id
    if not user_id and body.email:
        from ..services.auth_service import get_user_by_email
        user = get_user_by_email(db, body.email)
        if user:
            user_id = user.id
        else:
            raise HTTPException(status_code=404, detail="User not found with that email")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id or email required")

    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    require_project_role(db, project_id, current_user, ProjectRole.editor)

    # For project shares, we store as an AssetShare with project_id context
    # Use the first root folder or create a project-level share
    # Send notification email
    shared_user = db.query(User).filter(User.id == user_id).first()
    if shared_user:
        if body.share_token:
            project_link = f"{settings.frontend_url}/share/{body.share_token}"
        else:
            project_link = f"{settings.frontend_url}/projects/{project_id}"
        send_task_safe(send_share_email,
            to_email=shared_user.email,
            sharer_name=current_user.name or current_user.email,
            asset_name=project.name,
            asset_link=project_link,
            permission=body.permission.value if body.permission else None,
        )

    return DirectShareResponse(
        id=uuid.uuid4(),
        asset_id=None,
        folder_id=None,
        shared_with_user_id=user_id,
        shared_with_team_id=None,
        permission=body.permission or "view",
        shared_by=current_user.id,
        created_at=datetime.now(timezone.utc),
    )


@router.get("/folders/{folder_id}/shares", response_model=list[ShareLinkResponse])
def list_folder_share_links(
    folder_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.viewer)
    return db.query(ShareLink).filter(
        ShareLink.folder_id == folder_id,
        ShareLink.deleted_at.is_(None),
    ).all()


# ── Folder direct user/team sharing ──────────────────────────────────────────

@router.post("/folders/{folder_id}/share/user", response_model=DirectShareResponse, status_code=status.HTTP_201_CREATED)
def share_folder_with_user(
    folder_id: uuid.UUID,
    body: DirectShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Resolve user_id from email if not provided
    user_id = body.user_id
    if not user_id and body.email:
        from ..services.auth_service import get_user_by_email
        user = get_user_by_email(db, body.email)
        if user:
            user_id = user.id
        else:
            raise HTTPException(status_code=404, detail="User not found with that email")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id or email required")

    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.editor)

    # Upsert: reactivate if soft-deleted
    existing = db.query(AssetShare).filter(
        AssetShare.folder_id == folder_id,
        AssetShare.shared_with_user_id == user_id,
    ).first()
    if existing:
        if existing.deleted_at is None:
            existing.permission = body.permission
        else:
            existing.deleted_at = None
            existing.permission = body.permission
        db.commit()
        db.refresh(existing)
        return existing

    share = AssetShare(
        folder_id=folder_id,
        shared_with_user_id=user_id,
        permission=body.permission,
        shared_by=current_user.id,
    )
    db.add(share)
    db.commit()
    db.refresh(share)

    # Send share email
    shared_user = db.query(User).filter(User.id == user_id).first()
    if shared_user:
        if body.share_token:
            folder_link = f"{settings.frontend_url}/share/{body.share_token}"
        else:
            folder_link = f"{settings.frontend_url}/projects/{folder.project_id}?folder={folder_id}"
        send_task_safe(send_share_email,
            to_email=shared_user.email,
            sharer_name=current_user.name or current_user.email,
            asset_name=folder.name,
            asset_link=folder_link,
            permission=body.permission.value if body.permission else None,
        )

    return share


@router.post("/folders/{folder_id}/share/team", response_model=DirectShareResponse, status_code=status.HTTP_201_CREATED)
def share_folder_with_team(
    folder_id: uuid.UUID,
    body: DirectShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.team_id:
        raise HTTPException(status_code=400, detail="team_id required")
    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.editor)

    existing = db.query(AssetShare).filter(
        AssetShare.folder_id == folder_id,
        AssetShare.shared_with_team_id == body.team_id,
    ).first()
    if existing:
        if existing.deleted_at is None:
            existing.permission = body.permission
        else:
            existing.deleted_at = None
            existing.permission = body.permission
        db.commit()
        db.refresh(existing)
        return existing

    share = AssetShare(
        folder_id=folder_id,
        shared_with_team_id=body.team_id,
        permission=body.permission,
        shared_by=current_user.id,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share


# ── Delete folder share ──────────────────────────────────────────────────────

@router.get("/folders/{folder_id}/direct-shares")
def list_folder_direct_shares(
    folder_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List direct user shares for a folder."""
    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.viewer)
    shares = db.query(AssetShare).filter(
        AssetShare.folder_id == folder_id,
        AssetShare.deleted_at.is_(None),
        AssetShare.shared_with_user_id.isnot(None),
    ).all()
    return [{"id": str(s.id), "shared_with_user_id": str(s.shared_with_user_id), "permission": s.permission.value} for s in shares]


@router.get("/assets/{asset_id}/direct-shares")
def list_asset_direct_shares(
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List direct user shares for an asset."""
    asset = _get_asset(db, asset_id)
    require_project_role(db, asset.project_id, current_user, ProjectRole.viewer)
    shares = db.query(AssetShare).filter(
        AssetShare.asset_id == asset_id,
        AssetShare.deleted_at.is_(None),
        AssetShare.shared_with_user_id.isnot(None),
    ).all()
    return [{"id": str(s.id), "shared_with_user_id": str(s.shared_with_user_id), "permission": s.permission.value} for s in shares]


@router.delete("/folders/{folder_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder_share(
    folder_id: uuid.UUID,
    share_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folder = _get_folder(db, folder_id)
    require_project_role(db, folder.project_id, current_user, ProjectRole.editor)

    share = db.query(AssetShare).filter(
        AssetShare.id == share_id,
        AssetShare.folder_id == folder_id,
        AssetShare.deleted_at.is_(None),
    ).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")

    share.deleted_at = datetime.now(timezone.utc)
    db.commit()


# ── Direct user/team sharing (assets) ────────────────────────────────────────

@router.post("/assets/{asset_id}/share/user", response_model=DirectShareResponse, status_code=status.HTTP_201_CREATED)
def share_with_user(
    asset_id: uuid.UUID,
    body: DirectShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Resolve user_id from email if not provided
    user_id = body.user_id
    if not user_id and body.email:
        from ..services.auth_service import get_user_by_email
        user = get_user_by_email(db, body.email)
        if user:
            user_id = user.id
        else:
            raise HTTPException(status_code=404, detail="User not found with that email")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id or email required")

    asset = _get_asset(db, asset_id)
    require_project_role(db, asset.project_id, current_user, ProjectRole.editor)

    # Upsert: reactivate if soft-deleted
    existing = db.query(AssetShare).filter(
        AssetShare.asset_id == asset_id,
        AssetShare.shared_with_user_id == user_id,
    ).first()
    if existing:
        if existing.deleted_at is None:
            existing.permission = body.permission
        else:
            existing.deleted_at = None
            existing.permission = body.permission
        db.commit()
        db.refresh(existing)
        return existing

    share = AssetShare(
        asset_id=asset_id,
        shared_with_user_id=user_id,
        permission=body.permission,
        shared_by=current_user.id,
    )
    db.add(share)
    db.add(ActivityLog(user_id=current_user.id, asset_id=asset_id, action=ActivityAction.shared))
    db.commit()
    db.refresh(share)

    # Send share email
    shared_user = db.query(User).filter(User.id == user_id).first()
    if shared_user:
        # Use share link URL if token provided, otherwise internal URL
        if body.share_token:
            asset_link = f"{settings.frontend_url}/share/{body.share_token}"
        else:
            asset_link = f"{settings.frontend_url}/projects/{asset.project_id}/assets/{asset_id}"
        send_task_safe(send_share_email,
            to_email=shared_user.email,
            sharer_name=current_user.name or current_user.email,
            asset_name=asset.name,
            asset_link=asset_link,
            permission=body.permission.value if body.permission else None,
        )

    return share


@router.post("/assets/{asset_id}/share/team", response_model=DirectShareResponse, status_code=status.HTTP_201_CREATED)
def share_with_team(
    asset_id: uuid.UUID,
    body: DirectShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.team_id:
        raise HTTPException(status_code=400, detail="team_id required")
    asset = _get_asset(db, asset_id)
    require_project_role(db, asset.project_id, current_user, ProjectRole.editor)

    existing = db.query(AssetShare).filter(
        AssetShare.asset_id == asset_id,
        AssetShare.shared_with_team_id == body.team_id,
    ).first()
    if existing:
        if existing.deleted_at is None:
            existing.permission = body.permission
        else:
            existing.deleted_at = None
            existing.permission = body.permission
        db.commit()
        db.refresh(existing)
        return existing

    share = AssetShare(
        asset_id=asset_id,
        shared_with_team_id=body.team_id,
        permission=body.permission,
        shared_by=current_user.id,
    )
    db.add(share)
    db.add(ActivityLog(user_id=current_user.id, asset_id=asset_id, action=ActivityAction.shared))
    db.commit()
    db.refresh(share)
    return share


# ── Project-level share link listing ──────────────────────────────────────────

@router.get("/projects/{project_id}/share-links", response_model=list[ShareLinkListItem])
def list_project_share_links(
    project_id: uuid.UUID,
    search: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_project_role(db, project_id, current_user, ProjectRole.viewer)

    # Subquery for view_count and last_viewed_at
    activity_stats = db.query(
        ShareLinkActivity.share_link_id,
        sa_func.count(case((ShareLinkActivity.action == ShareActivityAction.opened, 1))).label("view_count"),
        sa_func.max(ShareLinkActivity.created_at).label("last_viewed_at"),
    ).group_by(ShareLinkActivity.share_link_id).subquery()

    # Asset share links
    asset_query = (
        db.query(
            ShareLink.id,
            ShareLink.token,
            ShareLink.title,
            ShareLink.description,
            ShareLink.is_enabled,
            ShareLink.permission,
            sqlalchemy.literal("asset").label("share_type"),
            Asset.name.label("target_name"),
            sa_func.coalesce(activity_stats.c.view_count, 0).label("view_count"),
            activity_stats.c.last_viewed_at,
        )
        .join(Asset, ShareLink.asset_id == Asset.id)
        .outerjoin(activity_stats, ShareLink.id == activity_stats.c.share_link_id)
        .filter(
            Asset.project_id == project_id,
            ShareLink.deleted_at.is_(None),
            Asset.deleted_at.is_(None),
        )
    )

    # Folder share links
    folder_query = (
        db.query(
            ShareLink.id,
            ShareLink.token,
            ShareLink.title,
            ShareLink.description,
            ShareLink.is_enabled,
            ShareLink.permission,
            sqlalchemy.literal("folder").label("share_type"),
            Folder.name.label("target_name"),
            sa_func.coalesce(activity_stats.c.view_count, 0).label("view_count"),
            activity_stats.c.last_viewed_at,
        )
        .join(Folder, ShareLink.folder_id == Folder.id)
        .outerjoin(activity_stats, ShareLink.id == activity_stats.c.share_link_id)
        .filter(
            Folder.project_id == project_id,
            ShareLink.deleted_at.is_(None),
            Folder.deleted_at.is_(None),
        )
    )

    # Project root share links
    project_query = (
        db.query(
            ShareLink.id,
            ShareLink.token,
            ShareLink.title,
            ShareLink.description,
            ShareLink.is_enabled,
            ShareLink.permission,
            sqlalchemy.literal("folder").label("share_type"),
            ShareLink.title.label("target_name"),
            sa_func.coalesce(activity_stats.c.view_count, 0).label("view_count"),
            activity_stats.c.last_viewed_at,
        )
        .outerjoin(activity_stats, ShareLink.id == activity_stats.c.share_link_id)
        .filter(
            ShareLink.project_id == project_id,
            ShareLink.deleted_at.is_(None),
        )
    )

    if search:
        escaped = _escape_like(search)
        asset_query = asset_query.filter(ShareLink.title.ilike(f"%{escaped}%"))
        folder_query = folder_query.filter(ShareLink.title.ilike(f"%{escaped}%"))
        project_query = project_query.filter(ShareLink.title.ilike(f"%{escaped}%"))

    results = asset_query.union_all(folder_query).union_all(project_query).all()

    return [
        ShareLinkListItem(
            id=row.id,
            token=row.token,
            title=row.title,
            description=row.description,
            is_enabled=row.is_enabled,
            permission=row.permission,
            share_type=row.share_type,
            target_name=row.target_name,
            view_count=row.view_count,
            last_viewed_at=row.last_viewed_at,
        )
        for row in results
    ]


# ── Share link activity ───────────────────────────────────────────────────────

@router.get("/share/{token}/activity", response_model=list[ShareLinkActivityResponse])
def get_share_link_activity(
    token: str,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    link = db.query(ShareLink).filter(ShareLink.token == token, ShareLink.deleted_at.is_(None)).first()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    project_id = _get_project_id_from_link(db, link)
    require_project_role(db, project_id, current_user, ProjectRole.viewer)

    offset = (page - 1) * per_page
    activities = db.query(ShareLinkActivity).filter(
        ShareLinkActivity.share_link_id == link.id,
    ).order_by(ShareLinkActivity.created_at.desc()).offset(offset).limit(per_page).all()
    return activities


# ── Add asset to existing share link ──────────────────────────────────────────

@router.post("/share/{token}/add-asset/{asset_id}", status_code=status.HTTP_200_OK)
def add_asset_to_share_link(
    token: str,
    asset_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add an asset to an existing share link. Converts single-asset links to project-level."""
    link = db.query(ShareLink).filter(
        ShareLink.token == token,
        ShareLink.deleted_at.is_(None),
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.deleted_at.is_(None)).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Determine the share link's project
    link_project_id = _get_project_id_from_link(db, link)

    # Ensure caller has editor role
    if link_project_id:
        require_project_role(db, link_project_id, current_user, ProjectRole.editor)

    # Ensure the asset belongs to the same project
    if link_project_id and asset.project_id != link_project_id:
        raise HTTPException(status_code=403, detail="Asset does not belong to this share link's project")

    # Check if asset is already the direct target
    if link.asset_id == asset_id:
        return {"detail": "Asset already included in this share link"}

    # Check if asset is already in share_link_items
    existing_item = db.query(ShareLinkItem).filter(
        ShareLinkItem.share_link_id == link.id,
        ShareLinkItem.asset_id == asset_id,
    ).first()
    if existing_item:
        return {"detail": "Asset already included in this share link"}

    # If this is a single-asset share link, migrate to multi-item mode
    if link.asset_id and not link.project_id:
        old_asset_id = link.asset_id
        link.project_id = link_project_id
        link.asset_id = None
        db.flush()
        # Add the original asset as a ShareLinkItem
        db.add(ShareLinkItem(share_link_id=link.id, asset_id=old_asset_id))

    # If this is a folder-only share, migrate to multi-item mode
    if link.folder_id and not link.project_id:
        old_folder_id = link.folder_id
        link.project_id = link_project_id
        link.folder_id = None
        db.flush()
        # Add the original folder as a ShareLinkItem
        db.add(ShareLinkItem(share_link_id=link.id, folder_id=old_folder_id))

    # Set project_id if not yet set
    if not link.project_id:
        link.project_id = link_project_id or asset.project_id
        db.flush()

    # Add the new asset
    db.add(ShareLinkItem(share_link_id=link.id, asset_id=asset_id))
    db.commit()
    db.refresh(link)
    # If this link is watermarked, enqueue a burn for the newly added asset so its
    # watermarked output is ready when a viewer requests playback.
    if link.show_watermark and asset.asset_type == AssetType.video:
        wm = _project_watermark(db, asset.project_id)
        if wm:
            _enqueue_watermark(db, asset.id, asset.project_id)
    return {"detail": "Asset added to share link"}


@router.post("/projects/{project_id}/share/multi", response_model=ShareLinkResponse, status_code=status.HTTP_201_CREATED)
def create_multi_share_link(
    project_id: uuid.UUID,
    body: MultiShareCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a single share link containing multiple selected assets and/or folders."""
    project = db.query(Project).filter(Project.id == project_id, Project.deleted_at.is_(None)).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    require_project_role(db, project_id, current_user, ProjectRole.editor)

    if not body.asset_ids and not body.folder_ids:
        raise HTTPException(status_code=400, detail="At least one asset or folder is required")

    # Validate all assets belong to this project
    for aid in body.asset_ids:
        asset = db.query(Asset).filter(Asset.id == aid, Asset.deleted_at.is_(None)).first()
        if not asset or asset.project_id != project_id:
            raise HTTPException(status_code=400, detail=f"Asset {aid} not found in this project")

    # Validate all folders belong to this project
    for fid in body.folder_ids:
        folder = db.query(Folder).filter(Folder.id == fid, Folder.deleted_at.is_(None)).first()
        if not folder or folder.project_id != project_id:
            raise HTTPException(status_code=400, detail=f"Folder {fid} not found in this project")

    # Determine title
    title = body.title
    if not title:
        count = len(body.asset_ids) + len(body.folder_ids)
        title = f"{count} items"

    token = secrets.token_urlsafe(32)
    password_hash = None
    password_encrypted = None
    if body.password:
        plain_bytes = body.password[:72].encode("utf-8")
        password_hash = bcrypt.hashpw(plain_bytes, bcrypt.gensalt()).decode("utf-8")
        try:
            password_encrypted = encrypt_password(body.password)
        except Exception:
            pass

    link = ShareLink(
        project_id=project_id,
        token=token,
        title=title,
        description=None,
        is_enabled=True,
        permission=body.permission,
        visibility=body.visibility,
        allow_download=body.allow_download,
        show_versions=body.show_versions,
        show_watermark=body.show_watermark,
        password_hash=password_hash,
        password_encrypted=password_encrypted,
        expires_at=body.expires_at,
        appearance=body.appearance.model_dump(),
        created_by=current_user.id,
    )
    db.add(link)
    db.flush()

    # Insert share_link_items
    for aid in body.asset_ids:
        db.add(ShareLinkItem(share_link_id=link.id, asset_id=aid))
    for fid in body.folder_ids:
        db.add(ShareLinkItem(share_link_id=link.id, folder_id=fid))

    db.commit()
    db.refresh(link)
    _enqueue_watermark_for_link(db, link)
    return link


# ── Guest approve / reject (public share endpoints) ──────────────────────────

def _decide_via_share(
    token: str,
    decision: ApprovalStatus,
    body: GuestApprovalCreate,
    share_session: Optional[str],
    db: Session,
    current_user: Optional[User],
) -> dict:
    """Shared implementation for guest/member approve + reject on a share link.

    Mirrors guest_comment's resolution chain: validate the (possibly secure /
    password-protected) link, require approve permission, scope-check the target
    asset, resolve the latest ready version, then upsert an Approval record keyed
    by the authenticated user or, for guests, by guest_email. Writes an
    ActivityLog (when a member) + a ShareLinkActivity row, and best-effort emails
    the asset creator like the authenticated approvals router does."""
    link = validate_share_link_with_session(
        db, token, share_session=share_session, current_user=current_user
    )

    # Only links granted approve permission may record decisions.
    if link.permission != SharePermission.approve:
        raise HTTPException(status_code=403, detail="This share link does not allow approvals")

    # Resolve asset_id: from body, link, or error — then scope-check it.
    target_asset_id = body.asset_id or link.asset_id
    if not target_asset_id:
        raise HTTPException(status_code=400, detail="asset_id is required for folder/project shares")
    asset = validate_asset_in_share(db, link, target_asset_id)

    # Resolve version_id: use provided or the latest ready version (mirrors guest_comment).
    version_id = body.version_id
    if not version_id:
        latest = db.query(AssetVersion).filter(
            AssetVersion.asset_id == asset.id,
            AssetVersion.deleted_at.is_(None),
            AssetVersion.processing_status == ProcessingStatus.ready,
        ).order_by(AssetVersion.version_number.desc()).first()
        if not latest:
            raise HTTPException(status_code=400, detail="No ready version found for this asset")
        version_id = latest.id

    # Determine actor: authenticated member, else guest identity from the body.
    guest_email: Optional[str] = None
    guest_name: Optional[str] = None
    if current_user is None:
        if not body.guest_email or not body.guest_name:
            raise HTTPException(
                status_code=400,
                detail="guest_email and guest_name required for anonymous approvals",
            )
        guest_email = body.guest_email.lower()
        guest_name = body.guest_name

    # Upsert: one decision per (version, member) or per (version, guest_email).
    if current_user is not None:
        existing = db.query(Approval).filter(
            Approval.asset_id == asset.id,
            Approval.version_id == version_id,
            Approval.user_id == current_user.id,
            Approval.deleted_at.is_(None),
        ).first()
    else:
        existing = db.query(Approval).filter(
            Approval.asset_id == asset.id,
            Approval.version_id == version_id,
            Approval.guest_email == guest_email,
            Approval.deleted_at.is_(None),
        ).first()

    if existing:
        existing.status = decision
        existing.note = body.note
        if current_user is None:
            existing.guest_name = guest_name
        approval = existing
    else:
        approval = Approval(
            asset_id=asset.id,
            version_id=version_id,
            user_id=current_user.id if current_user else None,
            guest_email=guest_email,
            guest_name=guest_name,
            status=decision,
            note=body.note,
        )
        db.add(approval)

    # ActivityLog requires a user_id (NOT NULL FK) — only write it for members.
    if current_user is not None:
        db.add(ActivityLog(
            user_id=current_user.id,
            asset_id=asset.id,
            action=ActivityAction.approved if decision == ApprovalStatus.approved else ActivityAction.rejected,
        ))
        if asset.created_by and asset.created_by != current_user.id:
            db.add(Notification(user_id=asset.created_by, type=NotificationType.approval, asset_id=asset.id))

    db.commit()
    db.refresh(approval)

    # Share link activity (attributed to member or guest).
    actor_email = current_user.email if current_user else (guest_email or "anonymous")
    actor_name = current_user.name if current_user else guest_name
    _log_share_activity(
        db, link.id,
        ShareActivityAction.approved if decision == ApprovalStatus.approved else ShareActivityAction.rejected,
        actor_email=actor_email,
        actor_name=actor_name,
        asset_id=asset.id,
        asset_name=asset.name,
    )

    # Best-effort: notify the asset creator by email (matches approvals.py).
    if asset.created_by and (current_user is None or asset.created_by != current_user.id):
        creator = db.query(User).filter(User.id == asset.created_by).first()
        if creator:
            asset_link = f"{settings.frontend_url}/assets/{asset.id}"
            send_task_safe(
                send_approval_email,
                to_email=creator.email,
                reviewer_name=actor_name or actor_email,
                asset_name=asset.name,
                status="approved" if decision == ApprovalStatus.approved else "rejected",
                asset_link=asset_link,
                note=body.note,
            )

    return {
        "status": approval.status.value if hasattr(approval.status, "value") else str(approval.status),
        "asset_id": str(asset.id),
        "version_id": str(version_id),
    }


@router.post("/share/{token}/approve", status_code=status.HTTP_200_OK)
def share_approve(
    token: str,
    body: GuestApprovalCreate,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. Record an approval on a share link
    (permission must be 'approve'). Works for guests (guest_email/guest_name) or
    authenticated members (recorded against their user_id)."""
    return _decide_via_share(token, ApprovalStatus.approved, body, share_session, db, current_user)


@router.post("/share/{token}/reject", status_code=status.HTTP_200_OK)
def share_reject(
    token: str,
    body: GuestApprovalCreate,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. Record a rejection on a share link
    (permission must be 'approve'). Works for guests or authenticated members."""
    return _decide_via_share(token, ApprovalStatus.rejected, body, share_session, db, current_user)


# ── Folder share public endpoints ─────────────────────────────────────────────

@router.get("/share/{token}/assets", response_model=FolderShareAssetsResponse)
def get_folder_share_assets(
    token: str,
    folder_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. Returns assets and subfolders for a folder or project share link.
    Secure + password-protected links are enforced via the session-aware validator."""
    link = validate_share_link_with_session(db, token, share_session=share_session, current_user=current_user)

    is_project_share = link.project_id is not None
    if not link.folder_id and not is_project_share:
        raise HTTPException(status_code=400, detail="This share link is not a folder or project share")

    # Check if this is a multi-share (project_id set with items in share_link_items)
    multi_share_items = db.query(ShareLinkItem).filter(ShareLinkItem.share_link_id == link.id).all() if is_project_share else []
    is_multi_share = len(multi_share_items) > 0

    # For multi-share links at the root level, return only the selected items
    if is_multi_share and not folder_id:
        multi_asset_ids = [item.asset_id for item in multi_share_items if item.asset_id]
        multi_folder_ids = [item.folder_id for item in multi_share_items if item.folder_id]

        # Get shared folders
        subfolder_items = []
        if multi_folder_ids:
            shared_folders = db.query(Folder).filter(
                Folder.id.in_(multi_folder_ids),
                Folder.deleted_at.is_(None),
            ).order_by(Folder.name).all()
            # Pre-collect up to 4 preview assets per folder, then bulk-resolve
            # their thumbnails in one media-file lookup (no per-asset N+1).
            preview_assets_by_sf: dict = {}
            all_preview_ids: list[uuid.UUID] = []
            for sf in shared_folders:
                pa_list = db.query(Asset).filter(
                    Asset.folder_id == sf.id, Asset.deleted_at.is_(None),
                ).order_by(Asset.created_at.desc()).limit(4).all()
                preview_assets_by_sf[sf.id] = pa_list
                all_preview_ids.extend(pa.id for pa in pa_list)
            preview_media = _bulk_latest_media_files(db, all_preview_ids)
            for sf in shared_folders:
                asset_count = db.query(sa_func.count(Asset.id)).filter(
                    Asset.folder_id == sf.id, Asset.deleted_at.is_(None),
                ).scalar() or 0
                child_folder_count = db.query(sa_func.count(Folder.id)).filter(
                    Folder.parent_id == sf.id, Folder.deleted_at.is_(None),
                ).scalar() or 0
                thumb_urls: list[str] = []
                for pa in preview_assets_by_sf.get(sf.id, []):
                    mf = preview_media.get(pa.id)
                    if mf and mf.s3_key_thumbnail:
                        thumb_urls.append(generate_presigned_get_url(mf.s3_key_thumbnail))
                subfolder_items.append(FolderShareSubfolder(
                    id=sf.id, name=sf.name, item_count=asset_count + child_folder_count, thumbnail_urls=thumb_urls,
                ))

        # Get shared assets (batch-loaded: media files + comment counts resolved
        # for the whole page in 2 grouped queries instead of ~2 per asset).
        asset_items = []
        if multi_asset_ids:
            total = len(multi_asset_ids)
            offset = (page - 1) * per_page
            shared_assets = db.query(Asset).filter(
                Asset.id.in_(multi_asset_ids), Asset.deleted_at.is_(None),
            ).order_by(Asset.created_at.desc()).offset(offset).limit(per_page).all()
            page_ids = [a.id for a in shared_assets]
            media_by_asset = _bulk_latest_media_files(db, page_ids)
            comment_counts = _bulk_comment_counts(db, page_ids)
            for a in shared_assets:
                mf = media_by_asset.get(a.id)
                thumbnail_url = generate_presigned_get_url(mf.s3_key_thumbnail) if mf and mf.s3_key_thumbnail else None
                comment_count = comment_counts.get(a.id, 0)
                asset_items.append(FolderShareAssetItem(
                    id=a.id, name=a.name, asset_type=a.asset_type.value if hasattr(a.asset_type, 'value') else str(a.asset_type),
                    thumbnail_url=thumbnail_url, created_at=a.created_at.isoformat() if a.created_at else "",
                    file_size_bytes=mf.file_size_bytes if mf else 0, comment_count=comment_count,
                ))
        else:
            total = 0

        return FolderShareAssetsResponse(
            subfolders=subfolder_items, assets=asset_items, total=total, page=page, per_page=per_page,
        )

    # Determine which folder to list contents from
    # For project shares, target_folder_id=None means project root
    target_folder_id = link.folder_id  # None for project root shares
    if folder_id:
        if is_project_share:
            # Project share: validate folder belongs to this project
            f = db.query(Folder).filter(Folder.id == folder_id, Folder.deleted_at.is_(None)).first()
            if not f or f.project_id != link.project_id:
                raise HTTPException(status_code=403, detail="Folder is not within the shared project")
        elif folder_id != link.folder_id and not _is_descendant_of(db, folder_id, link.folder_id):
            raise HTTPException(status_code=403, detail="Folder is not within the shared folder")
        target_folder_id = folder_id

    # Get subfolders
    if target_folder_id:
        subfolder_filter = Folder.parent_id == target_folder_id
    else:
        # Project root: folders with no parent in this project
        subfolder_filter = sqlalchemy.and_(
            Folder.parent_id.is_(None),
            Folder.project_id == link.project_id,
        )
    subfolders_query = db.query(Folder).filter(
        subfolder_filter,
        Folder.deleted_at.is_(None),
    ).order_by(Folder.name).all()

    # Pre-collect up to 4 preview assets per subfolder, then bulk-resolve their
    # thumbnails in one media-file lookup instead of one per preview asset.
    preview_assets_by_sf: dict = {}
    all_preview_ids: list[uuid.UUID] = []
    for sf in subfolders_query:
        pa_list = db.query(Asset).filter(
            Asset.folder_id == sf.id,
            Asset.deleted_at.is_(None),
        ).order_by(Asset.created_at.desc()).limit(4).all()
        preview_assets_by_sf[sf.id] = pa_list
        all_preview_ids.extend(pa.id for pa in pa_list)
    preview_media = _bulk_latest_media_files(db, all_preview_ids)

    subfolder_items = []
    for sf in subfolders_query:
        # Count assets + direct child folders in this subfolder
        asset_count = db.query(sa_func.count(Asset.id)).filter(
            Asset.folder_id == sf.id,
            Asset.deleted_at.is_(None),
        ).scalar() or 0
        child_folder_count = db.query(sa_func.count(Folder.id)).filter(
            Folder.parent_id == sf.id,
            Folder.deleted_at.is_(None),
        ).scalar() or 0

        # Up to 4 thumbnail previews from assets inside this subfolder
        thumb_urls: list[str] = []
        for pa in preview_assets_by_sf.get(sf.id, []):
            mf = preview_media.get(pa.id)
            if mf and mf.s3_key_thumbnail:
                thumb_urls.append(generate_presigned_get_url(mf.s3_key_thumbnail))
            if len(thumb_urls) >= 4:
                break

        subfolder_items.append(FolderShareSubfolder(
            id=sf.id,
            name=sf.name,
            item_count=asset_count + child_folder_count,
            thumbnail_urls=thumb_urls,
        ))

    # Get assets in this folder (or project root if target_folder_id is None)
    if target_folder_id:
        asset_filter = Asset.folder_id == target_folder_id
    else:
        # Project root: assets with no folder in this project
        asset_filter = sqlalchemy.and_(
            Asset.folder_id.is_(None),
            Asset.project_id == link.project_id,
        )
    total = db.query(sa_func.count(Asset.id)).filter(
        asset_filter,
        Asset.deleted_at.is_(None),
    ).scalar() or 0

    offset = (page - 1) * per_page
    assets = db.query(Asset).filter(
        asset_filter,
        Asset.deleted_at.is_(None),
    ).order_by(Asset.created_at.desc()).offset(offset).limit(per_page).all()

    # Batch-load media files, comment counts, and creator names for the whole
    # page (3 grouped queries) instead of ~4 serial queries per asset.
    page_ids = [a.id for a in assets]
    media_by_asset = _bulk_latest_media_files(db, page_ids)
    comment_counts = _bulk_comment_counts(db, page_ids)
    creator_names = _bulk_creator_names(db, [a.created_by for a in assets])

    asset_items = []
    for asset in assets:
        thumbnail_url = None
        file_size = None
        duration_seconds = None
        media_file = media_by_asset.get(asset.id)
        if media_file:
            if media_file.s3_key_thumbnail:
                thumbnail_url = generate_presigned_get_url(media_file.s3_key_thumbnail)
            file_size = media_file.file_size_bytes
            duration_seconds = media_file.duration_seconds

        comment_count = comment_counts.get(asset.id, 0)

        # Creator name (resolved from the bulk map).
        creator_name = creator_names.get(asset.created_by) if asset.created_by else None

        asset_items.append(FolderShareAssetItem(
            id=asset.id,
            name=asset.name,
            asset_type=asset.asset_type.value,
            thumbnail_url=thumbnail_url,
            file_size=file_size,
            duration_seconds=duration_seconds,
            comment_count=comment_count,
            created_by_name=creator_name,
            created_at=asset.created_at,
        ))

    return FolderShareAssetsResponse(
        assets=asset_items,
        subfolders=subfolder_items,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/share/{token}/stream/{asset_id}")
def get_share_stream_url(
    token: str,
    asset_id: uuid.UUID,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. Returns presigned stream URL for an asset in a share link."""
    link = validate_share_link_with_session(db, token, share_session=share_session, current_user=current_user)

    asset = _get_asset(db, asset_id)

    # Validate asset belongs to this share
    _validate_asset_in_share(db, link, asset)

    media_file = _get_latest_media_file(db, asset.id)
    if not media_file:
        raise HTTPException(status_code=404, detail="No ready media file found")

    # Watermarked share: serve the burned mp4 in place of the clean original/HLS
    # when it's ready. If not ready yet, ensure the burn is enqueued and fall back
    # to the clean playback chain below (existing safest behavior).
    wm_url = None
    if link.show_watermark and asset.asset_type == AssetType.video:
        wm_url = _watermarked_url(db, media_file)
        if not wm_url:
            _enqueue_watermark(db, asset.id, asset.project_id)

    # Surface HLS transcode status so the viewer can show a "Processing…" state
    # instead of a broken player (and so we can signal when we intentionally
    # withhold the raw master on a no-download share).
    hls_status_value = None
    if media_file and media_file.version_id:
        version = db.query(AssetVersion).filter(
            AssetVersion.id == media_file.version_id
        ).first()
        if version and version.hls_status is not None:
            hls_status_value = (
                version.hls_status.value
                if hasattr(version.hls_status, "value")
                else str(version.hls_status)
            )

    # Playback URL: HLS goes through the proxy so variant + segment URLs stay
    # signed; video w/o HLS yet serves the original file directly.
    if wm_url:
        url = wm_url
    elif asset.asset_type == AssetType.video:
        if _hls_is_ready(db, media_file):
            hls_token = create_hls_token(
                media_file.s3_key_processed,
                share_link_id=link.id,
                user_id=current_user.id if current_user else None,
            )
            url = f"{settings.frontend_url.rstrip('/')}/api/stream/hls/master.m3u8?token={hls_token}"
        elif not link.allow_download:
            # HLS isn't ready AND downloads are disabled. Serving a presigned GET
            # to the raw/processed master here would leak a saveable full-res file
            # from a no-download share. Withhold the URL and signal processing/
            # unavailable instead (the viewer renders the hls_status state).
            return {
                "url": None,
                "asset_type": asset.asset_type.value,
                "name": asset.name,
                "version_id": str(media_file.version_id) if media_file.version_id else None,
                "thumbnail_url": (
                    generate_presigned_get_url(media_file.s3_key_thumbnail)
                    if media_file.s3_key_thumbnail else None
                ),
                "duration_seconds": media_file.duration_seconds,
                "hls_status": hls_status_value,
            }
        elif media_file.s3_key_raw:
            # Download-enabled share, HLS pending/failed: serve the raw mp4 for
            # inline playback so a transcode hiccup never bricks viewing. Short
            # TTL + inline disposition.
            url = _presigned_inline_stream_url(media_file.s3_key_raw)
        else:
            raise HTTPException(status_code=404, detail="No playable media found")
    else:
        # A non-video original is served as a direct file download — gate it
        # behind allow_download.
        if not link.allow_download:
            raise HTTPException(status_code=403, detail="Downloads are not allowed for this share link")
        s3_key = media_file.s3_key_processed or media_file.s3_key_raw
        url = generate_presigned_get_url(s3_key)

    # Log viewed_asset activity
    _log_share_activity(
        db, link.id, ShareActivityAction.viewed_asset,
        actor_email=current_user.email if current_user else "anonymous",
        actor_name=current_user.name if current_user else None,
        asset_id=asset.id,
        asset_name=asset.name,
    )

    # Get thumbnail URL
    thumb_url = None
    if media_file.s3_key_thumbnail:
        thumb_url = generate_presigned_get_url(media_file.s3_key_thumbnail)

    return {
        "url": url,
        "asset_type": asset.asset_type.value,
        "name": asset.name,
        "version_id": str(media_file.version_id) if media_file.version_id else None,
        "thumbnail_url": thumb_url,
        "duration_seconds": media_file.duration_seconds,
        "hls_status": hls_status_value,
    }


@router.get("/share/{token}/thumbnail/{asset_id}")
def get_share_thumbnail_url(
    token: str,
    asset_id: uuid.UUID,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — optional auth. Returns presigned thumbnail URL for an asset in a share link.
    Secure + password-protected links are enforced via the session-aware validator."""
    link = validate_share_link_with_session(db, token, share_session=share_session, current_user=current_user)

    asset = _get_asset(db, asset_id)

    # Validate asset belongs to this share
    _validate_asset_in_share(db, link, asset)

    media_file = _get_latest_media_file(db, asset.id)
    if not media_file or not media_file.s3_key_thumbnail:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    url = generate_presigned_get_url(media_file.s3_key_thumbnail)
    return {"url": url}
