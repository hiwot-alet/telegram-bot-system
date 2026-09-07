"""
database.py — PostgreSQL adapter for the Telegram OTP & Verification bot.

Uses psycopg2 with a threaded connection pool. Provides:
  - phone number normalization to E.164
  - promoter upsert (by telegram_user_id)
  - OTP generation, HMAC hashing, and validation (with expiry/attempt/rate limits)
  - verification_events logging (drives the dashboard's real-time funnel)

Required environment variables:
  DATABASE_URL      postgresql://user:password@host:port/dbname
  OTP_HASH_SECRET   long random secret used to HMAC-hash OTP codes at rest

Optional environment variables:
  DEFAULT_COUNTRY_CODE   country calling code used to expand local numbers
                         that start with a leading 0 (default: "251", Ethiopia)
  DB_POOL_MIN            minimum pooled connections (default: 1)
  DB_POOL_MAX            maximum pooled connections (default: 10)
  OTP_LENGTH              number of digits in a generated OTP (default: 6)
  OTP_TTL_MINUTES         OTP validity window in minutes (default: 5)
  OTP_MAX_ATTEMPTS        max wrong-code attempts before status=failed (default: 5)
  OTP_RESEND_COOLDOWN_SECONDS   min seconds between resends (default: 60)
  OTP_MAX_SENDS_PER_HOUR        max OTP sends per promoter per rolling hour (default: 3)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
OTP_HASH_SECRET = os.environ.get("OTP_HASH_SECRET")

DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "251")

DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))

OTP_LENGTH = int(os.environ.get("OTP_LENGTH", "6"))
OTP_TTL_MINUTES = int(os.environ.get("OTP_TTL_MINUTES", "5"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.environ.get("OTP_RESEND_COOLDOWN_SECONDS", "60"))
OTP_MAX_SENDS_PER_HOUR = int(os.environ.get("OTP_MAX_SENDS_PER_HOUR", "3"))


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class RateLimitedError(RuntimeError):
    """Raised when an OTP resend is attempted before the rate limit allows it."""


# ----------------------------------------------------------------------------
# Connection pool
# ----------------------------------------------------------------------------

_pool: Optional[pg_pool.ThreadedConnectionPool] = None


def init_pool() -> pg_pool.ThreadedConnectionPool:
    """Initialize the module-level connection pool. Safe to call once at startup."""
    global _pool
    if _pool is not None:
        return _pool

    if not DATABASE_URL:
        raise ConfigError("DATABASE_URL environment variable is not set")
    if not OTP_HASH_SECRET:
        raise ConfigError("OTP_HASH_SECRET environment variable is not set")

    _pool = pg_pool.ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, dsn=DATABASE_URL)
    logger.info("Database connection pool initialized (min=%s, max=%s)", DB_POOL_MIN, DB_POOL_MAX)
    return _pool


MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def run_migrations() -> None:
    """Apply any .sql files under migrations/ not yet recorded in schema_migrations.

    Safe to call on every startup: already-applied files are skipped. Needed
    because this project has no separate migration-runner step in its deploy
    pipeline, so the app must bootstrap its own schema on first boot.
    """
    pool = init_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

            for filename in sorted(os.listdir(MIGRATIONS_DIR)):
                if not filename.endswith(".sql") or filename in applied:
                    continue
                path = os.path.join(MIGRATIONS_DIR, filename)
                with open(path, "r", encoding="utf-8") as f:
                    sql = f.read()
                logger.info("Applying migration %s", filename)
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("Database connection pool closed")


@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    """Borrow a connection from the pool, committing on success and rolling back on error."""
    pool = init_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True) -> Iterator[psycopg2.extensions.cursor]:
    """Borrow a connection and yield a cursor, handling commit/rollback/cleanup."""
    with get_connection() as conn:
        cursor_factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
        finally:
            cur.close()


# ----------------------------------------------------------------------------
# Phone number normalization
# ----------------------------------------------------------------------------

_NON_DIGIT_SEPARATORS = re.compile(r"[\s\-\(\)\.]")


def normalize_phone_number(raw: str, default_country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """
    Normalize a phone number to E.164 format (e.g. "+251912345678").

    Handles:
      - already-E.164 numbers ("+251912345678")
      - international prefix with 00 ("00251912345678")
      - local numbers with a leading trunk 0 ("0912345678" -> +251912345678)
      - numbers already missing a leading 0 or + (assumed to already include
        the country code)

    This is a lightweight, dependency-free normalizer suitable for Phase 1.
    For stricter validation (e.g. rejecting numbers with a plausible-but-wrong
    length or invalid area codes per country), consider swapping in the
    `phonenumbers` library later without changing this function's signature.

    Raises ValueError if the input cannot be turned into a plausible E.164 number.
    """
    if not raw or not raw.strip():
        raise ValueError("Phone number is empty")

    cleaned = _NON_DIGIT_SEPARATORS.sub("", raw.strip())

    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
    elif cleaned.startswith("0"):
        digits = f"{default_country_code}{cleaned[1:]}"
    else:
        digits = cleaned

    if not digits.isdigit():
        raise ValueError(f"Phone number contains non-numeric characters: {raw!r}")

    if not (8 <= len(digits) <= 15):
        raise ValueError(f"Normalized phone number has an implausible length: {raw!r}")

    return f"+{digits}"


# ----------------------------------------------------------------------------
# OTP generation, hashing, validation
# ----------------------------------------------------------------------------

def generate_otp_code(length: int = OTP_LENGTH) -> str:
    """Generate a cryptographically random numeric OTP code, e.g. '048213'."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_otp_code(code: str, phone_number: str) -> str:
    """
    HMAC-SHA256 the OTP code, bound to the phone number, keyed by OTP_HASH_SECRET.
    Raw codes are never stored — only this hash goes into otp_verifications.otp_code_hash.
    """
    if not OTP_HASH_SECRET:
        raise ConfigError("OTP_HASH_SECRET environment variable is not set")
    message = f"{phone_number}:{code}".encode("utf-8")
    return hmac.new(OTP_HASH_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _verify_otp_hash(code: str, phone_number: str, stored_hash: str) -> bool:
    candidate = hash_otp_code(code, phone_number)
    return hmac.compare_digest(candidate, stored_hash)


# ----------------------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------------------

def get_campaign_by_slug(slug: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, name, slug, status, created_at FROM campaigns WHERE slug = %s",
            (slug,),
        )
        return cur.fetchone()


# ----------------------------------------------------------------------------
# Promoters
# ----------------------------------------------------------------------------

def upsert_promoter(
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
    full_name: Optional[str] = None,
    campaign_id: Optional[int] = None,
) -> dict:
    """
    Insert a promoter row for this Telegram user, or update the mutable fields
    if one already exists (keyed on the unique telegram_user_id). Does not
    overwrite campaign_id or status on an existing promoter unless a new
    campaign_id is explicitly provided — re-running /start shouldn't silently
    reassign an already-verified promoter to a different campaign.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO promoters (telegram_user_id, telegram_username, full_name, campaign_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_user_id) DO UPDATE
                SET telegram_username = EXCLUDED.telegram_username,
                    full_name = EXCLUDED.full_name,
                    campaign_id = COALESCE(promoters.campaign_id, EXCLUDED.campaign_id)
            RETURNING id, telegram_user_id, telegram_username, full_name,
                      phone_number, campaign_id, status, created_at, updated_at, verified_at
            """,
            (telegram_user_id, telegram_username, full_name, campaign_id),
        )
        return cur.fetchone()


