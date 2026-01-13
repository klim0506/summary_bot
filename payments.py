import base64
import os
import uuid
from typing import Optional, Tuple

import aiohttp
from dotenv import load_dotenv

from database import _get_plan, create_payment_record, set_subscription, update_payment_status

# #region agent log helper (debug mode)
import json, time
DEBUG_LOG_PATH = r"c:\Users\klims\Desktop\ЯиП\бот док в саммари\.cursor\debug.log"
SESSION_ID = "debug-session"

def _dbg(message: str, data: dict, hypothesis_id: str, location: str, run_id: str = "run-pre"):
    payload = {
        "timestamp": int(time.time() * 1000),
        "sessionId": SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
    }
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion

# Load env explicitly here (bot may import before its own load_dotenv)
load_dotenv()

YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_API_KEY = os.getenv("YOOKASSA_API_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")


def _mask(val: Optional[str], show: int = 3) -> str:
    if not val:
        return "<empty>"
    if len(val) <= show:
        return "*" * len(val)
    return val[:show] + "*" * (len(val) - show)

# Log env load status at module import
_dbg("env_loaded", {
    "shop_id": _mask(YOOKASSA_SHOP_ID),
    "api_key": _mask(YOOKASSA_API_KEY),
    "return_url": YOOKASSA_RETURN_URL,
    "cwd": os.getcwd(),
}, hypothesis_id="H1-env-missing", location="payments.py:module")


def _auth_header() -> str:
    creds = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_API_KEY}"
    _dbg("build_auth_header", {"shop_set": bool(YOOKASSA_SHOP_ID), "key_set": bool(YOOKASSA_API_KEY)}, hypothesis_id="H1-env-missing", location="payments.py:_auth_header")
    return "Basic " + base64.b64encode(creds.encode("utf-8")).decode("utf-8")


