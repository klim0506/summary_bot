import asyncio
import os
from datetime import datetime, timedelta
from typing import List, Optional

from aiogram import Bot
from dotenv import load_dotenv

from database import (
    init_database,
    get_expired_subscriptions,
    deactivate_and_set_free,
    monthly_quota_reset_if_needed,
    get_user_subscription,
    get_user_payment_methods,
    get_last_payment,
    count_payment_attempts_since,
    get_users_stats,
    get_users_delta_since,
    get_operations_stats,
    get_payments_stats,
    get_payment_errors,
    get_all_user_ids,
)
from payments import (
    create_yookassa_recurring_payment,
    finalize_payment,
    check_yookassa_payment,
)

# Настройки
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS_RAW = os.getenv("ADMINS", "")
REPORT_TIME = os.getenv("REPORT_TIME", "22:00")
CONCURRENCY = int(os.getenv("BILLING_CONCURRENCY", "4"))
CONCURRENCY = max(1, min(CONCURRENCY, 10))

PENDING_WAIT_HOURS = 24
RETRY_WINDOW_DAYS = 3

# Периодичность фонового запуска продлений/квот (часы)
RENEW_INTERVAL_HOURS = int(os.getenv("RENEW_INTERVAL_HOURS", "24"))
RENEW_INTERVAL_HOURS = max(1, min(RENEW_INTERVAL_HOURS, 48))


def _parse_admins() -> List[int]:
    ids = []
    for part in ADMINS_RAW.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


ADMINS = _parse_admins()


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        try:
            # SQLite format "YYYY-MM-DD HH:MM:SS"
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


async def send_safe(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        print(f"Failed to send message to {chat_id}: {e}")


async def notify_admins(bot: Bot, text: str):
    for admin_id in ADMINS:
        await send_safe(bot, admin_id, text)


def _select_default_payment_method(user_id: int):
    methods = get_user_payment_methods(user_id)
    if not methods:
        return None
    for m in methods:
        if m.get("is_default"):
            return m
    return methods[0]


async def process_expired_subscriptions(bot: Bot, sem: asyncio.Semaphore):
    now = datetime.now()
    expired = get_expired_subscriptions(now)
    results = {"renewed": 0, "downgraded": 0, "pending_skip": 0, "failed": 0}
    for sub in expired:
        user_id = sub["user_id"]
        plan_type = sub["subscription_type"]

        last_payment = get_last_payment(user_id)
        if last_payment and last_payment["status"] == "pending":
            created = _parse_ts(last_payment["created_at"]) or now
            if now - created < timedelta(hours=PENDING_WAIT_HOURS):
                results["pending_skip"] += 1
                continue

        attempts = count_payment_attempts_since(user_id, now - timedelta(days=RETRY_WINDOW_DAYS))
        if attempts >= 3:
            deactivate_and_set_free(user_id)
            results["downgraded"] += 1
            await send_safe(
                bot,
                user_id,
                "❌ Не удалось продлить подписку после нескольких попыток. "
                "Мы перевели вас на тариф Free. Обновите способ оплаты, чтобы вернуться на подписку.",
            )
            continue

        method = _select_default_payment_method(user_id)
        if not method:
            deactivate_and_set_free(user_id)
            results["downgraded"] += 1
            await send_safe(
                bot,
                user_id,
                "❌ Не найден способ оплаты. Мы перевели вас на тариф Free. "
                "Добавьте карту, чтобы продлить подписку.",
            )
            continue

        async with sem:
            payment_id = await create_yookassa_recurring_payment(
                user_id,
                plan_type,
                method.get("card_number", ""),
                method.get("expiry_date", ""),
                method.get("cvv", ""),
            )
        if not payment_id:
            results["failed"] += 1
            await send_safe(
                bot,
                user_id,
                "❌ Автосписание не прошло. Обновите способ оплаты или попробуем завтра повторить.",
            )
            continue

        status = await finalize_payment(payment_id, user_id, plan_type)
        if status == "succeeded":
            results["renewed"] += 1
            await send_safe(
                bot,
                user_id,
                f"✅ Подписка {plan_type.upper()} продлена на месяц.",
            )
            await notify_admins(bot, f"Оплата успешна: user {user_id}, план {plan_type}, payment {payment_id}")
        elif status in ("pending", "waiting_for_capture"):
            results["pending_skip"] += 1
        else:
            results["failed"] += 1
            await send_safe(
                bot,
                user_id,
                "❌ Оплата не прошла. Проверьте карту или добавьте новый способ оплаты.",
            )
    return results


async def process_monthly_quotas():
    """Сбрасываем квоты раз в месяц для всех планов."""
    users = get_all_user_ids()
    for uid in users:
        sub = get_user_subscription(uid)
        plan = sub["subscription_type"] if sub else "free"
        monthly_quota_reset_if_needed(uid, plan)


def _delta_users_since(date_from: datetime):
    new_total = get_users_delta_since(date_from)
    return new_total


def _plan_delta_since(date_from: datetime):
    # Приближённо считаем по start_date >= date_from
    import sqlite3

    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT subscription_type, COUNT(*)
        FROM subscriptions
        WHERE is_active = 1 AND start_date >= ?
        GROUP BY subscription_type
        """,
        (date_from,),
    )
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def build_daily_report(now: datetime):
    day_start = now - timedelta(days=1)
    users_stats = get_users_stats()
    users_delta = _delta_users_since(day_start)
    plans_delta = _plan_delta_since(day_start)

    ops = get_operations_stats(day_start, now)
    pay = get_payments_stats(day_start, now)
    pay_errors = get_payment_errors(day_start, now)

    total_users = users_stats["total"]
    total_free = users_stats["free"]
    total_basic = users_stats["basic"]
    total_pro = users_stats["pro"]

    free_delta = plans_delta.get("free", 0)
    basic_delta = plans_delta.get("basic", 0)
    pro_delta = plans_delta.get("pro", 0)

    total_ops = ops["total"]
    success_ops = ops["success"]
    fail_ops = ops["fail"]
    fail_rate = f"{(fail_ops/total_ops*100):.1f}%" if total_ops else "0%"

    report_lines = [
        f"ОТЧЕТ_БОТ_САММАРИЗАЦИЯ {now.date()}",
        "",
        "*Количество пользователей:*",
        f"Q - всего: {total_users} (+{users_delta})",
        f"W - free: {total_free} (+{free_delta}) | E - basic: {total_basic} (+{basic_delta}) | R - pro: {total_pro} (+{pro_delta})",
        "",
        "*Генерации:*",
        f"I - всего: {total_ops}",
        f"T - успех: {success_ops} | Y - фейл: {fail_ops} ({fail_rate})",
        "",
        "*Оплаты:*",
        f"Сумма: {pay['amount']} ₽",
        f"Платежи: успех {pay['success']} / фейл {pay['fail']} / pending {pay['pending']}",
        f"Ретраи: 2-я {pay['statuses'].get('retry2', 0)} | 3-я {pay['statuses'].get('retry3', 0)}",
        f"Ошибки YooKassa: {pay_errors}",
    ]
    return "\n".join(report_lines)


def escape_md(text: str) -> str:
    """Минимальное экранирование для Markdown: слэши и подчёркивания."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace("_", "\\_")
    return text


async def send_daily_report(bot: Bot):
    now = datetime.now()
    text = build_daily_report(now)
    safe = escape_md(text)
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, safe, parse_mode="Markdown")
        except Exception as e:
            print(f"Failed to send report to {admin_id}: {e}")