def normalize_telegram_username(raw: str) -> str:
    """Lowercase and strip a leading '@' so lookups are consistent everywhere."""
    return raw.strip().lstrip("@").lower()


# ----------------------------------------------------------------------------
# Promoter roster (allowlist) — see 003_promoter_roster.sql.
#
# Only Telegram usernames an admin has explicitly added here are allowed to
# complete /start. This is the enforcement point INDOMIE asked for: adding a
# promoter = one admin command, removing one = one admin command, no
# engineering involvement for day-to-day roster changes.
# ----------------------------------------------------------------------------

def add_allowed_promoter(
    telegram_username: str,
    city: Optional[str] = None,
    campaign_id: Optional[int] = None,
    added_by_admin_id: Optional[int] = None,
) -> dict:
    """
    Register (or re-activate) a Telegram username on the promoter allowlist.
    Re-running this for a previously revoked username flips it back to
    'active' and updates city/campaign rather than erroring.
    """
    username = normalize_telegram_username(telegram_username)
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO allowed_promoters (telegram_username, city, campaign_id, added_by_admin_id, status)
            VALUES (%s, %s, %s, %s, 'active')
            ON CONFLICT (telegram_username) DO UPDATE
                SET status = 'active',
                    city = COALESCE(EXCLUDED.city, allowed_promoters.city),
                    campaign_id = COALESCE(EXCLUDED.campaign_id, allowed_promoters.campaign_id),
                    added_by_admin_id = EXCLUDED.added_by_admin_id,
                    revoked_at = NULL
            RETURNING id, telegram_username, city, campaign_id, status, created_at
            """,
            (username, city, campaign_id, added_by_admin_id),
        )
        return cur.fetchone()


def remove_allowed_promoter(telegram_username: str) -> Optional[dict]:
    """
    Revoke a promoter's access. Also blocks their existing promoters row (if
    any) so a currently-mid-verification session can't complete. Past
    verification history is untouched — this only stops future bot use.
    """
    username = normalize_telegram_username(telegram_username)
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE allowed_promoters
            SET status = 'revoked', revoked_at = now()
            WHERE telegram_username = %s
            RETURNING id, telegram_username, status
            """,
            (username,),
        )
        revoked = cur.fetchone()
        if revoked is not None:
            cur.execute(
                """
                UPDATE promoters SET status = 'blocked'
                WHERE telegram_username IS NOT NULL
                  AND lower(telegram_username) = %s
                  AND status != 'blocked'
                """,
                (username,),
            )
        return revoked


