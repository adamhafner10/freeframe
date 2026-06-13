import redis
import secrets
import time
import threading
from typing import Optional
from ..config import settings

# Redis client
_redis_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Get Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


# Magic code keys
MAGIC_CODE_PREFIX = "magic_code:"
MAGIC_CODE_ATTEMPTS_PREFIX = "magic_code_attempts:"
MAGIC_CODE_EXPIRY_SECONDS = 600  # 10 minutes
MAX_MAGIC_CODE_ATTEMPTS = 5


def generate_magic_code() -> str:
    """Generate a 6-digit magic code."""
    return str(secrets.randbelow(900000) + 100000)


def store_magic_code(email: str, code: str) -> None:
    """Store magic code in Redis with expiry."""
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    r.setex(key, MAGIC_CODE_EXPIRY_SECONDS, code)
    # Reset attempts counter
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    r.delete(attempts_key)


def verify_magic_code(email: str, code: str) -> tuple[bool, str]:
    """
    Verify magic code from Redis.
    Returns (success, error_message).
    """
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    
    # Check attempts
    attempts = r.get(attempts_key)
    if attempts and int(attempts) >= MAX_MAGIC_CODE_ATTEMPTS:
        return False, "Too many attempts. Request a new code."
    
    # Get stored code
    stored_code = r.get(key)
    if not stored_code:
        return False, "Code expired or not found"
    
    if stored_code != code:
        # Increment attempts
        r.incr(attempts_key)
        r.expire(attempts_key, MAGIC_CODE_EXPIRY_SECONDS)
        return False, "Invalid code"
    
    # Success - delete the code
    r.delete(key)
    r.delete(attempts_key)
    return True, ""


def delete_magic_code(email: str) -> None:
    """Delete magic code from Redis."""
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    r.delete(key)
    r.delete(attempts_key)


# Invite token keys (also in Redis for faster lookup)
INVITE_TOKEN_PREFIX = "invite_token:"
INVITE_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


def store_invite_token(token: str, user_id: str) -> None:
    """Store invite token -> user_id mapping in Redis."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    r.setex(key, INVITE_TOKEN_EXPIRY_SECONDS, user_id)


def get_user_id_from_invite_token(token: str) -> Optional[str]:
    """Get user_id from invite token."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    return r.get(key)


