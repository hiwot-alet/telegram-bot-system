"""
main.py — aiogram v3 Telegram bot engine for the promoter verification flow.

Promoter-facing commands:
  /start            capture the campaign deep-link slug (t.me/<bot>?start=<slug>)
                    and upsert the promoter row
  /verify           start phone capture (native "share contact" button or typed number)
  /confirm <code>   validate an OTP code (also works as bare text while awaiting a code)
  /help             command summary

Admin-only commands (Telegram user id must be in ADMIN_TELEGRAM_IDS):
  /add <slug> <name>   create a new campaign
  /deactivate <slug>   close a campaign
  /export [slug]       generate and send an .xlsx verification report
                        (all campaigns if slug omitted)
  /addpromoter <telegram_username> [city] [campaign_slug]
                       approve a Telegram username to use the bot
  /removepromoter <telegram_username>
                       revoke a promoter's access (past history is kept)
  /listpromoters [active|revoked]
                       show the current roster

All state-changing logic (OTP hashing, expiry, resend cooldown, attempt
limits) lives in database.py (Phase 1) — this module is the Telegram-facing
wiring around it plus the SMS send call.

Required environment variables:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  DATABASE_URL         see database.py
  OTP_HASH_SECRET       see database.py

Optional environment variables:
  ADMIN_TELEGRAM_IDS   comma-separated Telegram user ids allowed to run admin commands
  LOG_LEVEL            Python logging level name (default: INFO)

  ...plus everything database.py and sms.py read (OTP_*, SMS_*, DB_POOL_*).
  See .env.example for the full list.

Run with:
  python main.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

import openpyxl
import psycopg2.errors
from openpyxl.styles import Font

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import database as db
from sms import SmsClient, SmsSendError

# ----------------------------------------------------------------------------
# Configuration & logging
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

ADMIN_TELEGRAM_IDS = {
    int(x) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()
}


# ----------------------------------------------------------------------------
# FSM states
#
# These only gate whether a bare text message is interpreted as "a phone
# number" vs "an OTP code" — they are NOT the source of truth for
# verification progress (that's promoters.status / otp_verifications in the
# DB). If the bot restarts and a promoter's FSM state is lost, /verify and
# /confirm <code> still work correctly because they re-derive everything
# from the database.
# ----------------------------------------------------------------------------

class VerifyStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()


# A promoter verifying a customer is a separate flow from verifying
# themselves. Since FSMContext holds one state per chat, a promoter can be in
# VerifyStates.* OR CustomerStates.* at a time, never both — they finish (or
# abandon) one before starting the other. customer_phone/customer_id are
# carried between steps via state.update_data()/get_data().
class CustomerStates(StatesGroup):
    waiting_for_customer_phone = State()
    waiting_for_customer_name = State()
    waiting_for_customer_otp = State()


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

async def run_db(func, *args, **kwargs):
    """Run a blocking psycopg2 call (database.py) off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in ADMIN_TELEGRAM_IDS


# Messages that look like commands should never be swallowed by the
# state-scoped "bare text" handlers below, regardless of router/handler
# registration order.
NOT_A_COMMAND = ~F.text.startswith("/")

router = Router(name="promoter")
admin_router = Router(name="admin")
# Non-admins simply get no response from these handlers (falls through with
# no match) rather than an explicit "not authorized" — deliberate, so the
# bot doesn't advertise admin command names to arbitrary users.
admin_router.message.filter(IsAdmin())