def is_promoter_allowed(telegram_username: Optional[str]) -> Optional[dict]:
    """
    Returns the active allowed_promoters row for this username, or None if
    the username is missing, was never registered, or was revoked.
    """
    if not telegram_username:
        return None
    username = normalize_telegram_username(telegram_username)
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM allowed_promoters WHERE telegram_username = %s AND status = 'active'",
            (username,),
        )
        return cur.fetchone()


def list_allowed_promoters(status: Optional[str] = None) -> list[dict]:
    with get_cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM allowed_promoters WHERE status = %s ORDER BY city, telegram_username",
                (status,),
            )
        else:
            cur.execute("SELECT * FROM allowed_promoters ORDER BY status, city, telegram_username")
        return cur.fetchall()


def set_promoter_city(promoter_id: int, city: Optional[str]) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE promoters SET city = %s WHERE id = %s", (city, promoter_id))


def get_promoter_by_telegram_id(telegram_user_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM promoters WHERE telegram_user_id = %s",
            (telegram_user_id,),
        )
        return cur.fetchone()


def set_promoter_phone(promoter_id: int, phone_number: str) -> dict:
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE promoters SET phone_number = %s
            WHERE id = %s
            RETURNING id, telegram_user_id, phone_number, campaign_id, status
            """,
            (phone_number, promoter_id),
        )
        return cur.fetchone()


def set_promoter_status(promoter_id: int, status: str, verified: bool = False) -> dict:
    valid_statuses = {"pending", "otp_sent", "verified", "failed", "blocked"}
    if status not in valid_statuses:
        raise ValueError(f"Invalid promoter status: {status!r}")

    with get_cursor() as cur:
        if verified:
            cur.execute(
                """
                UPDATE promoters SET status = %s, verified_at = now()
                WHERE id = %s
                RETURNING id, telegram_user_id, status, verified_at
                """,
                (status, promoter_id),
            )
        else:
            cur.execute(
                """
                UPDATE promoters SET status = %s
                WHERE id = %s
                RETURNING id, telegram_user_id, status, verified_at
                """,
                (status, promoter_id),
            )
        return cur.fetchone()


# ----------------------------------------------------------------------------
# OTP verification lifecycle
# ----------------------------------------------------------------------------

@dataclass
class OtpIssueResult:
    otp_verification_id: int
    code: str  # plaintext code — caller passes this to the SMS gateway, never persists it
    expires_at: datetime


def _recent_send_count(cur, promoter_id: int, since: datetime) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS count FROM otp_verifications
        WHERE promoter_id = %s AND created_at >= %s
        """,
        (promoter_id, since),
    )
    return cur.fetchone()["count"]


