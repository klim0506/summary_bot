import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

# Настройка адаптера datetime -> ISO (устраняет DeprecationWarning SQLite 3.12+)
sqlite3.register_adapter(datetime, lambda val: val.isoformat())

# Путь к базе данных
DB_PATH = "bot_database.db"

# Названия планов
PLAN_FREE = "free"
PLAN_BASIC = "basic"
PLAN_PRO = "pro"


def init_database():
    """Инициализирует базу данных и создает необходимые таблицы"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица операций (генераций)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            file_name TEXT,
            file_type TEXT,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # Таблица документов для одноразового Q&A
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_name TEXT,
            file_type TEXT,
            content TEXT,
            question_used BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at)")
    
    # Таблица подписок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            subscription_type TEXT DEFAULT 'free',
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_date TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            auto_renewal BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # Таблица тарифов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscription_plans (
            plan_type TEXT PRIMARY KEY,
            monthly_limit INTEGER NOT NULL,
            price_rub INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица балансов генераций
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_quotas (
            user_id INTEGER PRIMARY KEY,
            plan_type TEXT NOT NULL,
            remaining INTEGER NOT NULL,
            last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # Таблица банковских реквизитов для автопродления
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_number TEXT,
            card_holder_name TEXT,
            expiry_date TEXT,
            cvv TEXT,
            bank_name TEXT,
            is_default BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # Таблица платежей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subscription_id INTEGER,
            amount DECIMAL(10, 2),
            currency TEXT DEFAULT 'RUB',
            payment_method_id INTEGER,
            status TEXT DEFAULT 'pending',
            transaction_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id),
            FOREIGN KEY (payment_method_id) REFERENCES payment_methods(id)
        )
    """)

    # Таблица рефералок (кто пригласил и бонусы)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            invitee_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            code TEXT,
            opened_bonus_given BOOLEAN DEFAULT 0,
            first_generation_bonus_given BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invitee_id) REFERENCES users(user_id),
            FOREIGN KEY (referrer_id) REFERENCES users(user_id)
        )
    """)

    # Добавляем недостающий столбец updated_at в payments (если таблица уже была)
    cursor.execute("PRAGMA table_info(payments)")
    cols = [row[1] for row in cursor.fetchall()]
    if "updated_at" not in cols:
        try:
            # SQLite не позволяет ADD COLUMN с функцией в DEFAULT, добавляем без DEFAULT
            cursor.execute("ALTER TABLE payments ADD COLUMN updated_at TIMESTAMP")
            cursor.execute("UPDATE payments SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")
        except Exception as e:
            print("Failed to add updated_at to payments:", e)

    # Добавляем недостающий столбец plan_type в payments
    cursor.execute("PRAGMA table_info(payments)")
    cols = [row[1] for row in cursor.fetchall()]
    if "plan_type" not in cols:
        try:
            cursor.execute("ALTER TABLE payments ADD COLUMN plan_type TEXT")
        except Exception as e:
            print("Failed to add plan_type to payments:", e)
    
    # Заполняем/обновляем планы
    cursor.execute("""
        INSERT INTO subscription_plans (plan_type, monthly_limit, price_rub)
        VALUES (?, ?, ?)
        ON CONFLICT(plan_type) DO UPDATE SET monthly_limit=excluded.monthly_limit,
            price_rub=excluded.price_rub,
            updated_at=CURRENT_TIMESTAMP
    """, (PLAN_FREE, 5, 0))
    cursor.execute("""
        INSERT INTO subscription_plans (plan_type, monthly_limit, price_rub)
        VALUES (?, ?, ?)
        ON CONFLICT(plan_type) DO UPDATE SET monthly_limit=excluded.monthly_limit,
            price_rub=excluded.price_rub,
            updated_at=CURRENT_TIMESTAMP
    """, (PLAN_BASIC, 50, 269))
    cursor.execute("""
        INSERT INTO subscription_plans (plan_type, monthly_limit, price_rub)
        VALUES (?, ?, ?)
        ON CONFLICT(plan_type) DO UPDATE SET monthly_limit=excluded.monthly_limit,
            price_rub=excluded.price_rub,
            updated_at=CURRENT_TIMESTAMP
    """, (PLAN_PRO, 200, 529))

    conn.commit()
    conn.close()


def get_or_create_user(user_id: int, username: Optional[str] = None, 
                      first_name: Optional[str] = None, 
                      last_name: Optional[str] = None) -> Dict:
    """Получает пользователя из БД или создает нового"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Проверяем, существует ли пользователь
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    
    if user:
        # Обновляем информацию о пользователе
        cursor.execute("""
            UPDATE users 
            SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (username, first_name, last_name, user_id))
        conn.commit()
        conn.close()
        return {
            'user_id': user[0],
            'username': username or user[1],
            'first_name': first_name or user[2],
            'last_name': last_name or user[3]
        }
    else:
        # Создаем нового пользователя
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, first_name, last_name))
        
        # Создаем бесплатную подписку
        cursor.execute("""
            INSERT INTO subscriptions (user_id, subscription_type, is_active)
            VALUES (?, 'free', 1)
        """, (user_id,))

        # Создаем запись баланса по free плану
        cursor.execute("""
            INSERT INTO user_quotas (user_id, plan_type, remaining, last_reset)
            SELECT ?, ?, monthly_limit, CURRENT_TIMESTAMP
            FROM subscription_plans WHERE plan_type = ?
        """, (user_id, PLAN_FREE, PLAN_FREE))
        
        conn.commit()
        conn.close()
        return {
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name
        }


def log_operation(user_id: int, operation_type: str, file_name: Optional[str] = None, 
                  file_type: Optional[str] = None, status: str = 'completed'):
    """Логирует операцию в базу данных"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO operations (user_id, operation_type, file_name, file_type, status)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, operation_type, file_name, file_type, status))
    
    conn.commit()
    conn.close()


def delete_user_documents(user_id: int):
    """Удаляет все документы пользователя (используется перед сохранением нового)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM documents WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def cleanup_expired_documents(days: int = 30):
    """Удаляет документы, по которым не задавали вопрос дольше days."""
    threshold = datetime.now() - timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM documents
        WHERE question_used = 0 AND created_at < ?
    """, (threshold.isoformat(),))
    conn.commit()
    conn.close()