# ----------------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    telegram_user = message.from_user

    # ------------------------------------------------------------------
    # Roster gate: only Telegram usernames an admin has pre-registered via
    # /addpromoter may use the bot at all. This is the access-control model
    # INDOMIE asked for — revoking/onboarding a promoter is a single admin
    # command on our side, no engineer involvement needed.
    # ------------------------------------------------------------------
    if telegram_user.id not in ADMIN_TELEGRAM_IDS:
        roster_entry = await run_db(db.is_promoter_allowed, telegram_user.username)
        if roster_entry is None:
            await message.answer(
                "This bot is private and restricted to registered promoters. "
                "Your Telegram account isn't on the approved list yet — please make sure you "
                "have a Telegram @username set, then contact your campaign administrator to be added."
            )
            return

    campaign_slug = (command.args or "").strip() or None
    campaign = None
    if campaign_slug:
        campaign = await run_db(db.get_campaign_by_slug, campaign_slug)
        if campaign is None:
            await message.answer(
                "That campaign link doesn't look valid or may have expired. "
                "You can still verify below, or ask the campaign organizer for a fresh link."
            )
        elif campaign["status"] != "active":
            await message.answer(
                f'The "{campaign["name"]}" campaign is not currently accepting new verifications.'
            )

    # A roster entry's own campaign/city (set by the admin at /addpromoter
    # time) takes priority over a deep-link slug, so a promoter can't be
    # reassigned just by someone sharing a different campaign's link.
    roster_entry = None if telegram_user.id in ADMIN_TELEGRAM_IDS else await run_db(
        db.is_promoter_allowed, telegram_user.username
    )
    effective_campaign_id = (
        roster_entry["campaign_id"] if roster_entry and roster_entry.get("campaign_id") else (campaign["id"] if campaign else None)
    )

    promoter = await run_db(
        db.upsert_promoter,
        telegram_user_id=telegram_user.id,
        telegram_username=telegram_user.username,
        full_name=telegram_user.full_name,
        campaign_id=effective_campaign_id,
    )

    if roster_entry and roster_entry.get("city"):
        await run_db(db.set_promoter_city, promoter["id"], roster_entry["city"])

    await run_db(db.log_event, promoter["id"], "started", campaign_id=promoter.get("campaign_id"))

    if promoter["status"] == "verified":
        await message.answer(f"Welcome back, {telegram_user.full_name}! You're already verified. ✅")
        return

    await message.answer(
        f"Welcome, {telegram_user.full_name}! 👋\n\n"
        "To complete promoter verification, send /verify and share your phone number."
    )


# ----------------------------------------------------------------------------
# /verify — phone capture
# ----------------------------------------------------------------------------

@router.message(Command("verify"))
async def cmd_verify(message: Message, state: FSMContext) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None:
        await message.answer("Please send /start first.")
        return

    if promoter["status"] == "blocked":
        await message.answer("Your access has been revoked. Please contact your campaign administrator.")
        return

    if promoter["status"] == "verified":
        await message.answer("You're already verified. ✅")
        return

    await state.set_state(VerifyStates.waiting_for_phone)
    await message.answer(
        "Tap the button below to share your phone number, or type it directly "
        "(e.g. 0912345678 or +251912345678).",
        reply_markup=phone_keyboard(),
    )


@router.message(VerifyStates.waiting_for_phone, F.contact)
async def handle_contact(message: Message, state: FSMContext, sms_client: SmsClient) -> None:
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer(
            "That looks like someone else's contact card. Please share your own phone number."
        )
        return
    await _capture_phone_and_send_otp(message, state, sms_client, contact.phone_number)


@router.message(VerifyStates.waiting_for_phone, F.text, NOT_A_COMMAND)
async def handle_phone_text(message: Message, state: FSMContext, sms_client: SmsClient) -> None:
    await _capture_phone_and_send_otp(message, state, sms_client, message.text)