def _last_send_time(cur, promoter_id: int) -> Optional[datetime]:
    cur.execute(
        """
        SELECT created_at FROM otp_verifications
        WHERE promoter_id = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (promoter_id,),
    )
    row = cur.fetchone()
    return row["created_at"] if row else None


def can_send_otp(promoter_id: int) -> bool:
    """
    Enforce §4 of the architecture doc: min OTP_RESEND_COOLDOWN_SECONDS between
    sends, max OTP_MAX_SENDS_PER_HOUR per promoter per rolling hour. Checked
    against persisted rows (not in-memory state) so it survives bot restarts.
    """
    now = datetime.now(timezone.utc)
    with get_cursor() as cur:
        last_sent = _last_send_time(cur, promoter_id)
        if last_sent is not None:
            elapsed = (now - last_sent).total_seconds()
            if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                return False

        hour_ago = now - timedelta(hours=1)
        sends_in_last_hour = _recent_send_count(cur, promoter_id, hour_ago)
        if sends_in_last_hour >= OTP_MAX_SENDS_PER_HOUR:
            return False

    return True


def create_otp_verification(promoter_id: int, phone_number: str) -> OtpIssueResult:
    """
    Generate a new OTP, hash and persist it, and return the plaintext code so
    the caller can hand it to the SMS gateway. Raises RateLimitedError if
    can_send_otp() would return False — callers should check can_send_otp()
    first to give the user a friendly message, but this is enforced here too
    as a safety net.
    """
    if not can_send_otp(promoter_id):
        raise RateLimitedError(
            f"Promoter {promoter_id} has exceeded the OTP resend rate limit"
        )

    code = generate_otp_code()
    code_hash = hash_otp_code(code, phone_number)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO otp_verifications
                (promoter_id, otp_code_hash, phone_number, status, expires_at)
            VALUES (%s, %s, %s, 'sent', %s)
            RETURNING id, expires_at
            """,
            (promoter_id, code_hash, phone_number, expires_at),
        )
        row = cur.fetchone()

    return OtpIssueResult(otp_verification_id=row["id"], code=code, expires_at=row["expires_at"])


def record_provider_message_id(otp_verification_id: int, provider_message_id: str) -> None:
    """Attach the SMS gateway's message id to an OTP row once the send call returns."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE otp_verifications SET provider_message_id = %s WHERE id = %s",
            (provider_message_id, otp_verification_id),
        )


def _latest_active_otp(cur, promoter_id: int) -> Optional[dict]:
    cur.execute(
        """
        SELECT * FROM otp_verifications
        WHERE promoter_id = %s AND status = 'sent'
        ORDER BY created_at DESC LIMIT 1
        """,
        (promoter_id,),
    )
    return cur.fetchone()


class OtpValidationResult:
    VERIFIED = "verified"
    WRONG_CODE = "wrong_code"
    EXPIRED = "expired"
    MAX_ATTEMPTS = "max_attempts"
    NOT_FOUND = "not_found"


def verify_otp(promoter_id: int, submitted_code: str) -> str:
    """
    Validate a submitted OTP code against the promoter's latest 'sent' OTP row.

    Returns one of the OtpValidationResult constants and updates DB state:
      - VERIFIED: otp_verifications.status='verified', promoters.status='verified'
      - WRONG_CODE: attempt_count incremented; if it now reaches OTP_MAX_ATTEMPTS,
        the OTP row is also marked 'failed'
      - EXPIRED: otp row marked 'expired'
      - MAX_ATTEMPTS: otp row already exhausted its attempts (status='failed')
      - NOT_FOUND: no active OTP row exists for this promoter (e.g. never
        requested, or already resolved) — caller should prompt for a resend
    """
    with get_cursor() as cur:
        otp_row = _latest_active_otp(cur, promoter_id)
        if otp_row is None:
            return OtpValidationResult.NOT_FOUND

        cur.execute("SELECT campaign_id FROM promoters WHERE id = %s", (promoter_id,))
        promoter_row = cur.fetchone()
        campaign_id = promoter_row["campaign_id"] if promoter_row else None

        now = datetime.now(timezone.utc)
        expires_at = otp_row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if now >= expires_at:
            cur.execute(
                "UPDATE otp_verifications SET status = 'expired' WHERE id = %s",
                (otp_row["id"],),
            )
            _log_event(cur, promoter_id, "otp_expired", campaign_id=campaign_id)
            return OtpValidationResult.EXPIRED

        if otp_row["attempt_count"] >= OTP_MAX_ATTEMPTS:
            cur.execute(
                "UPDATE otp_verifications SET status = 'failed' WHERE id = %s",
                (otp_row["id"],),
            )
            return OtpValidationResult.MAX_ATTEMPTS

        is_match = _verify_otp_hash(submitted_code, otp_row["phone_number"], otp_row["otp_code_hash"])

        if is_match:
            cur.execute(
                """
                UPDATE otp_verifications
                SET status = 'verified', verified_at = now()
                WHERE id = %s
                """,
                (otp_row["id"],),
            )
            cur.execute(
                """
                UPDATE promoters
                SET status = 'verified', verified_at = now()
                WHERE id = %s
                """,
                (promoter_id,),
            )
            _log_event(cur, promoter_id, "otp_verified", campaign_id=campaign_id)
            return OtpValidationResult.VERIFIED

        new_attempt_count = otp_row["attempt_count"] + 1
        new_status = "failed" if new_attempt_count >= OTP_MAX_ATTEMPTS else "sent"
        cur.execute(
            """
            UPDATE otp_verifications
            SET attempt_count = %s, status = %s
            WHERE id = %s
            """,
            (new_attempt_count, new_status, otp_row["id"]),
        )
        _log_event(
            cur, promoter_id, "otp_failed", campaign_id=campaign_id,
            metadata={"attempt_count": new_attempt_count, "reason": "wrong_code"},
        )
        return (
            OtpValidationResult.MAX_ATTEMPTS
            if new_status == "failed"
            else OtpValidationResult.WRONG_CODE
        )


# ----------------------------------------------------------------------------
# sms_delivery_logs
# ----------------------------------------------------------------------------

def record_sms_delivery(
    otp_verification_id: int,
    provider: str,
    provider_status: Optional[str] = None,
    raw_response: Optional[dict] = None,
) -> None:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO sms_delivery_logs (otp_verification_id, provider, provider_status, raw_response)
            VALUES (%s, %s, %s, %s)
            """,
            (
                otp_verification_id,
                provider,
                provider_status,
                psycopg2.extras.Json(raw_response) if raw_response is not None else None,
            ),
        )


