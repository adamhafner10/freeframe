import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware.auth import get_current_user, get_optional_user
from ..middleware.share_auth import get_share_link
from ..models.asset import Asset
from ..models.project import ProjectMember, ProjectRole
from ..models.comment import Annotation, Comment, CommentAttachment, CommentReaction
from ..models.activity import Mention, Notification, NotificationType, ActivityLog, ActivityAction
from ..models.user import User, GuestUser
from ..models.share import ShareLink, ShareLinkActivity, ShareActivityAction, SharePermission
from ..schemas.comment import (
    AnnotationResponse,
    AttachmentResponse,
    AttachmentUploadRequest,
    AttachmentUploadResponse,
    AuthorInfo,
    GuestAuthorInfo,
    CommentCreate,
    CommentResponse,
    CommentUpdate,
    GuestCommentCreate,
    ReactionCreate,
    ReactionResponse,
)
from ..services import s3_service
from ..services.permissions import (
    get_asset_share_permission,
    get_project_member,
    require_asset_access,
    require_project_role,
    validate_asset_in_share,
    validate_share_link_with_session,
)
from ..tasks.email_tasks import send_mention_email, send_comment_email
from ..tasks.celery_app import send_task_safe

router = APIRouter(tags=["comments"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_asset(db: Session, asset_id: uuid.UUID) -> Asset:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.deleted_at.is_(None)).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def _get_comment(db: Session, comment_id: uuid.UUID) -> Comment:
    comment = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return comment


def _require_comment_write_access(db: Session, asset: Asset, user: User) -> None:
    """Gate comment write/resolve actions behind a minimum capability.

    A project member needs at least the `reviewer` role; a viewer is read-only.
    A non-member who reaches the asset via a direct share needs at least
    `comment` permission. Read access (require_asset_access) stays open to all.
    """
    PERM_RANK = {
        SharePermission.approve: 3,
        SharePermission.comment: 2,
        SharePermission.view: 1,
    }
    # Asset creator always retains write access.
    if asset.created_by == user.id:
        return
    member = get_project_member(db, asset.project_id, user.id)
    if member:
        require_project_role(db, asset.project_id, user, ProjectRole.reviewer)
        return
    # Non-member: must hold at least `comment` permission via a direct share
    permission = get_asset_share_permission(db, asset, user)
    if PERM_RANK[permission] < PERM_RANK[SharePermission.comment]:
        raise HTTPException(status_code=403, detail="You do not have permission to comment")


def _build_attachment_response(attachment: CommentAttachment) -> AttachmentResponse:
    url = s3_service.generate_presigned_get_url(attachment.s3_key, expires_in=3600)
    return AttachmentResponse(
        id=attachment.id,
        file_name=attachment.original_filename,
        file_size=attachment.file_size_bytes,
        content_type=attachment.file_type,
        url=url,
    )


def _build_reaction_responses(
    reactions: list[CommentReaction],
    current_user_id: uuid.UUID | None,
) -> list[ReactionResponse]:
    counts: dict[str, int] = defaultdict(int)
    reacted: dict[str, bool] = defaultdict(bool)
    for r in reactions:
        counts[r.emoji] += 1
        if current_user_id and r.user_id == current_user_id:
            reacted[r.emoji] = True
    return [
        ReactionResponse(emoji=emoji, count=cnt, reacted=reacted[emoji])
        for emoji, cnt in counts.items()
    ]


def _build_comment_response(
    comment: Comment,
    db: Session,
    current_user_id: uuid.UUID | None = None,
    depth: int = 5,
) -> CommentResponse:
    annotation = db.query(Annotation).filter(Annotation.comment_id == comment.id).first()
    replies_raw = []
    if depth > 0:
        replies_raw = db.query(Comment).filter(
            Comment.parent_id == comment.id,
            Comment.deleted_at.is_(None),
        ).order_by(Comment.created_at).all()

    # Load attachments
    attachments_raw = db.query(CommentAttachment).filter(
        CommentAttachment.comment_id == comment.id,
    ).all()
    attachments = [_build_attachment_response(a) for a in attachments_raw]

    # Load reactions
    reactions_raw = db.query(CommentReaction).filter(
        CommentReaction.comment_id == comment.id,
    ).all()
    reactions = _build_reaction_responses(reactions_raw, current_user_id)

    # Load author info
    author_info = None
    if comment.author_id:
        author = db.query(User).filter(User.id == comment.author_id).first()
        if author:
            author_info = AuthorInfo(id=author.id, name=author.name, avatar_url=author.avatar_url)

    guest_author_info = None
    if comment.guest_author_id:
        guest = db.query(GuestUser).filter(GuestUser.id == comment.guest_author_id).first()
        if guest:
            guest_author_info = GuestAuthorInfo(id=guest.id, name=guest.name, email=guest.email)

    resp = CommentResponse.model_validate(comment)
    resp.author = author_info
    resp.guest_author = guest_author_info
    resp.annotation = AnnotationResponse.model_validate(annotation) if annotation else None
    resp.replies = [
        _build_comment_response(r, db, current_user_id=current_user_id, depth=depth - 1)
        for r in replies_raw
    ]
    resp.attachments = attachments
    resp.reactions = reactions
    return resp


def _get_annotations_map(comment_ids: list[uuid.UUID], db: Session) -> dict:
    """Batch-load annotations for a list of comment IDs."""
    if not comment_ids:
        return {}
    annotations = db.query(Annotation).filter(Annotation.comment_id.in_(comment_ids)).all()
    return {a.comment_id: a for a in annotations}


def _build_comment_tree_batched(
    top_level: list[Comment],
    all_comments: list[Comment],
    db: Session,
    current_user_id: uuid.UUID | None = None,
    depth: int = 5,
) -> list[CommentResponse]:
    """Build the full comment tree for `top_level` using a fixed number of
    batched queries instead of the per-comment recursion in
    `_build_comment_response`. Produces the identical response shape.

    `all_comments` must contain every non-deleted comment for the asset
    (top-level + replies) so the reply tree can be assembled in memory.
    """
    # Index replies by parent so the tree assembles without per-node queries.
    children_by_parent: dict[uuid.UUID, list[Comment]] = defaultdict(list)
    for c in all_comments:
        if c.parent_id is not None:
            children_by_parent[c.parent_id].append(c)
    for kids in children_by_parent.values():
        kids.sort(key=lambda c: c.created_at)

    # Collect every comment id that will appear in the output (respecting depth).
    included: list[Comment] = []
    seen: set[uuid.UUID] = set()

    def _collect(comment: Comment, remaining: int) -> None:
        if comment.id in seen:
            return
        seen.add(comment.id)
        included.append(comment)
        if remaining > 0:
            for child in children_by_parent.get(comment.id, []):
                _collect(child, remaining - 1)

    for c in top_level:
        _collect(c, depth)

    comment_ids = [c.id for c in included]

    # ── Batch-load all related rows in a handful of queries ──
    annotations_map = _get_annotations_map(comment_ids, db)

    attachments_by_comment: dict[uuid.UUID, list[CommentAttachment]] = defaultdict(list)
    reactions_by_comment: dict[uuid.UUID, list[CommentReaction]] = defaultdict(list)
    if comment_ids:
        for a in db.query(CommentAttachment).filter(
            CommentAttachment.comment_id.in_(comment_ids)
        ).all():
            attachments_by_comment[a.comment_id].append(a)
        for r in db.query(CommentReaction).filter(
            CommentReaction.comment_id.in_(comment_ids)
        ).all():
            reactions_by_comment[r.comment_id].append(r)

    author_ids = {c.author_id for c in included if c.author_id}
    guest_ids = {c.guest_author_id for c in included if c.guest_author_id}
    authors_map = {
        u.id: u for u in db.query(User).filter(User.id.in_(author_ids)).all()
    } if author_ids else {}
    guests_map = {
        g.id: g for g in db.query(GuestUser).filter(GuestUser.id.in_(guest_ids)).all()
    } if guest_ids else {}

    def _build(comment: Comment, remaining: int) -> CommentResponse:
        author_info = None
        if comment.author_id and comment.author_id in authors_map:
            author = authors_map[comment.author_id]
            author_info = AuthorInfo(id=author.id, name=author.name, avatar_url=author.avatar_url)

        guest_author_info = None
        if comment.guest_author_id and comment.guest_author_id in guests_map:
            guest = guests_map[comment.guest_author_id]
            guest_author_info = GuestAuthorInfo(id=guest.id, name=guest.name, email=guest.email)

        annotation = annotations_map.get(comment.id)

        resp = CommentResponse.model_validate(comment)
        resp.author = author_info
        resp.guest_author = guest_author_info
        resp.annotation = AnnotationResponse.model_validate(annotation) if annotation else None
        resp.replies = [
            _build(child, remaining - 1)
            for child in children_by_parent.get(comment.id, [])
        ] if remaining > 0 else []
        resp.attachments = [
            _build_attachment_response(a) for a in attachments_by_comment.get(comment.id, [])
        ]
        resp.reactions = _build_reaction_responses(
            reactions_by_comment.get(comment.id, []), current_user_id
        )
        return resp

    return [_build(c, depth) for c in top_level]


def _parse_mentions(body: str) -> list[str]:
    """Extract @email mentions from comment body."""
    return re.findall(r"@([\w.+-]+@[\w.-]+\.\w+)", body)


def _create_mentions(db: Session, comment: Comment, asset: Asset, body: str, author_name: str, mention_user_ids: list | None = None) -> None:
    """Create Mention + Notification records and send emails.
    Uses explicit mention_user_ids if provided, else falls back to parsing @email from body."""
    from ..services.auth_service import get_user_by_email
    from ..config import settings

    mentioned_users = []

    if mention_user_ids:
        # Use explicit user IDs from frontend
        for uid in set(mention_user_ids):
            user = db.query(User).filter(User.id == uid).first()
            if user and user.id != comment.author_id:
                mentioned_users.append(user)
    else:
        # Fallback: parse @email from body
        emails = _parse_mentions(body)
        for email in set(emails):
            user = get_user_by_email(db, email)
            if user and user.id != comment.author_id:
                mentioned_users.append(user)

    for user in mentioned_users:
        mention = Mention(comment_id=comment.id, mentioned_user_id=user.id)
        db.add(mention)
        notif = Notification(
            user_id=user.id,
            type=NotificationType.mention,
            asset_id=asset.id,
            comment_id=comment.id,
        )
        db.add(notif)

        asset_link = f"{settings.frontend_url}/projects/{asset.project_id}/assets/{asset.id}"
        send_task_safe(send_mention_email,
            to_email=user.email,
            mentioner_name=author_name,
            asset_name=asset.name,
            comment_preview=body[:200],
            asset_link=asset_link,
        )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/assets/{asset_id}/comments", response_model=list[CommentResponse])
def list_comments(
    asset_id: uuid.UUID,
    version_id: Optional[uuid.UUID] = None,
    visibility: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    require_asset_access(db, asset, current_user)
    # Load every non-deleted comment for this asset in ONE query, then split into
    # top-level + replies in memory to avoid the per-comment recursive N+1.
    all_query = db.query(Comment).filter(
        Comment.asset_id == asset_id,
        Comment.deleted_at.is_(None),
    )
    if version_id:
        all_query = all_query.filter(Comment.version_id == version_id)
    all_comments = all_query.order_by(Comment.created_at).all()

    top_level = [c for c in all_comments if c.parent_id is None]
    if visibility and visibility in ("public", "internal"):
        top_level = [c for c in top_level if c.visibility == visibility]

    return _build_comment_tree_batched(
        top_level, all_comments, db, current_user_id=current_user.id
    )


@router.post("/assets/{asset_id}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def create_comment(
    asset_id: uuid.UUID,
    body: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    _require_comment_write_access(db, asset, current_user)

    comment = Comment(
        asset_id=asset_id,
        version_id=body.version_id,
        parent_id=body.parent_id,
        author_id=current_user.id,
        timecode_start=body.timecode_start,
        timecode_end=body.timecode_end,
        body=body.body,
        visibility=body.visibility or "public",
    )
    db.add(comment)
    db.flush()

    if body.annotation:
        annotation = Annotation(
            comment_id=comment.id,
            drawing_data=body.annotation.drawing_data,
            frame_number=body.annotation.frame_number,
            carousel_position=body.annotation.carousel_position,
        )
        db.add(annotation)

    _create_mentions(db, comment, asset, body.body, current_user.name, body.mention_user_ids)

    # Notify asset creator about the comment (unless they're the commenter)
    if asset.created_by and asset.created_by != current_user.id:
        db.add(Notification(
            user_id=asset.created_by,
            type=NotificationType.comment,
            asset_id=asset_id,
            comment_id=comment.id,
        ))

    # Activity log
    activity = ActivityLog(user_id=current_user.id, asset_id=asset_id, action=ActivityAction.commented)
    db.add(activity)

    db.commit()
    db.refresh(comment)
    return _build_comment_response(comment, db, current_user_id=current_user.id)


@router.post("/assets/{asset_id}/comments/{comment_id}/replies", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def reply_to_comment(
    asset_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    _require_comment_write_access(db, asset, current_user)
    parent = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent comment not found")

    # Force body's version_id to match parent
    reply = Comment(
        asset_id=asset_id,
        version_id=parent.version_id,
        parent_id=comment_id,
        author_id=current_user.id,
        body=body.body,
    )
    db.add(reply)
    db.flush()
    _create_mentions(db, reply, asset, body.body, current_user.name, body.mention_user_ids)

    # Notify parent comment author about the reply (unless they're the replier)
    if parent.author_id and parent.author_id != current_user.id:
        db.add(Notification(
            user_id=parent.author_id,
            type=NotificationType.comment,
            asset_id=asset_id,
            comment_id=reply.id,
        ))

    db.commit()
    db.refresh(reply)
    return _build_comment_response(reply, db, current_user_id=current_user.id)


@router.patch("/comments/{comment_id}", response_model=CommentResponse)
def update_comment(
    comment_id: uuid.UUID,
    body: CommentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    # Allow comment owner or project owner to edit
    if comment.author_id != current_user.id:
        asset = _get_asset(db, comment.asset_id)
        member = db.query(ProjectMember).filter(
            ProjectMember.project_id == asset.project_id,
            ProjectMember.user_id == current_user.id,
            ProjectMember.role == ProjectRole.owner,
            ProjectMember.deleted_at.is_(None),
        ).first()
        if not member:
            raise HTTPException(status_code=403, detail="Can only edit your own comments")
    comment.body = body.body
    comment.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(comment)
    return _build_comment_response(comment, db, current_user_id=current_user.id)


@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    # Allow comment owner or project owner to delete
    if comment.author_id != current_user.id:
        asset = _get_asset(db, comment.asset_id)
        member = db.query(ProjectMember).filter(
            ProjectMember.project_id == asset.project_id,
            ProjectMember.user_id == current_user.id,
            ProjectMember.role == ProjectRole.owner,
            ProjectMember.deleted_at.is_(None),
        ).first()
        if not member:
            raise HTTPException(status_code=403, detail="Can only delete your own comments")
    comment.deleted_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/comments/{comment_id}/resolve", response_model=CommentResponse)
def resolve_comment(
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    asset = _get_asset(db, comment.asset_id)
    _require_comment_write_access(db, asset, current_user)
    comment.resolved = not comment.resolved
    db.commit()
    db.refresh(comment)
    return _build_comment_response(comment, db, current_user_id=current_user.id)


# ── Attachments ────────────────────────────────────────────────────────────────

@router.post(
    "/comments/{comment_id}/attachments",
    response_model=AttachmentUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_attachment(
    comment_id: uuid.UUID,
    body: AttachmentUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = _get_comment(db, comment_id)
    asset = _get_asset(db, comment.asset_id)
    require_asset_access(db, asset, current_user)

    # Generate S3 key
    key = f"comment-attachments/{comment_id}/{uuid.uuid4()}/{body.file_name}"

    # Generate presigned PUT URL
    s3 = s3_service.get_s3_client()
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ContentType": body.content_type,
        },
        ExpiresIn=3600,
    )

    # Save attachment record
    attachment = CommentAttachment(
        comment_id=comment_id,
        file_type=body.content_type,
        s3_key=key,
        original_filename=body.file_name,
        file_size_bytes=body.file_size,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    return AttachmentUploadResponse(
        upload_url=upload_url,
        attachment_id=attachment.id,
        key=key,
    )


@router.delete(
    "/comments/{comment_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_attachment(
    comment_id: uuid.UUID,
    attachment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = _get_comment(db, comment_id)
    asset = _get_asset(db, comment.asset_id)

    attachment = db.query(CommentAttachment).filter(
        CommentAttachment.id == attachment_id,
        CommentAttachment.comment_id == comment_id,
    ).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Must be comment author OR project owner/editor
    from ..models.project import ProjectRole
    from ..services.permissions import get_project_member
    is_comment_author = comment.author_id == current_user.id
    if not is_comment_author:
        pm = get_project_member(db, asset.project_id, current_user.id)
        if not pm or pm.role not in (ProjectRole.owner, ProjectRole.editor):
            raise HTTPException(status_code=403, detail="Not authorized to delete this attachment")

    # Delete from S3
    try:
        s3_service.delete_object(attachment.s3_key)
    except Exception:
        pass  # Best-effort S3 deletion

    db.delete(attachment)
    db.commit()


# ── Reactions ──────────────────────────────────────────────────────────────────

@router.post("/comments/{comment_id}/react", status_code=status.HTTP_204_NO_CONTENT)
def toggle_reaction(
    comment_id: uuid.UUID,
    body: ReactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = _get_comment(db, comment_id)
    asset = _get_asset(db, comment.asset_id)
    _require_comment_write_access(db, asset, current_user)

    existing = db.query(CommentReaction).filter(
        CommentReaction.comment_id == comment_id,
        CommentReaction.user_id == current_user.id,
        CommentReaction.emoji == body.emoji,
    ).first()

    if existing:
        db.delete(existing)
    else:
        reaction = CommentReaction(
            comment_id=comment_id,
            user_id=current_user.id,
            emoji=body.emoji,
        )
        db.add(reaction)

    db.commit()


@router.get("/comments/{comment_id}/reactions", response_model=list[ReactionResponse])
def list_reactions(
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    comment = _get_comment(db, comment_id)
    asset = _get_asset(db, comment.asset_id)
    require_asset_access(db, asset, current_user)

    reactions_raw = db.query(CommentReaction).filter(
        CommentReaction.comment_id == comment_id,
    ).all()
    return _build_reaction_responses(reactions_raw, current_user.id)


# ── Deep link ──────────────────────────────────────────────────────────────────

@router.get("/assets/{asset_id}/comments/{comment_id}/link")
def comment_deep_link(
    asset_id: uuid.UUID,
    comment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = _get_asset(db, asset_id)
    require_asset_access(db, asset, current_user)
    # Verify comment belongs to this asset
    comment = db.query(Comment).filter(
        Comment.id == comment_id,
        Comment.asset_id == asset_id,
        Comment.deleted_at.is_(None),
    ).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    url = f"{settings.frontend_url}/assets/{asset_id}?comment={comment_id}"
    return {"url": url}


# ── Guest comments (via share link) ───────────────────────────────────────────

@router.get("/share/{token}/comments")
def list_share_comments(
    token: str,
    asset_id: Optional[uuid.UUID] = None,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Public endpoint — list comments for a shared asset. Optional auth.
    Enforces secure + password-protected links via the session-aware validator.
    For folder/project shares, pass asset_id as query param to get comments for a specific asset."""
    link = validate_share_link_with_session(
        db, token, share_session=share_session, current_user=current_user
    )

    # Determine the asset_id to list comments for
    target_asset_id = link.asset_id or asset_id
    if not target_asset_id:
        return {"comments": []}
    # Confirm the asset is actually within this share's scope before exposing its
    # comments — a caller-supplied asset_id must not escape the shared folder/project.
    validate_asset_in_share(db, link, target_asset_id)
    asset_id = target_asset_id

    # External reviewers must never see internal/team-only comments. Load only
    # public-visibility comments (top-level + replies) and build the tree.
    all_comments = db.query(Comment).filter(
        Comment.asset_id == asset_id,
        Comment.deleted_at.is_(None),
        Comment.visibility == "public",
    ).order_by(Comment.created_at).all()

    top_level = [c for c in all_comments if c.parent_id is None]
    return _build_comment_tree_batched(top_level, all_comments, db)


@router.post("/share/{token}/comment", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def guest_comment(
    token: str,
    body: GuestCommentCreate,
    share_session: Optional[str] = Query(None, alias="share_session"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    link = validate_share_link_with_session(
        db, token, share_session=share_session, current_user=current_user
    )

    # Check share link permission allows commenting
    if link.permission == SharePermission.view:
        raise HTTPException(status_code=403, detail="This share link does not allow commenting")

    # Resolve asset_id: from body, link, or error
    target_asset_id = body.asset_id or link.asset_id
    if not target_asset_id:
        raise HTTPException(status_code=400, detail="asset_id is required for folder/project shares")
    # A caller-supplied asset_id must belong to this share's scope (folder/project),
    # otherwise a valid token could be used to comment on arbitrary assets.
    asset = validate_asset_in_share(db, link, target_asset_id)

    # Resolve version_id: use provided or get latest ready version
    version_id = body.version_id
    if not version_id:
        from ..models.asset import AssetVersion, ProcessingStatus
        latest = db.query(AssetVersion).filter(
            AssetVersion.asset_id == asset.id,
            AssetVersion.deleted_at.is_(None),
            AssetVersion.processing_status == ProcessingStatus.ready,
        ).order_by(AssetVersion.version_number.desc()).first()
        if latest:
            version_id = latest.id
        else:
            raise HTTPException(status_code=400, detail="No ready version found for this asset")

    # Determine author: logged-in user or guest
    author_id = None
    guest_author_id = None
    if current_user:
        author_id = current_user.id
    else:
        if not body.guest_email or not body.guest_name:
            raise HTTPException(status_code=400, detail="guest_email and guest_name required for anonymous comments")
        guest_email = body.guest_email.lower()
        guest = db.query(GuestUser).filter(GuestUser.email == guest_email).first()
        if not guest:
            guest = GuestUser(email=guest_email, name=body.guest_name)
            db.add(guest)
            db.flush()
        guest_author_id = guest.id

    comment = Comment(
        asset_id=asset.id,
        version_id=version_id,
        parent_id=body.parent_id,
        author_id=author_id,
        guest_author_id=guest_author_id,
        timecode_start=body.timecode_start,
        timecode_end=body.timecode_end,
        body=body.body,
    )
    db.add(comment)
    db.flush()

    # Parse mentions (guest can mention registered users)
    emails = _parse_mentions(body.body)
    for email in set(emails):
        from ..services.auth_service import get_user_by_email
        user = get_user_by_email(db, email)
        if user:
            mention = Mention(comment_id=comment.id, mentioned_user_id=user.id)
            db.add(mention)
            notif = Notification(
                user_id=user.id,
                type=NotificationType.mention,
                asset_id=asset.id,
                comment_id=comment.id,
            )
            db.add(notif)

    if body.annotation:
        annotation = Annotation(
            comment_id=comment.id,
            drawing_data=body.annotation.drawing_data,
            frame_number=body.annotation.frame_number,
            carousel_position=body.annotation.carousel_position,
        )
        db.add(annotation)

    db.commit()
    db.refresh(comment)

    # Log share link activity
    actor_email = current_user.email if current_user else (body.guest_email or "anonymous")
    actor_name = current_user.name if current_user else body.guest_name
    activity = ShareLinkActivity(
        share_link_id=link.id,
        action=ShareActivityAction.commented,
        actor_email=actor_email,
        actor_name=actor_name,
        asset_id=asset.id,
        asset_name=asset.name,
    )
    db.add(activity)
    db.commit()

    return _build_comment_response(comment, db)