async def _capture_phone_and_send_otp(
    message: Message, state: FSMContext, sms_client: SmsClient, raw_phone: str
) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None:
        await message.answer("Please send /start first.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    try:
        phone = await run_db(db.normalize_phone_number, raw_phone)
    except ValueError:
        await message.answer(
            "That doesn't look like a valid phone number. Please try again "
            "(e.g. 0912345678 or +251912345678)."
        )
        return

    await run_db(db.set_promoter_phone, promoter["id"], phone)
    await run_db(db.log_event, promoter["id"], "phone_captured", campaign_id=promoter.get("campaign_id"))

    await _issue_and_send_otp(message, state, sms_client, promoter["id"], promoter.get("campaign_id"), phone)


async def _issue_and_send_otp(
    message: Message,
    state: FSMContext,
    sms_client: SmsClient,
    promoter_id: int,
    campaign_id: Optional[int],
    phone: str,
) -> None:
    allowed = await run_db(db.can_send_otp, promoter_id)
    if not allowed:
        await message.answer(
            "Please wait a bit before requesting another code "
            f"(max {db.OTP_MAX_SENDS_PER_HOUR} per hour, "
            f"{db.OTP_RESEND_COOLDOWN_SECONDS}s between sends).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    try:
        issue = await run_db(db.create_otp_verification, promoter_id, phone)
    except db.RateLimitedError:
        await message.answer(
            "Please wait a bit before requesting another code.", reply_markup=ReplyKeyboardRemove()
        )
        return

    try:
        provider_message_id = await sms_client.send_otp(phone, issue.code, ttl_minutes=db.OTP_TTL_MINUTES)
    except SmsSendError as exc:
        logger.error("SMS delivery failed for promoter_id=%s: %s", promoter_id, exc)
        await run_db(
            db.log_event, promoter_id, "otp_failed", campaign_id=campaign_id,
            metadata={"reason": "sms_gateway_error", "detail": str(exc)},
        )
        await message.answer(
            "We couldn't send the verification code just now. Please try /verify again in a "
            "moment — if this keeps happening, contact the campaign organizer.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await run_db(db.record_provider_message_id, issue.otp_verification_id, provider_message_id)
    await run_db(
        db.record_sms_delivery, issue.otp_verification_id,
        provider="sms_gateway", provider_status="sent",
    )
    await run_db(db.set_promoter_status, promoter_id, "otp_sent")
    await run_db(db.log_otp_sent, promoter_id, campaign_id, issue.otp_verification_id)

    await state.set_state(VerifyStates.waiting_for_otp)
    await message.answer(
        f"📨 A {db.OTP_LENGTH}-digit code was sent to {phone}. It expires in {db.OTP_TTL_MINUTES} minutes.\n\n"
        "Reply with the code, or use /confirm <code>.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ----------------------------------------------------------------------------
# /confirm <code> — also accepts bare-digit text while awaiting a code
# ----------------------------------------------------------------------------

@router.message(Command("confirm"))
async def cmd_confirm(message: Message, command: CommandObject, state: FSMContext) -> None:
    code = (command.args or "").strip()
    if not code:
        await message.answer("Usage: /confirm <code> — e.g. /confirm 048213")
        return
    await _process_otp_attempt(message, state, code)


@router.message(VerifyStates.waiting_for_otp, F.text, NOT_A_COMMAND)
async def handle_otp_text(message: Message, state: FSMContext) -> None:
    await _process_otp_attempt(message, state, message.text.strip())


async def _process_otp_attempt(message: Message, state: FSMContext, code: str) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None or not promoter.get("phone_number"):
        await message.answer("Please send /verify first to start verification.")
        return

    if not re.fullmatch(r"\d{4,8}", code):
        await message.answer("Please send just the numeric code, e.g. /confirm 048213.")
        return

    result = await run_db(db.verify_otp, promoter["id"], code)

    if result == db.OtpValidationResult.VERIFIED:
        await state.clear()
        logger.info("VERIFIED promoter_id=%s telegram_user_id=%s", promoter["id"], message.from_user.id)
        await message.answer("✅ VERIFIED — thanks, your phone number is confirmed!")

    elif result == db.OtpValidationResult.WRONG_CODE:
        logger.info("FAILED (wrong_code) promoter_id=%s", promoter["id"])
        await message.answer("❌ That code doesn't match. Please double-check and try again.")

    elif result == db.OtpValidationResult.EXPIRED:
        logger.info("FAILED (expired) promoter_id=%s", promoter["id"])
        await state.clear()
        await message.answer("⏰ That code expired. Send /verify to request a new one.")

    elif result == db.OtpValidationResult.MAX_ATTEMPTS:
        logger.info("FAILED (max_attempts) promoter_id=%s", promoter["id"])
        await run_db(db.set_promoter_status, promoter["id"], "failed")
        await state.clear()
        await message.answer(
            f"🚫 Too many incorrect attempts (max {db.OTP_MAX_ATTEMPTS}). "
            "Send /verify to request a new code."
        )

    else:  # NOT_FOUND — no active OTP row (never requested, or already resolved)
        await state.clear()
        await message.answer("No active verification code found. Send /verify to request one.")


# ----------------------------------------------------------------------------
# /addcustomer — a promoter collects a customer's phone (+ optional name),
# an OTP goes to the CUSTOMER, and /confirmcustomer <code> resolves it.
# Requires the promoter to already be verified themselves.
# ----------------------------------------------------------------------------

@router.message(Command("addcustomer"))
async def cmd_add_customer(message: Message, state: FSMContext) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None:
        await message.answer("Please send /start first.")
        return

    if promoter["status"] == "blocked":
        await message.answer("Your access has been revoked. Please contact your campaign administrator.")
        return

    if promoter["status"] != "verified":
        await message.answer(
            "You need to complete your own /verify first before you can verify customers."
        )
        return

    await state.set_state(CustomerStates.waiting_for_customer_phone)
    await message.answer(
        "Let's verify a customer. What's their phone number? "
        "(e.g. 0912345678 or +251912345678)"
    )


@router.message(CustomerStates.waiting_for_customer_phone, F.text, NOT_A_COMMAND)
async def handle_customer_phone_text(message: Message, state: FSMContext) -> None:
    try:
        phone = await run_db(db.normalize_phone_number, message.text)
    except ValueError:
        await message.answer(
            "That doesn't look like a valid phone number. Please try again "
            "(e.g. 0912345678 or +251912345678)."
        )
        return

    await state.update_data(customer_phone=phone)
    await state.set_state(CustomerStates.waiting_for_customer_name)
    await message.answer(
        "Got it. What's the customer's name? Send /skip to leave it blank."
    )


@router.message(CustomerStates.waiting_for_customer_name, Command("skip"))
async def handle_customer_name_skip(message: Message, state: FSMContext, sms_client: SmsClient) -> None:
    await _finalize_customer_and_send_otp(message, state, sms_client, full_name=None)


@router.message(CustomerStates.waiting_for_customer_name, F.text, NOT_A_COMMAND)
async def handle_customer_name_text(message: Message, state: FSMContext, sms_client: SmsClient) -> None:
    await _finalize_customer_and_send_otp(message, state, sms_client, full_name=message.text.strip())


async def _finalize_customer_and_send_otp(
    message: Message, state: FSMContext, sms_client: SmsClient, full_name: Optional[str]
) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None:
        await message.answer("Please send /start first.")
        await state.clear()
        return

    data = await state.get_data()
    phone = data.get("customer_phone")
    if not phone:
        await message.answer("Something went wrong — let's start over with /addcustomer.")
        await state.clear()
        return

    customer = await run_db(db.upsert_customer, phone, full_name)
    campaign_id = promoter.get("campaign_id")
    await run_db(db.log_event, promoter["id"], "customer_started", campaign_id=campaign_id, customer_id=customer["id"])
    await run_db(db.log_event, promoter["id"], "customer_phone_captured", campaign_id=campaign_id, customer_id=customer["id"])

    await _issue_and_send_customer_otp(message, state, sms_client, promoter, customer, phone)


async def _issue_and_send_customer_otp(
    message: Message, state: FSMContext, sms_client: SmsClient, promoter: dict, customer: dict, phone: str
) -> None:
    promoter_id = promoter["id"]
    customer_id = customer["id"]
    campaign_id = promoter.get("campaign_id")

    allowed = await run_db(db.can_send_customer_otp, customer_id)
    if not allowed:
        await message.answer(
            "This customer requested a code too recently. Please wait a bit and try again."
        )
        return

    try:
        issue = await run_db(db.create_customer_verification, customer_id, promoter_id, phone)
    except db.RateLimitedError:
        await message.answer(
            "This customer requested a code too recently. Please wait a bit and try again."
        )
        return

    try:
        provider_message_id = await sms_client.send_otp(phone, issue.code, ttl_minutes=db.OTP_TTL_MINUTES)
    except SmsSendError as exc:
        logger.error("Customer SMS delivery failed for customer_id=%s: %s", customer_id, exc)
        await run_db(
            db.log_event, promoter_id, "customer_otp_failed", campaign_id=campaign_id, customer_id=customer_id,
            metadata={"reason": "sms_gateway_error", "detail": str(exc)},
        )
        await message.answer(
            "We couldn't send the code to this customer right now. Please try /addcustomer again shortly."
        )
        return

    await run_db(db.record_customer_provider_message_id, issue.customer_verification_id, provider_message_id)
    await run_db(
        db.record_customer_sms_delivery, issue.customer_verification_id,
        provider="sms_gateway", provider_status="sent",
    )
    await run_db(db.set_customer_status, customer_id, "otp_sent")
    await run_db(db.log_customer_otp_sent, promoter_id, campaign_id, customer_id, issue.customer_verification_id)

    await state.update_data(customer_id=customer_id)
    await state.set_state(CustomerStates.waiting_for_customer_otp)
    await message.answer(
        f"📨 A {db.OTP_LENGTH}-digit code was sent to the customer ({phone}). "
        f"It expires in {db.OTP_TTL_MINUTES} minutes.\n\n"
        "Ask them for the code, then reply with it here, or use /confirmcustomer <code>."
    )


@router.message(Command("confirmcustomer"))
async def cmd_confirm_customer(message: Message, command: CommandObject, state: FSMContext) -> None:
    code = (command.args or "").strip()
    if not code:
        await message.answer("Usage: /confirmcustomer <code> — e.g. /confirmcustomer 048213")
        return
    await _process_customer_otp_attempt(message, state, code)


@router.message(CustomerStates.waiting_for_customer_otp, F.text, NOT_A_COMMAND)
async def handle_customer_otp_text(message: Message, state: FSMContext) -> None:
    await _process_customer_otp_attempt(message, state, message.text.strip())


async def _process_customer_otp_attempt(message: Message, state: FSMContext, code: str) -> None:
    promoter = await run_db(db.get_promoter_by_telegram_id, message.from_user.id)
    if promoter is None:
        await message.answer("Please send /start first.")
        return

    data = await state.get_data()
    customer_id = data.get("customer_id")
    if not customer_id:
        await message.answer("No customer verification in progress. Start one with /addcustomer.")
        return

    if not re.fullmatch(r"\d{4,8}", code):
        await message.answer("Please send just the numeric code, e.g. /confirmcustomer 048213.")
        return

    result = await run_db(db.verify_customer_otp, customer_id, promoter["id"], code)

    if result == db.OtpValidationResult.VERIFIED:
        await state.clear()
        logger.info("CUSTOMER VERIFIED customer_id=%s promoter_id=%s", customer_id, promoter["id"])
        await message.answer(
            "✅ Customer VERIFIED — logged under your account. Send /addcustomer to verify another."
        )

    elif result == db.OtpValidationResult.WRONG_CODE:
        logger.info("CUSTOMER FAILED (wrong_code) customer_id=%s promoter_id=%s", customer_id, promoter["id"])
        await message.answer("❌ That code doesn't match. Please double-check with the customer and try again.")

    elif result == db.OtpValidationResult.EXPIRED:
        logger.info("CUSTOMER FAILED (expired) customer_id=%s promoter_id=%s", customer_id, promoter["id"])
        await state.clear()
        await message.answer("⏰ That code expired. Send /addcustomer to start a fresh verification.")

    elif result == db.OtpValidationResult.MAX_ATTEMPTS:
        logger.info("CUSTOMER FAILED (max_attempts) customer_id=%s promoter_id=%s", customer_id, promoter["id"])
        await run_db(db.set_customer_status, customer_id, "failed")
        await state.clear()
        await message.answer(
            f"🚫 Too many incorrect attempts (max {db.OTP_MAX_ATTEMPTS}). "
            "Send /addcustomer to try again."
        )

    else:  # NOT_FOUND
        await state.clear()
        await message.answer("No active verification code found. Send /addcustomer to start one.")


# ----------------------------------------------------------------------------
# /help
# ----------------------------------------------------------------------------

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/start — begin (use the campaign link you were given)\n"
        "/verify — share or type your phone number to receive a code\n"
        "/confirm <code> — confirm the code you received by SMS\n"
        "/addcustomer — verify a customer's phone number (requires you to be verified first)\n"
        "/confirmcustomer <code> — confirm the code sent to that customer"
    )


# ----------------------------------------------------------------------------
# Admin: promoter roster management
#
# INDOMIE's requirement: adding/removing a promoter should be a single admin
# action with no engineer involvement. These three commands are that action.
# Note: a promoter MUST have a Telegram @username set for /addpromoter to
# work, since Telegram's Bot API has no way to look someone up by phone
# number — only by username, or once they've already messaged the bot.
# ----------------------------------------------------------------------------

@admin_router.message(Command("addpromoter"))
async def cmd_add_promoter(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip().split()
    if not args:
        await message.answer(
            "Usage: /addpromoter <telegram_username> [city] [campaign_slug]\n"
            "e.g. /addpromoter @abebe_kebede Hawassa summer2026"
        )
        return

    username = args[0]
    city = args[1] if len(args) > 1 else None
    campaign_slug = args[2] if len(args) > 2 else None

    campaign_id = None
    if campaign_slug:
        campaign = await run_db(db.get_campaign_by_slug, campaign_slug)
        if campaign is None:
            await message.answer(f'No campaign found with slug "{campaign_slug}". Roster entry not added.')
            return
        campaign_id = campaign["id"]

    entry = await run_db(
        db.add_allowed_promoter,
        telegram_username=username,
        city=city,
        campaign_id=campaign_id,
        added_by_admin_id=message.from_user.id,
    )
    await message.answer(
        f'✅ @{entry["telegram_username"]} is now approved'
        + (f' ({entry["city"]})' if entry.get("city") else "")
        + ".\nThey can now send /start to the bot to begin verification."
    )


@admin_router.message(Command("removepromoter"))
async def cmd_remove_promoter(message: Message, command: CommandObject) -> None:
    username = (command.args or "").strip()
    if not username:
        await message.answer("Usage: /removepromoter <telegram_username>")
        return

    entry = await run_db(db.remove_allowed_promoter, username)
    if entry is None:
        await message.answer(f'No roster entry found for "{username}".')
        return

    await message.answer(
        f'🔒 Access revoked for @{entry["telegram_username"]}. '
        "If they had an active bot session, they can no longer verify or add customers."
    )


@admin_router.message(Command("listpromoters"))
async def cmd_list_promoters(message: Message, command: CommandObject) -> None:
    status_filter = (command.args or "").strip().lower() or None
    if status_filter not in (None, "active", "revoked"):
        await message.answer("Usage: /listpromoters [active|revoked]")
        return

    entries = await run_db(db.list_allowed_promoters, status_filter)
    if not entries:
        await message.answer("No roster entries found.")
        return

    lines = []
    for e in entries:
        marker = "✅" if e["status"] == "active" else "🔒"
        city = f" — {e['city']}" if e.get("city") else ""
        lines.append(f'{marker} @{e["telegram_username"]}{city}')

    await message.answer(f"Promoter roster ({len(entries)}):\n" + "\n".join(lines))


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------

class _SlugAlreadyExists(Exception):
    pass


def _create_campaign(slug: str, name: str) -> dict:
    with db.get_cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO campaigns (name, slug) VALUES (%s, %s) RETURNING id, name, slug, status",
                (name, slug),
            )
        except psycopg2.errors.UniqueViolation as exc:
            raise _SlugAlreadyExists(slug) from exc
        return cur.fetchone()


def _deactivate_campaign(slug: str) -> Optional[dict]:
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE campaigns SET status = 'closed' WHERE slug = %s RETURNING id, name, slug, status",
            (slug,),
        )
        return cur.fetchone()


def _fetch_promoters_for_export(slug: Optional[str]) -> list[dict]:
    base_query = """
        SELECT p.telegram_user_id, p.telegram_username, p.full_name, p.phone_number,
               c.slug AS campaign_slug, c.name AS campaign_name,
               p.status, p.created_at, p.verified_at
        FROM promoters p
        LEFT JOIN campaigns c ON c.id = p.campaign_id
    """
    with db.get_cursor() as cur:
        if slug:
            cur.execute(base_query + " WHERE c.slug = %s ORDER BY p.created_at", (slug,))
        else:
            cur.execute(base_query + " ORDER BY p.created_at")
        return cur.fetchall()


def _build_export_workbook(rows: list) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Verifications"

    headers = [
        "Telegram User ID", "Telegram Username", "Full Name", "Phone Number",
        "Campaign Slug", "Campaign Name", "Status", "Started At (UTC)",
        "Verified At (UTC)", "Time to Verify",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in rows:
        created_at = row["created_at"]
        verified_at = row["verified_at"]
        time_to_verify = str(verified_at - created_at) if verified_at and created_at else ""
        ws.append([
            row["telegram_user_id"],
            row["telegram_username"] or "",
            row["full_name"] or "",
            row["phone_number"] or "",
            row["campaign_slug"] or "",
            row["campaign_name"] or "",
            row["status"],
            created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else "",
            verified_at.strftime("%Y-%m-%d %H:%M:%S") if verified_at else "",
            time_to_verify,
        ])

    for column_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in column_cells)
        ws.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 10), 40)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@admin_router.message(Command("add"))