def record_customer_sms_delivery(
    customer_verification_id: int,
    provider: str,
    provider_status: Optional[str] = None,
    raw_response: Optional[dict] = None,
) -> None:
    """Same as record_sms_delivery(), but for an OTP sent to a customer."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO sms_delivery_logs (customer_verification_id, provider, provider_status, raw_response)
            VALUES (%s, %s, %s, %s)
            """,
            (
                customer_verification_id,
                provider,
                provider_status,
                psycopg2.extras.Json(raw_response) if raw_response is not None else None,
            ),
        )


# ----------------------------------------------------------------------------
# verification_events (funnel/audit log)
# ----------------------------------------------------------------------------

_VALID_EVENT_TYPES = {
    "started", "phone_captured", "otp_sent",
    "otp_verified", "otp_failed", "otp_expired",
    # Customer-verification events (see 002_add_customers.sql). promoter_id is
    # always set on these too — it's the promoter running the verification —
    # customer_id identifies who's actually being verified.
    "customer_started", "customer_phone_captured", "customer_otp_sent",
    "customer_otp_verified", "customer_otp_failed", "customer_otp_expired",
}


def _log_event(
    cur,
    promoter_id: int,
    event_type: str,
    campaign_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
    customer_id: Optional[int] = None,
) -> None:
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(f"Invalid event_type: {event_type!r}")

    cur.execute(
        """
        INSERT INTO verification_events (promoter_id, campaign_id, event_type, metadata, customer_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            promoter_id,
            campaign_id,
            event_type,
            psycopg2.extras.Json(metadata) if metadata is not None else None,
            customer_id,
        ),
    )


def log_event(
    promoter_id: int,
    event_type: str,
    campaign_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
    customer_id: Optional[int] = None,
) -> None:
    """
    Public entry point for logging a verification_events row from bot handlers
    (e.g. 'started', 'phone_captured', 'customer_started'). The
    otp_sent/otp_verified/otp_failed/otp_expired and their customer_*
    counterparts are logged automatically by create_otp_verification()/
    verify_otp() and create_customer_verification()/verify_customer_otp() at
    the appropriate points. Pass customer_id only for customer_* event types —
    leave it unset for a promoter's own verification events.
    """
    with get_cursor() as cur:
        _log_event(
            cur, promoter_id, event_type,
            campaign_id=campaign_id, metadata=metadata, customer_id=customer_id,
        )


def log_otp_sent(promoter_id: int, campaign_id: Optional[int], otp_verification_id: int) -> None:
    """Call this after create_otp_verification() succeeds and the SMS gateway accepts the send."""
    with get_cursor() as cur:
        _log_event(
            cur, promoter_id, "otp_sent", campaign_id=campaign_id,
            metadata={"otp_verification_id": otp_verification_id},
        )


# ----------------------------------------------------------------------------
# Customer verification (see 002_add_customers.sql)
#
# A promoter collects a customer's phone number (and optional name); the OTP
# is sent to the CUSTOMER, but every step is logged under the PROMOTER who
# ran it. This section mirrors the promoter self-verification functions
# above almost 1:1 — same hashing, expiry, attempt-limit, and rate-limit
# rules from OTP_* — just scoped to a (customer_id, promoter_id) pair instead
# of a single promoter_id.
# ----------------------------------------------------------------------------

def upsert_customer(phone_number: str, full_name: Optional[str] = None) -> dict:
    """
    Insert a customer row keyed by phone_number, or return the existing one.
    Unlike upsert_promoter (keyed on a stable telegram_user_id), a customer's
    only stable identifier is their phone number, so that's the global dedup
    key — the same customer verified by two different promoters over time is
    still one customer row. A blank/omitted name on a repeat visit doesn't
    erase a name captured earlier.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO customers (phone_number, full_name)
            VALUES (%s, %s)
            ON CONFLICT (phone_number) DO UPDATE
                SET full_name = COALESCE(NULLIF(EXCLUDED.full_name, ''), customers.full_name)
            RETURNING id, phone_number, full_name, status, created_at, updated_at, verified_at
            """,
            (phone_number, full_name),
        )
        return cur.fetchone()


def get_customer_by_phone(phone_number: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM customers WHERE phone_number = %s", (phone_number,))
        return cur.fetchone()


def get_customer_by_id(customer_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
        return cur.fetchone()


def set_customer_status(customer_id: int, status: str, verified: bool = False) -> dict:
    valid_statuses = {"pending", "otp_sent", "verified", "failed"}
    if status not in valid_statuses:
        raise ValueError(f"Invalid customer status: {status!r}")

    with get_cursor() as cur:
        if verified:
            cur.execute(
                """
                UPDATE customers SET status = %s, verified_at = now()
                WHERE id = %s
                RETURNING id, status, verified_at
                """,
                (status, customer_id),
            )
        else:
            cur.execute(
                "UPDATE customers SET status = %s WHERE id = %s RETURNING id, status, verified_at",
                (status, customer_id),
            )
        return cur.fetchone()


def _recent_customer_send_count(cur, customer_id: int, since: datetime) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS count FROM customer_verifications
        WHERE customer_id = %s AND created_at >= %s
        """,
        (customer_id, since),
    )
    return cur.fetchone()["count"]