async def create_yookassa_payment(user_id: int, plan_type: str) -> Optional[Tuple[str, str]]:
    """
    Создает платеж в ЮКассе и возвращает (payment_id, confirmation_url)
    """
    plan = _get_plan(plan_type)
    if not plan:
        print("YooKassa create: plan not found", plan_type)
        _dbg("plan_not_found", {"plan": plan_type}, hypothesis_id="H2-plan", location="payments.py:create_yookassa_payment")
        return None
    amount_value, _price = None, None
    limit, price = plan
    amount_value = f"{price:.2f}"

    headers = {
        "Authorization": _auth_header(),
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    payload = {
        "amount": {"value": amount_value, "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "capture": True,
        "description": f"Subscription {plan_type} for user {user_id}",
        "metadata": {"user_id": user_id, "plan": plan_type},
    }
    # Добавляем чек (receipt), чтобы не получать invalid_request: receipt
    receipt = {
        "customer": {
            "full_name": f"tg_user_{user_id}",
            "email": "noreply@example.com",
            "phone": "+79000000000",
        },
        "items": [
            {
                "description": f"{plan_type} plan",
                "quantity": "1.00",
                "amount": {"value": amount_value, "currency": "RUB"},
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }
    payload["receipt"] = receipt
    print("YooKassa create request:", {
        "shop_id": _mask(YOOKASSA_SHOP_ID),
        "api_key": _mask(YOOKASSA_API_KEY),
        "plan": plan_type,
        "amount": amount_value,
        "return_url": YOOKASSA_RETURN_URL,
    })
    _dbg("create_request", {
        "shop_id": _mask(YOOKASSA_SHOP_ID),
        "api_key": _mask(YOOKASSA_API_KEY),
        "plan": plan_type,
        "amount": amount_value,
        "return_url": YOOKASSA_RETURN_URL,
        "headers_auth_set": bool(headers.get("Authorization")),
    }, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_payment")
    async with aiohttp.ClientSession() as session:
        async with session.post(YOOKASSA_API_URL, json=payload, headers=headers) as resp:
            data = await resp.json()
            print("YooKassa create status:", resp.status, "response:", data)
            _dbg("create_response", {"status": resp.status, "data": data}, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_payment")
            if resp.status >= 400:
                print("YooKassa create error:", data)
                _dbg("create_error", {"status": resp.status, "data": data}, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_payment")
                return None
            payment_id = data.get("id")
            confirmation = data.get("confirmation", {})
            url = confirmation.get("confirmation_url")
            if payment_id and url:
                create_payment_record(user_id, plan_type, payment_id, float(amount_value))
                _dbg("create_success", {"payment_id": payment_id, "confirm_url": url}, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_payment")
                return payment_id, url
            print("YooKassa create: missing payment_id or confirmation_url", data)
            _dbg("create_missing_confirm", {"data": data}, hypothesis_id="H3-missing-fields", location="payments.py:create_yookassa_payment")
    return None


async def create_yookassa_recurring_payment(user_id: int, plan_type: str, card_number: str, expiry_date: str, cvv: str) -> Optional[str]:
    """
    Пытается списать оплату автоматически по сохранённой карте. Возвращает payment_id при успехе создания.
    """
    plan = _get_plan(plan_type)
    if not plan:
        print("YooKassa recurring: plan not found", plan_type)
        _dbg("plan_not_found", {"plan": plan_type}, hypothesis_id="H2-plan", location="payments.py:create_yookassa_recurring_payment")
        return None
    limit, price = plan
    amount_value = f"{price:.2f}"

    # Парсим срок действия карты (ожидаем MM/YY или MM/YYYY)
    exp_month = ""
    exp_year = ""
    try:
        parts = expiry_date.replace(" ", "").replace("-", "/").split("/")
        if len(parts) >= 2:
            exp_month = parts[0]
            exp_year = parts[1]
            if len(exp_year) == 2:
                exp_year = "20" + exp_year
    except Exception:
        pass

    headers = {
        "Authorization": _auth_header(),
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    payload = {
        "amount": {"value": amount_value, "currency": "RUB"},
        "capture": True,
        "description": f"Subscription {plan_type} auto-renew for user {user_id}",
        "metadata": {"user_id": user_id, "plan": plan_type, "auto": True},
        "payment_method_data": {
            "type": "bank_card",
            "card": {
                "number": card_number,
                "expiry_month": exp_month,
                "expiry_year": exp_year,
            },
            "csc": cvv,
        },
    }
    _dbg("recurring_request", {
        "plan": plan_type,
        "amount": amount_value,
        "has_card": bool(card_number),
        "expiry": expiry_date,
    }, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_recurring_payment")

    async with aiohttp.ClientSession() as session:
        async with session.post(YOOKASSA_API_URL, json=payload, headers=headers) as resp:
            data = await resp.json()
            print("YooKassa recurring status:", resp.status, "response:", data)
            _dbg("recurring_response", {"status": resp.status, "data": data}, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_recurring_payment")
            if resp.status >= 400:
                _dbg("recurring_error", {"status": resp.status, "data": data}, hypothesis_id="H1-env-missing", location="payments.py:create_yookassa_recurring_payment")
                return None
            payment_id = data.get("id")
            if payment_id:
                create_payment_record(user_id, plan_type, payment_id, float(amount_value))
                return payment_id
    return None


async def check_yookassa_payment(payment_id: str) -> Optional[str]:
    """
    Возвращает статус платежа или None при ошибке.
    """
    headers = {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
    }
    url = f"{YOOKASSA_API_URL}/{payment_id}"
    print("YooKassa check request:", {"payment_id": payment_id})
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            print("YooKassa check status:", resp.status, "response:", data)
            if resp.status >= 400:
                print("YooKassa check error:", data)
                return None
            return data.get("status")


async def finalize_payment(payment_id: str, user_id: int, plan_type: str) -> Optional[str]:
    """
    Проверяет платеж и при успехе активирует подписку.
    Возвращает статус платежа.
    """
    print("YooKassa finalize:", {"payment_id": payment_id, "user_id": user_id, "plan": plan_type})
    status = await check_yookassa_payment(payment_id)
    if not status:
        print("YooKassa finalize: status is None")
        return None
    update_payment_status(payment_id, status)
    if status == "succeeded":
        set_subscription(user_id, plan_type)
    return status
