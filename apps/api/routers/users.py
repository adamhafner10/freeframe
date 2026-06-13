from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from ..database import get_db
from ..schemas.auth import UserResponse, InviteRequest, UpdateProfileRequest
from ..models.user import User, UserStatus
from ..middleware.auth import get_current_user
from ..services.auth_service import hash_password, get_user_by_email
from ..services.email_service import email_service
from ..config import settings

# Invite tokens are valid for this many days. Kept as a constant so the DB
# expiry and the value rendered into the email stay in lock-step.
INVITE_EXPIRY_DAYS = 7

router = APIRouter(prefix="/users", tags=["users"])

@router.get("", response_model=list[UserResponse])
def get_users_batch(
    ids: str = Query(..., description="Comma-separated user IDs"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get basic user info for a batch of user IDs. Any authenticated user can call this."""
    try:
        user_ids = [uuid.UUID(uid.strip()) for uid in ids.split(",") if uid.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")
    if len(user_ids) > 100:
        raise HTTPException(status_code=400, detail="Max 100 user IDs per request")
    users = db.query(User).filter(User.id.in_(user_ids), User.deleted_at.is_(None)).all()
    return users


@router.get("/search", response_model=list[UserResponse])
def search_users(
    q: str = Query(..., min_length=1, description="Search by name or email"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search users by name or email. Returns up to 10 matching users."""
    pattern = f"%{q}%"
    users = db.query(User).filter(
        User.deleted_at.is_(None),
        (User.name.ilike(pattern) | User.email.ilike(pattern)),
    ).limit(10).all()
    return users


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

@router.post("/invite", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def invite_user(body: InviteRequest, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    if get_user_by_email(db, body.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Generate invite token
    invite_token = secrets.token_urlsafe(48)
    invite_expires = datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS)

    user = User(
        email=body.email,
        name=body.name,
        status=UserStatus.pending_invite,
        invite_token=invite_token,
        invite_token_expires_at=invite_expires,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Send the invite email SYNCHRONOUSLY. The invite token lives ONLY in this
    # email (no resend-on-dispatch, no token in the 201 body), so a swallowed
    # broker/publish error would silently lock the invitee out forever while the
    # API reported success. If the send fails we roll back the just-created
    # pending user and return 503 so a retry is clean (no "already registered"
    # ghost user blocking a re-invite).
    invite_url = f"{settings.frontend_url}/invite/{invite_token}"
    try:
        email_service.send_invite_email_sync(
            user.email,
            current_user.name or "Admin",
            "FileStream",
            invite_url,
            expiry_days=INVITE_EXPIRY_DAYS,
        )
    except Exception:
        db.delete(user)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to send the invite email right now. Please try again in a moment.",
        )

    return user


@router.post("/{user_id}/resend-invite", response_model=UserResponse)
def resend_invite(user_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """Regenerate the invite token for a still-pending user and re-send it.

    The invite token only ever exists in the delivered email; if that first send
    is lost there is otherwise no way to recover (re-inviting hits "already
    registered"). This endpoint mints a fresh token + expiry and re-sends
    synchronously so a failed delivery surfaces as a 503 the admin can retry.
    """
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status != UserStatus.pending_invite:
        raise HTTPException(status_code=400, detail="User is not pending an invite")

    # Mint a fresh token so the previous (possibly leaked or undelivered) one is
    # invalidated, and persist before sending so the link in the email is valid.
    new_token = secrets.token_urlsafe(48)
    user.invite_token = new_token
    user.invite_token_expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS)
    db.commit()
    db.refresh(user)

    invite_url = f"{settings.frontend_url}/invite/{new_token}"
    try:
        email_service.send_invite_email_sync(
            user.email,
            current_user.name or "Admin",
            "FileStream",
            invite_url,
            expiry_days=INVITE_EXPIRY_DAYS,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to send the invite email right now. Please try again in a moment.",
        )

    return user

@router.patch("/{user_id}", response_model=UserResponse)
def update_user(user_id: uuid.UUID, body: UpdateProfileRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Update user profile. Users can update their own profile."""
    if current_user.id != user_id and not current_user.is_superadmin:
        raise HTTPException(status_code=403, detail="Can only update your own profile")
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.name is not None:
        user.name = body.name.strip()
    if body.avatar_url is not None:
        user.avatar_url = body.avatar_url
    db.commit()
    db.refresh(user)
    return user

@router.patch("/{user_id}/deactivate", response_model=UserResponse)
def deactivate_user(user_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.status = UserStatus.deactivated
    db.commit()
    db.refresh(user)
    return user

@router.patch("/{user_id}/reactivate", response_model=UserResponse)
def reactivate_user(user_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.status = UserStatus.active
    db.commit()
    db.refresh(user)
    return user

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: uuid.UUID, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.deleted_at = datetime.now(timezone.utc)
    db.commit()
