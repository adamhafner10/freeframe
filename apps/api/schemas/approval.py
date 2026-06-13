from pydantic import BaseModel
import uuid
from datetime import datetime
from typing import Optional
from ..models.approval import ApprovalStatus

class ApprovalCreate(BaseModel):
    version_id: uuid.UUID
    note: Optional[str] = None

class GuestApprovalCreate(BaseModel):
    """Approve/reject request from a share link. asset_id is required for
    folder/project shares (single-asset shares resolve it from the link).
    guest_email/guest_name are only consulted when the caller isn't an
    authenticated member."""
    asset_id: Optional[uuid.UUID] = None  # Required for folder/project shares
    version_id: Optional[uuid.UUID] = None  # Auto-resolved if not provided
    note: Optional[str] = None
    guest_email: Optional[str] = None  # Not needed if user is logged in
    guest_name: Optional[str] = None

class ApprovalResponse(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    version_id: uuid.UUID
    user_id: uuid.UUID
    status: ApprovalStatus
    note: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}