async def cmd_add_campaign(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args or " " not in args:
        await message.answer("Usage: /add <slug> <campaign name>\ne.g. /add summer2026 Summer 2026 Promo")
        return

    slug, name = args.split(" ", 1)
    slug = slug.strip().lower()
    name = name.strip()

    if not re.fullmatch(r"[a-z0-9\-_]+", slug):
        await message.answer("Slug may only contain lowercase letters, numbers, '-' and '_'.")
        return

    try:
        campaign = await run_db(_create_campaign, slug, name)
    except _SlugAlreadyExists:
        await message.answer(f'A campaign with slug "{slug}" already exists.')
        return

    bot_username = (await message.bot.get_me()).username
    await message.answer(
        f'✅ Campaign created: {campaign["name"]} (slug: {campaign["slug"]})\n'
        f"Share this link: https://t.me/{bot_username}?start={campaign['slug']}"
    )


@admin_router.message(Command("deactivate"))
async def cmd_deactivate_campaign(message: Message, command: CommandObject) -> None:
    slug = (command.args or "").strip().lower()
    if not slug:
        await message.answer("Usage: /deactivate <slug>")
        return

    updated = await run_db(_deactivate_campaign, slug)
    if updated is None:
        await message.answer(f'No campaign found with slug "{slug}".')
        return

    await message.answer(f'🔒 Campaign "{updated["name"]}" ({slug}) is now closed.')


@admin_router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject) -> None:
    slug = (command.args or "").strip().lower() or None
    rows = await run_db(_fetch_promoters_for_export, slug)

    if not rows:
        scope = f'campaign "{slug}"' if slug else "any campaign"
        await message.answer(f"No promoters found for {scope}.")
        return

    workbook_bytes = await asyncio.to_thread(_build_export_workbook, rows)
    filename = f"verification_report_{slug or 'all'}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"

    await message.answer_document(
        document=BufferedInputFile(workbook_bytes, filename=filename),
        caption=f"Verification report — {len(rows)} promoter(s).",
    )


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------

async def main() -> None:
    db.init_pool()
    sms_client = SmsClient()

    if not ADMIN_TELEGRAM_IDS:
        logger.warning("ADMIN_TELEGRAM_IDS is empty — no one can use /add, /deactivate, or /export.")
    logger.info("Starting bot (SMS mock mode: %s)", sms_client.mock_mode)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(admin_router)

    try:
        # sms_client is injected into handlers whose signature declares a
        # `sms_client: SmsClient` parameter (aiogram workflow data).
        await dp.start_polling(bot, sms_client=sms_client)
    finally:
        await sms_client.close()
        db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
