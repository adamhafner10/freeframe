from pydantic import BaseModel, EmailStr, Field
from typing import Annotated
import uuid
from ..models.user import UserStatus


# bcrypt silently truncates at 72 bytes, so we cap a bit under that to keep
# the "password" the user types equal to the one we verify against later.
Password = Annotated[str, Field(min_length=8, max_length=64)]


class RegisterRequest(BaseModel):
    email: EmailStr
    name: str
    password: Password

class LoginRequest(BaseModel):
    email: EmailStr
    password: str  # no min_length here — we validate against stored hash, not create

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    needs_password: bool = False  # True if user needs to set password

class RefreshRequest(BaseModel):
    refresh_token: str

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    avatar_url: str | None
    status: UserStatus
    email_verified: bool = False
    is_superadmin: bool = False
    invite_token: str | None = None
    preferences: dict = {}

    model_config = {"from_attributes": True}

class InviteRequest(BaseModel):
    email: EmailStr
    name: str

# Magic code flow
class SendMagicCodeRequest(BaseModel):
    email: EmailStr

class SendMagicCodeResponse(BaseModel):
    message: str
    email: str

class VerifyMagicCodeRequest(BaseModel):
    email: EmailStr
    code: str

class SetPasswordRequest(BaseModel):
    password: Password

# Invite flow
class AcceptInviteRequest(BaseModel):
    token: str
    password: Password

class InviteInfoResponse(BaseModel):
    email: str
    name: str
    org_name: str | None = None

class UpdateProfileRequest(BaseModel):
    name: str | None = None
    avatar_url: str | None = None

class UpdateUserRoleRequest(BaseModel):
    is_admin: bool

class DeactivateUserRequest(BaseModel):
    user_id: uuid.UUID