def _next_occurrence(hour: int, minute: int) -> datetime:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _sleep_until(dt: datetime):
    now = datetime.now()
    delay = max(0, (dt - now).total_seconds())
    await asyncio.sleep(delay)


def _parse_report_time(rt: str):
    try:
        parts = rt.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return 22, 0


async def run_report_loop(bot: Bot):
    hour, minute = _parse_report_time(REPORT_TIME)
    while True:
        target = _next_occurrence(hour, minute)
        await _sleep_until(target)
        try:
            await send_daily_report(bot)
        except Exception as e:
            print(f"Report loop error: {e}")


async def run_renew_loop(bot: Bot, sem: asyncio.Semaphore):
    interval = RENEW_INTERVAL_HOURS * 3600
    while True:
        try:
            await process_monthly_quotas()
            await process_expired_subscriptions(bot, sem)
        except Exception as e:
            print(f"Renew loop error: {e}")
        await asyncio.sleep(interval)


async def broadcast(bot: Bot, text: str, photo: Optional[str] = None):
    users = get_all_user_ids()
    for uid in users:
        try:
            if photo:
                await bot.send_photo(uid, photo=photo, caption=text)
            else:
                await bot.send_message(uid, text)
        except Exception as e:
            print(f"Broadcast to {uid} failed: {e}")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Billing service for subscriptions, quotas, reports.")
    parser.add_argument("--task", default="service", choices=["service", "all", "renew", "report", "broadcast", "maintenance"])
    parser.add_argument("--text", help="Text for broadcast/maintenance")
    parser.add_argument("--photo", help="Photo path for broadcast")
    args = parser.parse_args()

    init_database()
    bot = Bot(token=BOT_TOKEN)
    sem = asyncio.Semaphore(CONCURRENCY)

    try:
        if args.task == "service":
            await asyncio.gather(
                run_renew_loop(bot, sem),
                run_report_loop(bot),
            )
            return

        if args.task in ("broadcast", "maintenance"):
            if not args.text:
                print("Text is required for broadcast/maintenance")
                return
            await broadcast(bot, args.text, photo=args.photo)
            return

        if args.task in ("all", "renew"):
            await process_monthly_quotas()
            await process_expired_subscriptions(bot, sem)

        if args.task in ("all", "report"):
            await send_daily_report(bot)
    finally:
        try:
            await bot.session.close()
        except Exception as e:
            print(f"Failed to close bot session: {e}")


if __name__ == "__main__":
    asyncio.run(main())