def save_document_content(user_id: int, file_name: str, file_type: str, content: str) -> int:
    """
    Сохраняет текст документа для последующего одноразового Q&A.
    Перед сохранением очищает предыдущие документы пользователя, чтобы не было путаницы.
    """
    delete_user_documents(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO documents (user_id, file_name, file_type, content, question_used, created_at, last_accessed)
        VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """, (user_id, file_name, file_type, content))
    doc_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def get_pending_document(user_id: int) -> Optional[Dict]:
    """Возвращает последний документ пользователя, по которому ещё не задавали вопрос."""
    cleanup_expired_documents()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, file_name, file_type, content, created_at, last_accessed
        FROM documents
        WHERE user_id = ? AND question_used = 0
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "file_name": row[1],
        "file_type": row[2],
        "content": row[3],
        "created_at": row[4],
        "last_accessed": row[5],
    }


def get_document_by_id(doc_id: int) -> Optional[Dict]:
    """Возвращает документ по id (если ещё не удалён)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, file_name, file_type, content, created_at, last_accessed
        FROM documents
        WHERE id = ?
    """, (doc_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "file_name": row[2],
        "file_type": row[3],
        "content": row[4],
        "created_at": row[5],
        "last_accessed": row[6],
    }


def mark_document_answered(doc_id: int):
    """Помечает документ как использованный и удаляет содержимое."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()


def get_user_subscription(user_id: int) -> Optional[Dict]:
    """Получает информацию о подписке пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, subscription_type, start_date, end_date, is_active, auto_renewal
        FROM subscriptions
        WHERE user_id = ? AND is_active = 1
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))
    
    subscription = cursor.fetchone()
    conn.close()
    
    if subscription:
        return {
            'id': subscription[0],
            'subscription_type': subscription[1],
            'start_date': subscription[2],
            'end_date': subscription[3],
            'is_active': subscription[4],
            'auto_renewal': subscription[5]
        }
    return None


def _get_plan(plan_type: str) -> Optional[Tuple[int, int]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT monthly_limit, price_rub FROM subscription_plans WHERE plan_type = ?
    """, (plan_type,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None


def _reset_quota_if_needed(user_id: int, plan_type: str):
    plan = _get_plan(plan_type)
    if not plan:
        return
    limit, _ = plan
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT remaining, last_reset FROM user_quotas WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    now = datetime.now()
    if not row:
        cursor.execute("""
            INSERT INTO user_quotas (user_id, plan_type, remaining, last_reset)
            VALUES (?, ?, ?, ?)
        """, (user_id, plan_type, limit, now))
        conn.commit()
        conn.close()
        return

    remaining, last_reset_str = row
    last_reset = datetime.fromisoformat(last_reset_str) if last_reset_str else now
    if (now - last_reset) >= timedelta(days=30) or remaining < 0:
        cursor.execute("""
            UPDATE user_quotas
            SET remaining = ?, last_reset = ?, plan_type = ?
            WHERE user_id = ?
        """, (limit, now, plan_type, user_id))
    conn.commit()
    conn.close()


def check_operations_quota(user_id: int) -> Tuple[bool, int, str]:
    """
    Проверяет баланс генераций. Авто-ресет раз в 30 дней для текущего плана.
    Returns: (can_operate, remaining, plan_type)
    """
    sub = get_user_subscription(user_id)
    plan_type = sub['subscription_type'] if sub else PLAN_FREE
    _reset_quota_if_needed(user_id, plan_type)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT remaining FROM user_quotas WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()

    remaining = row[0] if row else 0
    return remaining > 0, remaining, plan_type


def consume_generation(user_id: int) -> bool:
    """Списывает одну генерацию, если хватает баланса."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT remaining FROM user_quotas WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or row[0] <= 0:
        conn.close()
        return False
    cursor.execute("""
        UPDATE user_quotas SET remaining = remaining - 1 WHERE user_id = ?
    """, (user_id,))
    conn.commit()
    conn.close()
    return True


def reset_user_quota(user_id: int) -> int:
    """Полностью восстанавливает квоту согласно текущему плану."""
    sub = get_user_subscription(user_id)
    plan_type = sub['subscription_type'] if sub else PLAN_FREE
    plan = _get_plan(plan_type)
    if not plan:
        plan_type = PLAN_FREE
        plan = _get_plan(plan_type)
    limit = plan[0] if plan else 0
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_quotas (user_id, plan_type, remaining, last_reset)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            plan_type=excluded.plan_type,
            remaining=excluded.remaining,
            last_reset=excluded.last_reset
    """, (user_id, plan_type, limit, now))
    conn.commit()
    conn.close()
    return limit


def set_subscription(user_id: int, plan_type: str, months: int = 1):
    """Активирует подписку и выставляет end_date +months."""
    now = datetime.now()
    end_date = now + timedelta(days=30 * months)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO subscriptions (user_id, subscription_type, start_date, end_date, is_active, auto_renewal)
        VALUES (?, ?, ?, ?, 1, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            subscription_type=excluded.subscription_type,
            start_date=excluded.start_date,
            end_date=excluded.end_date,
            is_active=1,
            updated_at=CURRENT_TIMESTAMP
    """, (user_id, plan_type, now, end_date))
    conn.commit()
    conn.close()
    # Обновляем квоту под новый план
    reset_user_quota(user_id)


def set_subscription_calendar_month(user_id: int, plan_type: str, from_date: Optional[datetime] = None):
    """Активирует подписку на +1 календарный месяц, стартуя от from_date или сейчас."""
    base_date = from_date or datetime.now()
    try:
        from dateutil.relativedelta import relativedelta
    except Exception:
        # fallback: 30 дней
        relativedelta = None
    if relativedelta:
        end_date = base_date + relativedelta(months=1)
    else:
        end_date = base_date + timedelta(days=30)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO subscriptions (user_id, subscription_type, start_date, end_date, is_active, auto_renewal)
        VALUES (?, ?, ?, ?, 1, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            subscription_type=excluded.subscription_type,
            start_date=excluded.start_date,
            end_date=excluded.end_date,
            is_active=1,
            updated_at=CURRENT_TIMESTAMP
    """, (user_id, plan_type, base_date, end_date))
    conn.commit()
    conn.close()
    reset_user_quota(user_id)


def create_payment_record(user_id: int, plan_type: str, payment_id: str, amount: float, currency: str = "RUB"):
    """Создаёт запись платежа со статусом pending."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payments (user_id, subscription_id, amount, currency, payment_method_id, status, transaction_id, updated_at, plan_type)
        VALUES (?, NULL, ?, ?, NULL, 'pending', ?, CURRENT_TIMESTAMP, ?)
    """, (user_id, amount, currency, payment_id, plan_type))
    conn.commit()
    conn.close()


