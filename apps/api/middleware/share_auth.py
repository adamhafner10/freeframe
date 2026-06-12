from typing import Optional
from fastapi import Header, HTTPException, Depends, status
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.share import ShareLink
from ..models.user import User
from ..middleware.auth import get_optional_user
from ..services.permissions import validate_share_link_with_session


def get_share_link(
    x_share_token: Optional[str] = Header(default=None),
    x_share_session: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
) -> Optional[ShareLink]:
    """Optional share link dependency — returns None if no token provided.

    Enforces the same visibility/password rules as the query-param share flow:
    `secure` links require an authenticated user, and password-protected links
    require a valid session (via the X-Share-Session header) or the link
    creator. Fails closed (401/403) when those conditions are not met."""
    if not x_share_token:
        return None
    return validate_share_link_with_session(
        db, x_share_token, share_session=x_share_session, current_user=current_user
    )


def require_share_link(
    x_share_token: Optional[str] = Header(default=None),
    x_share_session: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
) -> ShareLink:
    """Required share link dependency — raises 401 if no token provided.

    Enforces secure/password rules via the session-aware validator (see
    `get_share_link`); password-protected links without a satisfiable session
    or creator context fail closed."""
    if not x_share_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share token required")
    return validate_share_link_with_session(
        db, x_share_token, share_session=x_share_session, current_user=current_user
    )
