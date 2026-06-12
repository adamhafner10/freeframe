"""Admin endpoints for user management."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session
from pydantic import BaseModel
import uuid

from ..database import get_db
from ..middleware.auth import get_current_user
from ..models.user import User, UserStatus
from ..models.project import Project
from ..models.asset import Asset, AssetVersion, MediaFile
from ..schemas.auth import UserResponse, UpdateUserRoleRequest

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Storage / usage dashboard schemas ───────────────────────────────────────────

class ProjectStorageResponse(BaseModel):
    project_id: uuid.UUID
    name: str
    bytes: int
    bytes_human: str
    asset_count: int
    version_count: int


class StorageSummaryResponse(BaseModel):
    total_bytes: int
    total_human: str
    project_count: int
    projects: list[ProjectStorageResponse]


def _human_bytes(num: int) -> str:
    """Render a byte count as a human-readable string (binary units)."""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PB"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserResponse])
def list_all_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all users in the system. Only accessible by admins."""
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can access this endpoint"
        )

    users = db.query(User).filter(User.deleted_at.is_(None)).all()
    return users

@router.patch("/users/{user_id}/deactivate", response_model=UserResponse)
def deactivate_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a user. Admins cannot deactivate themselves."""
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can deactivate users"
        )

    # Prevent admin from deactivating themselves
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate yourself"
        )

    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.status = UserStatus.deactivated
    db.commit()
    db.refresh(user)
    return user

@router.patch("/users/{user_id}/reactivate", response_model=UserResponse)
def reactivate_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reactivate a deactivated user. Only accessible by admins."""
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can reactivate users"
        )

    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.status = UserStatus.active
    db.commit()
    db.refresh(user)
    return user

@router.patch("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: uuid.UUID,
    body: UpdateUserRoleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promote or demote a user to/from admin role. Only accessible by admins."""
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can change user roles"
        )

    # Prevent admin from removing their own admin role
    if user_id == current_user.id and not body.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove your own admin role"
        )

    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_superadmin = body.is_admin
    db.commit()
    db.refresh(user)
    return user


@router.get("/storage", response_model=StorageSummaryResponse)
def get_storage_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-project B2 storage breakdown (billed per GB). Superadmin only.

    Aggregates media_files.file_size_bytes grouped by project in a SINGLE
    query (media_files -> asset_versions -> assets -> projects). Soft-deleted
    versions, assets, and projects are excluded consistently; media_files has
    no soft-delete column, so it inherits exclusion from its parent version.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can access this endpoint"
        )

    rows = (
        db.query(
            Project.id.label("project_id"),
            Project.name.label("name"),
            func.coalesce(func.sum(MediaFile.file_size_bytes), 0).label("bytes"),
            func.count(distinct(Asset.id)).label("asset_count"),
            func.count(distinct(AssetVersion.id)).label("version_count"),
        )
        .join(Asset, Asset.project_id == Project.id)
        .join(AssetVersion, AssetVersion.asset_id == Asset.id)
        .join(MediaFile, MediaFile.version_id == AssetVersion.id)
        .filter(
            Project.deleted_at.is_(None),
            Asset.deleted_at.is_(None),
            AssetVersion.deleted_at.is_(None),
        )
        .group_by(Project.id, Project.name)
        .order_by(func.coalesce(func.sum(MediaFile.file_size_bytes), 0).desc())
        .all()
    )

    projects = [
        ProjectStorageResponse(
            project_id=r.project_id,
            name=r.name,
            bytes=int(r.bytes or 0),
            bytes_human=_human_bytes(int(r.bytes or 0)),
            asset_count=int(r.asset_count or 0),
            version_count=int(r.version_count or 0),
        )
        for r in rows
    ]

    total_bytes = sum(p.bytes for p in projects)
    return StorageSummaryResponse(
        total_bytes=total_bytes,
        total_human=_human_bytes(total_bytes),
        project_count=len(projects),
        projects=projects,
    )