def _last_customer_send_time(cur, customer_id: int) -> Optional[datetime]:
    cur.execute(
        """
        SELECT created_at FROM customer_verifications
        WHERE customer_id = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (customer_id,),
    )
    row = cur.fetchone()
    return row["created_at"] if row else None


def can_send_customer_otp(customer_id: int) -> bool:
    """Same cooldown/hourly-cap rules as can_send_otp(), scoped to a customer's phone."""
    now = datetime.now(timezone.utc)
    with get_cursor() as cur:
        last_sent = _last_customer_send_time(cur, customer_id)
        if last_sent is not None:
            elapsed = (now - last_sent).total_seconds()
            if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                return False

        hour_ago = now - timedelta(hours=1)
        if _recent_customer_send_count(cur, customer_id, hour_ago) >= OTP_MAX_SENDS_PER_HOUR:
            return False

    return True


@dataclass
class CustomerOtpIssueResult:
    customer_verification_id: int
    code: str  # plaintext — caller passes this to the SMS gateway, never persists it
    expires_at: datetime


def create_customer_verification(customer_id: int, promoter_id: int, phone_number: str) -> CustomerOtpIssueResult:
    """Generate and persist a new OTP for a customer, attributed to promoter_id."""
    if not can_send_customer_otp(customer_id):
        raise RateLimitedError(
            f"Customer {customer_id} has exceeded the OTP resend rate limit"
        )

    code = generate_otp_code()
    code_hash = hash_otp_code(code, phone_number)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO customer_verifications
                (customer_id, promoter_id, otp_code_hash, phone_number, status, expires_at)
            VALUES (%s, %s, %s, %s, 'sent', %s)
            RETURNING id, expires_at
            """,
            (customer_id, promoter_id, code_hash, phone_number, expires_at),
        )
        row = cur.fetchone()

    return CustomerOtpIssueResult(
        customer_verification_id=row["id"], code=code, expires_at=row["expires_at"]
    )


def record_customer_provider_message_id(customer_verification_id: int, provider_message_id: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE customer_verifications SET provider_message_id = %s WHERE id = %s",
            (provider_message_id, customer_verification_id),
        )


def _latest_active_customer_otp(cur, customer_id: int) -> Optional[dict]:
    cur.execute(
        """
        SELECT * FROM customer_verifications
        WHERE customer_id = %s AND status = 'sent'
        ORDER BY created_at DESC LIMIT 1
        """,
        (customer_id,),
    )
    return cur.fetchone()


def verify_customer_otp(customer_id: int, promoter_id: int, submitted_code: str) -> str:
    """
    Same state machine as verify_otp(), scoped to a customer being verified by
    a specific promoter. Returns an OtpValidationResult constant. Every
    resulting verification_events row carries customer_id=customer_id AND
    promoter_id=promoter_id, so "who verified this customer" is always
    reconstructable from the log, not just from the current customers.status.
    """
    with get_cursor() as cur:
        otp_row = _latest_active_customer_otp(cur, customer_id)
        if otp_row is None:
            return OtpValidationResult.NOT_FOUND

        cur.execute("SELECT campaign_id FROM promoters WHERE id = %s", (promoter_id,))
        promoter_row = cur.fetchone()
        campaign_id = promoter_row["campaign_id"] if promoter_row else None

        now = datetime.now(timezone.utc)
        expires_at = otp_row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if now >= expires_at:
            cur.execute(
                "UPDATE customer_verifications SET status = 'expired' WHERE id = %s",
                (otp_row["id"],),
            )
            _log_event(
                cur, promoter_id, "customer_otp_expired",
                campaign_id=campaign_id, customer_id=customer_id,
            )
            return OtpValidationResult.EXPIRED

        if otp_row["attempt_count"] >= OTP_MAX_ATTEMPTS:
            cur.execute(
                "UPDATE customer_verifications SET status = 'failed' WHERE id = %s",
                (otp_row["id"],),
            )
            return OtpValidationResult.MAX_ATTEMPTS

        is_match = _verify_otp_hash(submitted_code, otp_row["phone_number"], otp_row["otp_code_hash"])

        if is_match:
            cur.execute(
                """
                UPDATE customer_verifications
                SET status = 'verified', verified_at = now()
                WHERE id = %s
                """,
                (otp_row["id"],),
            )
            cur.execute(
                """
                UPDATE customers
                SET status = 'verified', verified_at = now()
                WHERE id = %s
                """,
                (customer_id,),
            )
            _log_event(
                cur, promoter_id, "customer_otp_verified",
                campaign_id=campaign_id, customer_id=customer_id,
            )
            return OtpValidationResult.VERIFIED

        new_attempt_count = otp_row["attempt_count"] + 1
        new_status = "failed" if new_attempt_count >= OTP_MAX_ATTEMPTS else "sent"
        cur.execute(
            """
            UPDATE customer_verifications
            SET attempt_count = %s, status = %s
            WHERE id = %s
            """,
            (new_attempt_count, new_status, otp_row["id"]),
        )
        _log_event(
            cur, promoter_id, "customer_otp_failed",
            campaign_id=campaign_id, customer_id=customer_id,
            metadata={"attempt_count": new_attempt_count, "reason": "wrong_code"},
        )
        return (
            OtpValidationResult.MAX_ATTEMPTS
            if new_status == "failed"
            else OtpValidationResult.WRONG_CODE
        )


def log_customer_otp_sent(
    promoter_id: int, campaign_id: Optional[int], customer_id: int, customer_verification_id: int
) -> None:
    """Call this after create_customer_verification() succeeds and the SMS gateway accepts the send."""
    with get_cursor() as cur:
        _log_event(
            cur, promoter_id, "customer_otp_sent",
            campaign_id=campaign_id, customer_id=customer_id,
            metadata={"customer_verification_id": customer_verification_id},
        )
