"""
sms.py — SMS gateway abstraction for OTP delivery.

Wraps an external SMS REST API behind a small, provider-agnostic interface so
main.py never depends on a specific vendor's request/response shape. Swapping
providers means changing the payload/response mapping in this file only.

Required environment variables (production):
  SMS_API_URL     base endpoint the provider exposes for sending a message
  SMS_API_KEY     bearer token / API key for that endpoint

Optional environment variables:
  SMS_SENDER_ID              sender name/number shown to the recipient, if the
                              provider supports it (default: unset)
  SMS_TIMEOUT_SECONDS         per-request HTTP timeout (default: 10)
  SMS_MAX_RETRIES             retries for transient failures (timeouts, 5xx)
                              before giving up (default: 2)
  SMS_MOCK_MODE               "true" to force mock mode even if
                              SMS_API_URL/SMS_API_KEY are set (default: "false")
  SMS_RESPONSE_ID_FIELD       dotted path used to pull the provider's message id
                              out of its JSON response, e.g. "data.id"
                              (default: tries "message_id", "id", "data.id" in order)

Mock mode: if SMS_MOCK_MODE=true, or SMS_API_URL/SMS_API_KEY are not both set,
send_otp() never makes a network call — it logs the code (useful for local
dev) and returns a synthetic "MOCK-<uuid4>" message id. This lets the bot run
end-to-end without a real SMS contract. A warning is logged when mock mode is
entered implicitly (missing config) rather than explicitly requested, so it's
hard to end up there by accident in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a dev convenience, not a hard requirement
    pass

logger = logging.getLogger(__name__)


class SmsError(Exception):
    """Base class for all SMS-related failures."""


class SmsConfigError(SmsError):
    """Raised when the client is misconfigured for a real (non-mock) send."""


class SmsSendError(SmsError):
    """
    Raised when an OTP could not be delivered after retries. Callers (main.py)
    should catch this, log an 'otp_failed' verification_event, and let the
    promoter know delivery failed rather than leaving them staring at silence.
    """


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _extract_message_id(payload: Any, field_path: Optional[str]) -> str:
    """
    Best-effort extraction of a provider message id from a JSON response.
    Tries an explicit dotted field_path first (e.g. "data.id"), then falls
    back to common conventions. Falls back to a generated id (with a warning)
    rather than raising, since a missing message id shouldn't fail an
    otherwise-successful send.
    """
    candidates = []
    if field_path:
        candidates.append(field_path)
    candidates.extend(["message_id", "id", "data.message_id", "data.id"])

    for path in candidates:
        node = payload
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if node is not None:
            return str(node)

    generated = f"UNKNOWN-{uuid.uuid4()}"
    logger.warning(
        "SMS provider response did not contain a recognizable message id; "
        "using generated id %s. Response was: %r",
        generated,
        payload,
    )
    return generated


def _strip_plus(e164_phone: str) -> str:
    """
    SMSEthiopia's msisdn format is the country code + number with NO '+' and
    no spaces (e.g. "251911234567"), while the rest of this system stores
    phone numbers in E.164 with a leading '+' (see database.normalize_phone_number).
    Convert only at the SMS-gateway boundary — the DB/otp_verifications keep
    the '+' form throughout.
    """
    return e164_phone.lstrip("+")


class SmsClient:
    """
    Thin async wrapper around an SMS gateway's REST API.

    Usage:
        client = SmsClient()
        try:
            provider_message_id = await client.send_otp("+251912345678", "048213")
        except SmsSendError as exc:
            ...
        finally:
            await client.close()

    Or as an async context manager:
        async with SmsClient() as client:
            await client.send_otp(phone, code)
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        sender_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        mock_mode: Optional[bool] = None,
        response_id_field: Optional[str] = None,
    ) -> None:
        self.api_url = api_url or os.environ.get("SMS_API_URL")
        self.api_key = api_key or os.environ.get("SMS_API_KEY")
        self.sender_id = sender_id or os.environ.get("SMS_SENDER_ID")
        self.timeout_seconds = timeout_seconds or float(os.environ.get("SMS_TIMEOUT_SECONDS", "10"))
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("SMS_MAX_RETRIES", "2"))
        self.response_id_field = response_id_field or os.environ.get("SMS_RESPONSE_ID_FIELD")

        explicit_mock = _get_bool_env("SMS_MOCK_MODE", default=False) if mock_mode is None else mock_mode
        missing_config = not (self.api_url and self.api_key)

        if explicit_mock:
            self.mock_mode = True
        elif missing_config:
            self.mock_mode = True
            logger.warning(
                "SMS_API_URL/SMS_API_KEY are not both configured — falling back to mock mode. "
                "OTP codes will be logged, not sent, until real credentials are provided."
            )
        else:
            self.mock_mode = False

        self._http_client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "SmsClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._http_client

    def _build_message(self, code: str, ttl_minutes: Optional[int] = None) -> str:
        if ttl_minutes:
            return f"Your verification code is {code}. It expires in {ttl_minutes} minutes."
        return f"Your verification code is {code}."

    async def _mock_send(self, phone: str, message: str) -> str:
        mock_id = f"MOCK-{uuid.uuid4()}"
        logger.info("[SMS MOCK] to=%s message=%r provider_message_id=%s", phone, message, mock_id)
        return mock_id

    async def _real_send(self, phone: str, message: str) -> str:
        if not self.api_url or not self.api_key:
            raise SmsConfigError("SMS_API_URL and SMS_API_KEY must both be set to send a real SMS")

        # SMSEthiopia's documented request shape: {"msisdn": "<digits, no +>", "text": "<message>"}.
        # There is no sender_id request field — the sender name (e.g. "INDOMIE")
        # is tied to the API key via campaign approval in their console, not
        # passed per-request. self.sender_id is kept only for logging/reference.
        payload = {
            "msisdn": _strip_plus(phone),
            "text": message,
        }

        # SMSEthiopia authenticates via a "KEY" header (not "Authorization: Bearer").
        headers = {
            "KEY": self.api_key,
            "Content-Type": "application/json",
        }

        client = self._client()
        last_error: Optional[Exception] = None
        backoff_seconds = 1.0

        for attempt in range(1, self.max_retries + 2):  # +1 initial try, +1 for range inclusivity
            try:
                response = await client.post(self.api_url, json=payload, headers=headers)
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError:
                    data = {}

                # SMSEthiopia's documented success response is
                # {"status": "success", "message": "SMS sent successfully"} — it
                # doesn't include a provider message id. Treat a non-"success"
                # status as a failure even on HTTP 200, since their API can
                # apparently return errors with a 200 status code.
                status_field = data.get("status") if isinstance(data, dict) else None
                if status_field is not None and status_field != "success":
                    error_detail = data.get("message", "unknown error") if isinstance(data, dict) else "unknown error"
                    raise SmsSendError(f"SMSEthiopia rejected the request: {error_detail}")

                return _extract_message_id(data, self.response_id_field)

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500:
                    # Client errors (bad request, auth failure, invalid phone, etc.) won't
                    # succeed on retry — fail fast with the provider's own error body.
                    body_preview = exc.response.text[:500]
                    raise SmsSendError(
                        f"SMS gateway rejected the request (HTTP {status}): {body_preview}"
                    ) from exc
                last_error = exc
                logger.warning(
                    "SMS gateway returned HTTP %s on attempt %s/%s; will retry",
                    status, attempt, self.max_retries + 1,
                )

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning(
                    "SMS gateway request failed (%s) on attempt %s/%s; will retry",
                    exc, attempt, self.max_retries + 1,
                )

            if attempt <= self.max_retries:
                await asyncio.sleep(backoff_seconds)
                backoff_seconds *= 2

        raise SmsSendError(
            f"Failed to send SMS to {phone} after {self.max_retries + 1} attempts"
        ) from last_error

    async def send_otp(self, phone: str, code: str, ttl_minutes: Optional[int] = None) -> str:
        """
        Send an OTP code to `phone`. Returns the provider's message id (or a
        synthetic MOCK-... id in mock mode). Raises SmsSendError if delivery
        ultimately fails after retries.

        `phone` is expected to already be normalized to E.164
        (see database.normalize_phone_number) — this client does not
        re-validate phone format, since that's the caller's responsibility.
        """
        message = self._build_message(code, ttl_minutes)

        if self.mock_mode:
            return await self._mock_send(phone, message)

        return await self._real_send(phone, message)


# ----------------------------------------------------------------------------
# Module-level singleton, for callers that just want a shared client without
# managing lifecycle themselves (main.py creates/closes this explicitly around
# the bot's polling loop instead — see main.py's `on_startup`/`on_shutdown`).
# ----------------------------------------------------------------------------

_default_client: Optional[SmsClient] = None


def get_sms_client() -> SmsClient:
    """Return a process-wide SmsClient, creating it on first use."""
    global _default_client
    if _default_client is None:
        _default_client = SmsClient()
    return _default_client


async def close_sms_client() -> None:
    global _default_client
    if _default_client is not None:
        await _default_client.close()
        _default_client = None