def update_payment_status(payment_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE payments SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE transaction_id = ?
    """, (status, payment_id))
    conn.commit()
    conn.close()


def get_last_payment(user_id: int):
    """Возвращает последнюю запись платежа пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT transaction_id, status, created_at, plan_type
        FROM payments
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "transaction_id": row[0],
        "status": row[1],
        "created_at": row[2],
        "plan_type": row[3],
    }


def add_payment_method(user_id: int, card_number: str, card_holder_name: str,
                      expiry_date: str, cvv: str, bank_name: Optional[str] = None):
    """Добавляет банковские реквизиты для автопродления"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Если это первая карта, делаем её основной
    cursor.execute("SELECT COUNT(*) FROM payment_methods WHERE user_id = ?", (user_id,))
    is_first = cursor.fetchone()[0] == 0
    
    cursor.execute("""
        INSERT INTO payment_methods 
        (user_id, card_number, card_holder_name, expiry_date, cvv, bank_name, is_default)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, card_number, card_holder_name, expiry_date, cvv, bank_name, is_first))
    
    conn.commit()
    conn.close()


def get_user_payment_methods(user_id: int) -> List[Dict]:
    """Получает список банковских реквизитов пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, card_number, card_holder_name, expiry_date, bank_name, is_default
        FROM payment_methods
        WHERE user_id = ?
        ORDER BY is_default DESC, created_at DESC
    """, (user_id,))
    
    methods = cursor.fetchall()
    conn.close()
    
    return [
        {
            'id': m[0],
            'card_number': m[1],
            'card_holder_name': m[2],
            'expiry_date': m[3],
            'bank_name': m[4],
            'is_default': m[5]
        }
        for m in methods
    ]