def delete_invite_token(token: str) -> None:
    """Delete invite token from Redis."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    r.delete(key)


# ── IP-based rate limiting ────────────────────────────────────────────────────

RATE_LIMIT_PREFIX = "rl:"


# In-process fallback limiter — used ONLY when Redis is unavailable so that
# security-sensitive limits (auth, share-password) keep throttling instead of
# silently disabling protection. It is per-process (not shared across workers)
# and therefore intentionally conservative: a security limit that loses Redis
# degrades to a tighter local cap rather than failing open.
_local_counters: dict = {}
_local_lock = threading.Lock()


def _local_rate_limit(
    key: str,
    max_requests: int,
    window_seconds: int,
) -> tuple[bool, int]:
    """Conservative per-process sliding-window fallback. Returns (allowed, retry_after)."""
    now = time.monotonic()
    with _local_lock:
        bucket = _local_counters.get(key)
        if bucket is None or now >= bucket["reset"]:
            bucket = {"count": 0, "reset": now + window_seconds}
            _local_counters[key] = bucket
        if bucket["count"] >= max_requests:
            return False, max(int(bucket["reset"] - now), 1)
        bucket["count"] += 1
        return True, 0


def check_rate_limit(
    ip: str,
    action: str,
    max_requests: int,
    window_seconds: int,
    *,
    fail_closed: bool = False,
) -> tuple[bool, int]:
    """
    Check if an IP has exceeded the rate limit for a given action.
    Returns (allowed, remaining_seconds_until_reset).
    Uses a simple counter with TTL in Redis.

    On Redis error:
      * fail_closed=False (default, non-security limits): degrade to a
        conservative in-process limiter so requests are still throttled rather
        than silently unbounded.
      * fail_closed=True (auth / share-password contexts): never silently
        disable protection — fall back to the in-process limiter and, if even
        that cannot be evaluated, deny the request.
    """
    try:
        r = get_redis()
        key = f"{RATE_LIMIT_PREFIX}{action}:{ip}"
        current = r.get(key)

        if current is not None and int(current) >= max_requests:
            ttl = r.ttl(key)
            return False, max(ttl, 1)

        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.execute()
        return True, 0
    except Exception:
        # Redis is unavailable. NEVER silently disable throttling: degrade to a
        # conservative in-process limiter. For security limits, deny outright if
        # the local limiter itself cannot be evaluated.
        try:
            return _local_rate_limit(
                f"{RATE_LIMIT_PREFIX}{action}:{ip}", max_requests, window_seconds
            )
        except Exception:
            if fail_closed:
                return False, window_seconds
            return True, 0


# ── Per-share-link password brute-force lockout ───────────────────────────────
#
# bcrypt cost alone does not stop online brute-force of a share password once
# per-IP throttling is bypassed. We cap failed attempts per share link in Redis
# and lock the link out for a cool-down once the cap is hit. Keyed by share link
# id (the durable identifier), independent of caller IP.

SHARE_PW_ATTEMPTS_PREFIX = "share_pw_attempts:"
MAX_SHARE_PW_ATTEMPTS = 10
SHARE_PW_LOCKOUT_SECONDS = 900  # 15 minutes


def _share_pw_attempts_key(link_id: str) -> str:
    return f"{SHARE_PW_ATTEMPTS_PREFIX}{link_id}"


def check_share_password_lockout(link_id: str) -> tuple[bool, int]:
    """Return (locked_out, retry_after_seconds) for a share link's password.

    Fails CLOSED on Redis error: if we cannot read the attempt counter for a
    password-protected link, we must not silently allow unlimited guessing.
    """
    try:
        r = get_redis()
        key = _share_pw_attempts_key(link_id)
        attempts = r.get(key)
        if attempts is not None and int(attempts) >= MAX_SHARE_PW_ATTEMPTS:
            ttl = r.ttl(key)
            return True, max(ttl, 1)
        return False, 0
    except Exception:
        # Cannot verify attempt budget — treat as locked to avoid open brute-force.
        return True, SHARE_PW_LOCKOUT_SECONDS


def register_share_password_failure(link_id: str) -> tuple[bool, int]:
    """Record a failed share-password attempt; start a lockout once the cap is hit.

    Returns (now_locked_out, retry_after_seconds). Best-effort on Redis error
    (the read-side check above is the fail-closed gate)."""
    try:
        r = get_redis()
        key = _share_pw_attempts_key(link_id)
        count = r.incr(key)
        # (Re)arm the cool-down window on every failure so sustained guessing
        # keeps the link locked.
        r.expire(key, SHARE_PW_LOCKOUT_SECONDS)
        if count >= MAX_SHARE_PW_ATTEMPTS:
            return True, SHARE_PW_LOCKOUT_SECONDS
        return False, 0
    except Exception:
        return False, 0


def reset_share_password_attempts(link_id: str) -> None:
    """Clear the failed-attempt counter after a successful password verification."""
    try:
        r = get_redis()
        r.delete(_share_pw_attempts_key(link_id))
    except Exception:
        pass


# ── Share link password sessions ──────────────────────────────────────────────

SHARE_SESSION_PREFIX = "share_session:"
SHARE_SESSION_EXPIRY_SECONDS = 3600  # 1 hour


def create_share_session(token: str, session_id: str) -> None:
    """Store a session after successful password verification."""
    r = get_redis()
    key = f"{SHARE_SESSION_PREFIX}{token}:{session_id}"
    r.setex(key, SHARE_SESSION_EXPIRY_SECONDS, "1")


def verify_share_session(token: str, session_id: str) -> bool:
    """Check if a valid password session exists for this share link."""
    r = get_redis()
    key = f"{SHARE_SESSION_PREFIX}{token}:{session_id}"
    return r.exists(key) > 0