def reset_user_operations(user_id: int) -> int:
    """
    Обнуляет все операции пользователя (удаляет их из базы данных)
    
    Args:
        user_id: ID пользователя
    
    Returns:
        Количество удаленных операций
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Получаем количество операций до удаления
    cursor.execute("SELECT COUNT(*) FROM operations WHERE user_id = ?", (user_id,))
    count_before = cursor.fetchone()[0]
    
    # Удаляем все операции пользователя
    cursor.execute("DELETE FROM operations WHERE user_id = ?", (user_id,))
    
    conn.commit()
    conn.close()
    
    return count_before


# ====== Дополнительные функции для сервисов ======

def get_expired_subscriptions(now: Optional[datetime] = None):
    """Возвращает активные подписки, у которых end_date < now."""
    now = now or datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, subscription_type, start_date, end_date
        FROM subscriptions
        WHERE is_active = 1 AND end_date IS NOT NULL AND end_date < ?
    """, (now,))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "user_id": r[0],
            "subscription_type": r[1],
            "start_date": r[2],
            "end_date": r[3],
        }
        for r in rows
    ]


def deactivate_and_set_free(user_id: int):
    """Деактивирует текущую подписку и переводит на free, сбрасывая квоту."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute("""
        UPDATE subscriptions
        SET subscription_type = ?, start_date = ?, end_date = NULL, is_active = 1, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (PLAN_FREE, now, user_id))
    conn.commit()
    conn.close()
    reset_user_quota(user_id)


def monthly_quota_reset_if_needed(user_id: int, plan_type: str):
    """
    Сбрасывает квоту, если прошёл месяц от last_reset (календарно, relativedelta).
    """
    try:
        from dateutil.relativedelta import relativedelta
    except Exception:
        relativedelta = None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT remaining, last_reset FROM user_quotas WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    now = datetime.now()
    if not row:
        conn.close()
        reset_user_quota(user_id)
        return
    remaining, last_reset_str = row
    last_reset = datetime.fromisoformat(last_reset_str) if last_reset_str else now
    if relativedelta:
        need_reset = now >= (last_reset + relativedelta(months=1))
    else:
        need_reset = (now - last_reset) >= timedelta(days=30)
    if need_reset or remaining < 0:
        conn.close()
        reset_user_quota(user_id)
    else:
        conn.close()


def get_users_stats():
    """Возвращает общее число пользователей и разбивку по планам."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    cursor.execute("""
        SELECT subscription_type, COUNT(*) 
        FROM subscriptions 
        WHERE is_active = 1
        GROUP BY subscription_type
    """)
    rows = cursor.fetchall()
    conn.close()
    plans = {r[0]: r[1] for r in rows}
    return {
        "total": total,
        "free": plans.get(PLAN_FREE, 0),
        "basic": plans.get(PLAN_BASIC, 0),
        "pro": plans.get(PLAN_PRO, 0),
    }


def get_users_delta_since(date_from: datetime):
    """Возвращает число новых пользователей с даты."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (date_from,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_operations_stats(date_from: datetime, date_to: datetime):
    """Статистика операций за период."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as success,
            SUM(CASE WHEN status != 'completed' THEN 1 ELSE 0 END) as fail
        FROM operations
        WHERE created_at >= ? AND created_at < ?
    """, (date_from, date_to))
    row = cursor.fetchone()
    conn.close()
    return {
        "total": row[0] or 0,
        "success": row[1] or 0,
        "fail": row[2] or 0,
    }


def get_payments_stats(date_from: datetime, date_to: datetime):
    """Статистика платежей за период: успех/фейл/pending и сумма успехов."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS success_cnt,
            SUM(CASE WHEN status NOT IN ('succeeded', 'pending') THEN 1 ELSE 0 END) AS fail_cnt,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_cnt,
            SUM(CASE WHEN status = 'succeeded' THEN amount ELSE 0 END) AS amount_sum
        FROM payments
        WHERE created_at >= ? AND created_at < ?
    """, (date_from, date_to))
    row = cursor.fetchone()
    # Ретраи по попыткам
    cursor.execute("""
        SELECT status, COUNT(*) 
        FROM payments
        WHERE created_at >= ? AND created_at < ?
        GROUP BY status
    """, (date_from, date_to))
    statuses = {r[0]: r[1] for r in cursor.fetchall()}
    conn.close()
    return {
        "success": row[0] or 0,
        "fail": row[1] or 0,
        "pending": row[2] or 0,
        "amount": row[3] or 0.0,
        "statuses": statuses,
    }


def get_payment_errors(date_from: datetime, date_to: datetime):
    """Псевдо-агрегат ошибок: берём статус != succeeded/pending."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT status, COUNT(*)
        FROM payments
        WHERE created_at >= ? AND created_at < ? AND status NOT IN ('succeeded', 'pending')
        GROUP BY status
    """, (date_from, date_to))
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def count_payment_attempts_since(user_id: int, date_from: datetime):
    """Количество платежных попыток пользователя с даты (любой статус)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM payments
        WHERE user_id = ? AND created_at >= ?
    """, (user_id, date_from))
    count = cursor.fetchone()[0]
    conn.close()
    return count or 0


def get_all_user_ids():
    """Список всех user_id для рассылок."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_bonus_quota(user_id: int, bonus: int) -> int:
    """
    Начисляет bonus к оставшимся генерациям пользователя.
    Возвращает новое значение remaining.
    """
    if bonus <= 0:
        return 0

    # Убедимся, что запись квоты существует и актуальна
    sub = get_user_subscription(user_id)
    plan_type = sub['subscription_type'] if sub else PLAN_FREE
    _reset_quota_if_needed(user_id, plan_type)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT remaining FROM user_quotas WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    current = row[0] if row else 0
    new_value = current + bonus
    cursor.execute("""
        INSERT INTO user_quotas (user_id, plan_type, remaining, last_reset)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET remaining = ?, last_reset = CURRENT_TIMESTAMP
    """, (user_id, plan_type, new_value, new_value))
    conn.commit()
    conn.close()
    return new_value


def register_referral_start(invitee_id: int, referrer_id: int, code: str | None = None) -> bool:
    """
    Регистрирует факт захода по реферальной ссылке.
    Возвращает True, если запись новая и бонус можно начислять.
    """
    if invitee_id == referrer_id:
        return False

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT opened_bonus_given FROM referrals WHERE invitee_id = ?", (invitee_id,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return False

    cursor.execute("""
        INSERT INTO referrals (invitee_id, referrer_id, code, opened_bonus_given, first_generation_bonus_given)
        VALUES (?, ?, ?, 1, 0)
    """, (invitee_id, referrer_id, code))
    conn.commit()
    conn.close()
    return True


def mark_referral_first_generation(invitee_id: int) -> int | None:
    """
    Помечает первую генерацию приглашённого. Возвращает referrer_id, если бонус нужно начислить.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT referrer_id, first_generation_bonus_given
        FROM referrals
        WHERE invitee_id = ?
    """, (invitee_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    referrer_id, already_given = row
    if already_given:
        conn.close()
        return None

    cursor.execute("""
        UPDATE referrals SET first_generation_bonus_given = 1 WHERE invitee_id = ?
    """, (invitee_id,))
    conn.commit()
    conn.close()
    return referrer_id

