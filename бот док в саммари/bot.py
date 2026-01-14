import asyncio
import json
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Клавиатура покупки/расшаривания при исчерпании квоты
def build_subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=SUBSCRIBE_TEXT_BASIC, callback_data="sub_basic"),
            ],
            [
                InlineKeyboardButton(text=SUBSCRIBE_TEXT_PRO, callback_data="sub_pro"),
            ],
            [
                InlineKeyboardButton(text=SUBSCRIBE_TEXT_SHARE, callback_data="share"),
            ],
        ]
    )

import os
from dotenv import load_dotenv
import re
from openai import AsyncOpenAI
import io
from prompts import (
    SUMMARY_PROMPT,
    CHUNK_SUMMARY_PROMPT,
    QUESTION_PROMPT,
    SUBSCRIBE_TEXT_BASIC,
    SUBSCRIBE_TEXT_PRO,
    SUBSCRIBE_TEXT_SHARE,
    MSG_FILE_VIDEO,
    MSG_FILE_AUDIO,
    MSG_FILE_LINK,
    MSG_FILE_PROMPT,
    MSG_FILE_UNKNOWN,
    MSG_START,
    MSG_SECRET_OK,
    MSG_UNSUPPORTED_FORMAT,
    MSG_PROCESSING_START,
    MSG_EXTRACT_FAIL,
    MSG_EXTRACT_FAIL_SCAN,
    MSG_FILE_TOO_LARGE,
    MSG_QUOTA_EXCEEDED,
    MSG_SUMMARY_START,
    MSG_SUMMARY_START_NOPREVIEW,
    MSG_LINK_PROCESSING_START,
    MSG_LINK_EXTRACT_FAIL,
    MSG_LINK_SUMMARY_START,
    MSG_LINK_SUMMARY_START_NOPREVIEW,
    MSG_LINK_RESULT,
    MSG_CHUNK_FAIL,
    MSG_RESULT,
    MSG_ERROR,
    MSG_SUBSCRIBE_CREATED,
    MSG_SUBSCRIBE_FAILED,
    MSG_PAYMENT_BAD_DATA,
    MSG_PAYMENT_CHECK_FAIL,
    MSG_PAYMENT_SUCCESS,
    MSG_PAYMENT_PENDING,
    MSG_PAYMENT_OTHER_STATUS,
    MSG_SHARE_PROMPT,
    MSG_REF_AUTHOR_OPENED,
    MSG_REF_AUTHOR_USED,
)
import base64
import tempfile
from database import (
    init_database, get_or_create_user, log_operation,
    check_operations_quota, consume_generation, reset_user_quota,
    register_referral_start, mark_referral_first_generation,
    add_bonus_quota,
    save_document_content,
    get_pending_document,
    get_document_by_id,
    mark_document_answered,
)
from text_extractors import extract_text_from_file, extract_text_from_url
from payments import create_yookassa_payment, finalize_payment

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bot")

# Получаем токен бота из переменной окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
BOT_USERNAME = None
# Подпись с актуальным хэндлом бота для итоговых сообщений (экранируем _ для Markdown)
SUMMARY_SIGNATURE = "Суммаризировано @fast/_summary/_bot"
# Ограничение: один вопрос по документу в течение жизни сохранённого текста
QUESTION_HINT = (
    "Можно задать 1 вопрос по этому документу. Просто отправь вопрос одним сообщением, "
    "я отвечу, опираясь на текст файла. После ответа текст удалю. "
    "Если вопрос не задашь, текст удалится через 30 дней."
)
# Временное состояние: пользователь ждёт ответ по документу (user_id -> doc_id)
question_sessions: dict[int, int] = {}

# Инициализируем асинхронный клиент DeepSeek
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Инициализируем бота и диспетчер
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def log_event(event: str, **data):
    """Структурированное событие для дальнейшей аналитики."""
    try:
        payload = {"event": event, **data}
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to log event")


async def download_file_bytes(file_id: str, max_attempts: int = 3, base_delay: int = 2, timeout: int = 120) -> bytes:
    """
    Скачивает файл с Telegram с повторами при сетевых ошибках.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            tg_file = await bot.get_file(file_id)
            file_content = await bot.download_file(tg_file.file_path, timeout=timeout)
            return file_content.read()
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                await asyncio.sleep(base_delay * attempt)
            else:
                logger.exception("download_file_bytes failed after retries")
                raise last_error

def get_file_type(filename: str) -> str:
    """Определяет тип файла по расширению"""
    if not filename:
        return "Неизвестный тип"
    
    extension = filename.lower().split('.')[-1] if '.' in filename else ''
    
    file_types = {
        'pdf': 'PDF',
        'doc': 'Word документ',
        'docx': 'Word документ',
        'pptx': 'PowerPoint презентация',
        'ppt': 'PowerPoint презентация',
        'xlsx': 'Excel таблица',
        'xls': 'Excel таблица',
        'zip': 'Архив',
        'py': 'Исходный код (Python)',
        'js': 'Исходный код (JavaScript)',
        'ts': 'Исходный код (TypeScript)',
        'tsx': 'Исходный код (TypeScript/JSX)',
        'c': 'Исходный код (C)',
        'cpp': 'Исходный код (C++)',
        'h': 'Исходный код (C/C++ header)',
        'hpp': 'Исходный код (C++ header)',
        'java': 'Исходный код (Java)',
        'go': 'Исходный код (Go)',
        'jpg': 'Изображение',
        'jpeg': 'Изображение',
        'png': 'Изображение',
        'gif': 'Изображение',
        'webp': 'Изображение',
        'bmp': 'Изображение'
    }
    return file_types.get(extension, f"Неизвестный тип файла ({extension})")


def is_url(text: str) -> bool:
    """Проверяет, является ли текст URL"""
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(url_pattern.match(text.strip()))


def encode_ref_code(user_id: int) -> str:
    """Кодирует user_id в компактный параметр start."""
    raw = str(user_id).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_ref_code(code: str) -> int | None:
    """Декодирует параметр start в user_id или None."""
    if not code:
        return None
    try:
        padding = "=" * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(code + padding).decode()
        return int(decoded)
    except Exception:
        return None


def format_markdown_text(text: str) -> str:
    """
    Убирает простейшее Markdown-форматирование, чтобы отправлять plain text
    
    Args:
        text: Исходный текст
    
    Returns:
        Очищенный текст без маркдаун-разметки
    """
    import re
    # убираем жир/курсив вида **text** или __text__
    cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    cleaned = re.sub(r'__(.*?)__', r'\1', cleaned)
    # заменяем маркдаун-маркеры списка "* " или "- " на простой дефис
    cleaned = re.sub(r'^\s*\*\s+', '- ', cleaned, flags=re.MULTILINE)
    # убираем одиночные звёздочки вокруг слов
    cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
    return cleaned


def _cleanup_json_text(text: str) -> str:
    """Убирает возможные обертки ```json ... ``` и лишние пробелы."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def parse_summary_json(raw_text: str) -> dict:
    """
    Пытается распарсить JSON-ответ финального саммари.
    Возвращает словарь или пустой dict при ошибке.
    """
    try:
        cleaned = _cleanup_json_text(raw_text)
        return json.loads(cleaned)
    except Exception as e:
        logger.warning("Failed to parse summary JSON: %s", e)
        return {}


def escape_markdown(text: str) -> str:
    """Экранирует спецсимволы для Markdown."""
    if not text:
        return ""
    for ch in ("\\", "`", "*", "_", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_read_time(words: int, wpm: int = 180) -> str:
    """Формирует строку времени чтения при скорости wpm слов в минуту."""
    if words <= 0:
        return "меньше 1 сек"
    total_seconds = int(words / wpm * 60)
    if total_seconds < 60:
        return f"{total_seconds} сек"
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    if minutes >= 60:
        hours = minutes // 60
        minutes = minutes % 60
        if minutes == 0:
            return f"{hours} ч"
        return f"{hours} ч {minutes} мин"
    if seconds == 0:
        return f"{minutes} мин"
    return f"{minutes} мин {seconds} сек"


# Семафор для ограничения количества одновременных запросов к API
# Разрешаем до 3 одновременных запросов (снижено для стабильности)
# Инициализируем как None, создадим в main()
api_semaphore = None


async def make_api_request(messages: list, system_prompt: str, temperature: float = 0.7, max_retries: int = 5) -> str:
    """
    Универсальная функция для выполнения API запросов с семафором и retry
    
    Args:
        messages: Список сообщений для API
        system_prompt: Системный промпт
        temperature: Температура для генерации
        max_retries: Максимальное количество попыток
    
    Returns:
        Ответ от API
    """
    global api_semaphore
    if api_semaphore is None:
        api_semaphore = asyncio.Semaphore(3)
    
    async with api_semaphore:
        for attempt in range(max_retries):
            try:
                response = await deepseek_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {
                            "role": "system",
                            "content": system_prompt
                        }
                    ] + messages,
                    temperature=temperature
                )
                
                return response.choices[0].message.content
            except Exception as e:
                if attempt < max_retries - 1:
                    # Ждем перед повторной попыткой (экспоненциальная задержка)
                    wait_time = 2 ** attempt
                    await asyncio.sleep(wait_time)
                    logger.warning("API request failed attempt %s/%s: %s", attempt + 1, max_retries, e)
                else:
                    logger.exception("API request failed after %s attempts", max_retries)
                    raise


async def summarize_text_chunk(text: str, context: str = "") -> str:
    """
    Саммаризирует один чанк текста - максимальное сокращение с одной цитатой
    
    Args:
        text: Текст для саммаризации
        context: Дополнительный контекст (опционально)
    
    Returns:
        Сокращенный текст с одной цитатой
    """
    user_message = f"Максимально сократи этот текст и оставь одну прямую цитату:\n\n{text}"
    logger.debug("Summarize chunk preview: %s", ' '.join(text[:30].split('\n')))
    return await make_api_request(
        messages=[{"role": "user", "content": user_message}],
        system_prompt=CHUNK_SUMMARY_PROMPT,
        temperature=0.3
    )


async def answer_question_with_document(document_text: str, question: str) -> str:
    """
    Отвечает на один вопрос пользователя, опираясь на текст документа.
    """
    user_message = (
        "Ответь на вопрос по документу, опираясь только на текст. "
        "Если ответа нет в документе, скажи об этом.\n\n"
        f"Вопрос: {question}\n\n"
        f"Текст документа:\n{document_text}"
    )
    return await make_api_request(
        messages=[{"role": "user", "content": user_message}],
        system_prompt=QUESTION_PROMPT,
        temperature=0.2
    )


def create_sliding_window_chunks(text: str, chunk_size: int, overlap_size: int) -> list:
    """
    Создает перекрывающиеся чанки текста (скользящее окно)
    Оптимизировано для случая без перекрытия (overlap_size = 0)
    
    Args:
        text: Исходный текст
        chunk_size: Размер чанка в байтах
        overlap_size: Размер перекрытия в байтах
    
    Returns:
        Список чанков
    """
    chunks = []
    lines = text.split('\n')
    
    if not lines:
        return []
    
    # Оптимизация для случая без перекрытия
    if overlap_size == 0:
        current_chunk_lines = []
        current_size = 0
        
        for line in lines:
            line_text = line + '\n'
            line_size = len(line_text.encode('utf-8'))
            
            # Если добавление строки превысит размер чанка
            if current_size + line_size > chunk_size and current_chunk_lines:
                # Сохраняем текущий чанк
                chunk_text = '\n'.join(current_chunk_lines)
                chunks.append(chunk_text)
                # Начинаем новый чанк
                current_chunk_lines = [line]
                current_size = line_size
            else:
                # Добавляем строку в текущий чанк
                current_chunk_lines.append(line)
                current_size += line_size
        
        # Добавляем последний чанк
        if current_chunk_lines:
            chunk_text = '\n'.join(current_chunk_lines)
            chunks.append(chunk_text)
        
        logger.debug("Chunks created (no overlap): %s", len(chunks))
        return chunks
    
    # Оригинальная логика с перекрытием
    current_chunk_lines = []
    current_size = 0
    
    i = 0
    while i < len(lines):
        line = lines[i]
        line_text = line + '\n'
        line_size = len(line_text.encode('utf-8'))
        
        # Если добавление строки превысит размер чанка
        if current_size + line_size > chunk_size and current_chunk_lines:
            # Сохраняем текущий чанк
            chunk_text = '\n'.join(current_chunk_lines)
            chunks.append(chunk_text)
            
            # Вычисляем, сколько строк нужно для перекрытия
            overlap_text = '\n'.join(current_chunk_lines)
            overlap_bytes = len(overlap_text.encode('utf-8'))
            
            # Находим начало перекрытия (последние overlap_size байт)
            if overlap_bytes > overlap_size:
                # Берем последние строки, которые помещаются в overlap_size
                overlap_lines = []
                overlap_current_size = 0
                
                for j in range(len(current_chunk_lines) - 1, -1, -1):
                    line_to_add = current_chunk_lines[j] + '\n'
                    line_to_add_size = len(line_to_add.encode('utf-8'))
                    if overlap_current_size + line_to_add_size <= overlap_size:
                        overlap_lines.insert(0, current_chunk_lines[j])
                        overlap_current_size += line_to_add_size
                    else:
                        break
                
                current_chunk_lines = overlap_lines
                current_size = overlap_current_size
            else:
                # Весь предыдущий чанк помещается в перекрытие
                current_chunk_lines = current_chunk_lines.copy()
                current_size = overlap_bytes
            
            # Добавляем текущую строку
            current_chunk_lines.append(line)
            current_size += line_size
        else:
            # Добавляем строку в текущий чанк
            current_chunk_lines.append(line)
            current_size += line_size
        
        i += 1
    
    # Добавляем последний чанк
    if current_chunk_lines:
        chunk_text = '\n'.join(current_chunk_lines)
        chunks.append(chunk_text)

    logger.debug("Chunks created (with overlap): %s", len(chunks))
    
    return chunks


async def recursive_summarize(summaries: list, filename: str = "", level: int = 1) -> str:
    """
    Рекурсивно саммаризирует список саммари, пока не получится единое саммари
    
    Args:
        summaries: Список саммари для объединения
        filename: Имя файла (опционально)
        level: Уровень рекурсии (для отладки)
    
    Returns:
        Финальное единое саммари
    """
    # Если саммари только одно, возвращаем его
    if len(summaries) == 1:
        return summaries[0]
    
    # Максимальный размер для одного запроса (примерно 60K токенов)
    MAX_CHUNK_SIZE = 12 * 1024  # 12 KB
    OVERLAP_SIZE = 2 * 1024  # 2 KB перекрытие
    
    # Объединяем все саммари
    combined_text = "\n\n".join([f"Саммари {i+1}:\n{s}" for i, s in enumerate(summaries)])
    combined_size = len(combined_text.encode('utf-8'))
    
    # Если объединенный текст помещается в один запрос
    if combined_size <= MAX_CHUNK_SIZE:
        user_message = f"Создай итоговое саммари документа"
        if filename:
            user_message += f" {filename}"
        user_message += f" на основе следующих саммари (уровень {level}):\n\n{combined_text}"
        
        return await make_api_request(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=SUMMARY_PROMPT,
            temperature=0.7
        )
    else:
        # Разбиваем на чанки с перекрытием и обрабатываем каждый
        chunks = create_sliding_window_chunks(combined_text, MAX_CHUNK_SIZE, OVERLAP_SIZE)
        
        # Саммаризируем каждый чанк параллельно с ограничением через семафор
        tasks = []
        for i, chunk in enumerate(chunks):
            user_message = f"Создай саммари этой части анализа документа"
            if filename:
                user_message += f" {filename}"
            user_message += f" (уровень {level}, часть {i+1} из {len(chunks)}):\n\n{chunk}"
            
            task = make_api_request(
                messages=[{"role": "user", "content": user_message}],
                system_prompt=SUMMARY_PROMPT,
                temperature=0.7
            )
            tasks.append(task)
        
        new_summaries = await asyncio.gather(*tasks)
        
        # Рекурсивно продолжаем саммаризацию
        return await recursive_summarize(new_summaries, filename, level + 1)


# Декоратор для обработки фотографий
@dp.message(F.photo)
async def handle_photo(message: Message):
    """Обработчик фотографий"""
    await message.answer(MSG_UNSUPPORTED_FORMAT)


# Декоратор для обработки видео
@dp.message(F.video)
async def handle_video(message: Message):
    """Обработчик видео"""
    await message.answer(MSG_FILE_VIDEO)


# Декоратор для обработки аудио
@dp.message(F.audio)
async def handle_audio(message: Message):
    """Обработчик аудио"""
    await message.answer(MSG_FILE_AUDIO)


# Декоратор для обработки документов
@dp.message(F.document)
async def handle_document(message: Message):
    """Обработчик документов"""
    # Получаем информацию о документе
    document = message.document
    if not document:
        await message.answer("❌ Не удалось получить информацию о документе")
        return
    
    filename = document.file_name or "unknown"
    display_filename = filename.replace("\\", " ")
    file_type = get_file_type(filename)
    file_size = document.file_size
    
    # Определяем расширение файла
    file_extension = filename.lower().split('.')[-1] if '.' in filename else ''
    
    # Проверяем, поддерживается ли формат
    supported_formats = [
        'pdf', 'docx', 'doc', 'pptx', 'txt', 'rtf',
        'xlsx', 'xls', 'csv', 'md', 'html', 'htm', 'odt', 'odp',
        'py', 'js', 'ts', 'tsx', 'c', 'cpp', 'h', 'hpp', 'java', 'go'
    ]
    if file_extension not in supported_formats:
        log_event(
            "unsupported_format",
            user_id=message.from_user.id,
            filename=filename,
            ext=file_extension
        )
        await message.answer(MSG_UNSUPPORTED_FORMAT)
        return
    
    # Отправляем сообщение о начале обработки
    processing_msg = await message.answer(MSG_PROCESSING_START.format(filename=display_filename, file_type=file_type))
    log_event("document_received", user_id=message.from_user.id, filename=filename, ext=file_extension, mime=file_type)
    
    generation_consumed = False
    try:
        # Скачиваем файл с повторами и повышенным таймаутом
        file_bytes = await download_file_bytes(document.file_id)
        
        # Извлекаем текст из документа (для всех остальных форматов)
        text = extract_text_from_file(file_bytes, file_extension)
        
        if not text:
            # Для PDF явно говорим, что это может быть скан
            if file_extension == "pdf":
                await processing_msg.edit_text(
                    MSG_EXTRACT_FAIL_SCAN.format(filename=display_filename)
                )
            else:
                await processing_msg.edit_text(
                    MSG_EXTRACT_FAIL.format(filename=display_filename)
                )
            log_event("extract_fail", user_id=message.from_user.id, filename=filename, ext=file_extension)
            return
        
        # Проверяем лимит операций/квоту
        user = get_or_create_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        
        can_operate, remaining, plan_type = check_operations_quota(message.from_user.id)
        if not can_operate:
            await processing_msg.edit_text(
                MSG_QUOTA_EXCEEDED.format(plan_type=plan_type, remaining=remaining),
                reply_markup=build_subscribe_keyboard()
            )
            log_event("quota_exceeded", user_id=message.from_user.id, plan_type=plan_type, remaining=remaining)
            return
        
        # Обновляем сообщение о начале саммаризации
        words = len(text.split())
        read_time = format_read_time(words)
        base_progress = (
            f"📄 Документ: {display_filename}\n"
            f"📝 Символов: {len(text)} | Слов: {words}\n"
            f"⏱️ Читать самому: ~{read_time}\n"
            "🤖 Делаю саммари"
        )
        try:
            await processing_msg.edit_text(base_progress + "...")
        except Exception:
            await processing_msg.edit_text(base_progress + "...")

        stop_animation = asyncio.Event()

        async def _stop_animation_task():
            if stop_animation.is_set():
                return
            stop_animation.set()
            try:
                await animation_task
            except Exception:
                animation_task.cancel()

        async def _animate_progress():
            dots = [".", "..", "..."]
            idx = 0
            while not stop_animation.is_set():
                try:
                    await processing_msg.edit_text(f"{base_progress}{dots[idx]}")
                except Exception:
                    pass
                idx = (idx + 1) % len(dots)
                await asyncio.sleep(1.2)

        animation_task = asyncio.create_task(_animate_progress())
        
        # Разбиваем текст на чанки и саммаризируем
        # Размер первоначальных чанков
        MAX_CHUNK_SIZE = 50 * 1024  # 50 KB
        OVERLAP_SIZE = 0  # Без перекрытия для ускорения
        
        chunks = create_sliding_window_chunks(text, MAX_CHUNK_SIZE, OVERLAP_SIZE)
        
        if not chunks:
            await _stop_animation_task()
            await processing_msg.edit_text(
                MSG_CHUNK_FAIL
            )
            return
        
        # Саммаризируем чанки параллельно; внутри make_api_request стоит семафор
        logger.debug("Chunks to summarize: %s", len(chunks))
        tasks = []
        for chunk in chunks:
            # небольшая задержка, чтобы не стартовать все задачи одновременно
            await asyncio.sleep(0.05)
            tasks.append(summarize_text_chunk(chunk))
        summaries = await asyncio.gather(*tasks)
        
        # Объединяем сокращенные чанки и создаем финальное саммари
        combined_summaries = "\n\n".join(summaries)
        
        # Если объединенный текст небольшой, создаем финальное саммари сразу
        if len(combined_summaries.encode('utf-8')) <= 25 * 1024:  # 25 KB
            user_message = f"Создай итоговое саммари документа {display_filename} на основе следующих сокращенных частей:\n\n{combined_summaries}"
            
            final_summary = await make_api_request(
                messages=[{"role": "user", "content": user_message}],
                system_prompt=SUMMARY_PROMPT,
                temperature=0.7
            )
        else:
            # Если слишком большой, используем рекурсивное объединение
            final_summary = await recursive_summarize(summaries, filename)
        
        # Останавливаем анимацию перед отправкой итогового ответа
        await _stop_animation_task()

        # Формируем итоговое сообщение из JSON или с fallback на сырой текст
        summary_data = parse_summary_json(final_summary)
        if summary_data:
            # Название в заголовке всегда из имени файла, чтобы не подставлялось «суть файла»
            title = display_filename
            main_topic = summary_data.get("main_topic") or ""
            key_points = summary_data.get("key_points") or ""
            important_info = summary_data.get("important_info") or ""
            header = f"✅ {escape_markdown(title)}\n\n"
            message_text = header + (
                f"*1. Основная тема документа:*\n{escape_markdown(main_topic)}\n\n"
                f"*2. Ключевые моменты и выводы:*\n{escape_markdown(key_points)}\n\n"
                f"*3. Важная информация:*\n{escape_markdown(important_info)}\n\n"
            ) + SUMMARY_SIGNATURE
            parse_mode = "Markdown"
        else:
            # На случай невалидного JSON отправляем сырое саммари без форматирования
            header = f"✅ {escape_markdown(display_filename)}\n\n"
            message_text = header + escape_markdown(final_summary) + "\n\n" + SUMMARY_SIGNATURE
            parse_mode = "Markdown"

        # Сохраняем текст документа для одноразового вопроса
        try:
            save_document_content(message.from_user.id, display_filename, file_type, text)
        except Exception as e:
            logger.warning("Failed to save document for Q&A: %s", e)
        
        # Логируем операцию
        log_operation(
            user_id=message.from_user.id,
            operation_type='document_summary',
            file_name=filename,
            file_type=file_type
        )

        # Списываем одну генерацию
        generation_consumed = consume_generation(message.from_user.id)

        # Бонусы за первую генерацию по рефералке
        referrer_id = mark_referral_first_generation(message.from_user.id)
        if referrer_id:
            add_bonus_quota(referrer_id, 2)
            try:
                await bot.send_message(referrer_id, MSG_REF_AUTHOR_USED)
            except Exception as e:
                logger.warning("Notify referrer first generation failed: %s", e)
        
        # Отправляем результат
        ask_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Вопрос по документу", callback_data="ask_doc")]
            ]
        )
        await processing_msg.edit_text(
            message_text,
            parse_mode=parse_mode,
            reply_markup=ask_kb
        )
        log_event("document_summary_ok", user_id=message.from_user.id, filename=filename, ext=file_extension)
        
    except Exception as e:
        logger.exception("Error while processing document")
        if 'animation_task' in locals():
            try:
                await _stop_animation_task()
            except Exception:
                pass
        await processing_msg.edit_text(
            MSG_ERROR
        )
        # Если списали генерацию перед падением — вернём её пользователю.
        if generation_consumed:
            try:
                add_bonus_quota(message.from_user.id, 1)
                log_event("document_summary_refund", user_id=message.from_user.id, filename=filename)
            except Exception as refund_error:
                logger.warning("Failed to refund generation after document error: %s", refund_error)

# Декоратор для обработки ссылок
@dp.message(Command("start"))
async def handle_start(message: Message):
    """Приветствие и возможная активация реферала"""
    payload = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name
    )

    log_event("start", user_id=message.from_user.id, payload=payload)

    if payload:
        referrer_id = decode_ref_code(payload)
        if referrer_id and referrer_id != message.from_user.id:
            is_new = register_referral_start(
                invitee_id=message.from_user.id,
                referrer_id=referrer_id,
                code=payload
            )
            if is_new:
                # Подстраховка: убедимся, что у автора есть запись пользователя/квота
                get_or_create_user(referrer_id)
                add_bonus_quota(referrer_id, 1)
                log_event("referral_opened", referrer_id=referrer_id, invitee_id=message.from_user.id)
                try:
                    await bot.send_message(referrer_id, MSG_REF_AUTHOR_OPENED)
                except Exception as e:
                    logger.warning("Notify referrer open failed: %s", e)

    # Если пользователь пришёл по реферальной ссылке (есть payload), не отправляем
    # стандартное приветствие, чтобы избежать автосообщения при переходе по deep link.
    if not payload:
        await message.answer(MSG_START)


@dp.message(F.text)
async def handle_text(message: Message):
    """Обработчик текстовых сообщений - проверяет на ссылки и секретное слово"""
    text = message.text.strip()
    
    # Проверяем на секретное слово "noool"
    if text.lower() == "noool":
        # Получаем или создаем пользователя
        user = get_or_create_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        
        # Добавляем 5 генераций к текущей квоте
        new_limit = add_bonus_quota(message.from_user.id, 5)
        
        await message.answer(MSG_SECRET_OK.format(limit=new_limit))
        return
    
    if is_url(text):
        processing_msg = await message.answer(
            MSG_LINK_PROCESSING_START.format(url=text)
        )
        log_event("link_received", user_id=message.from_user.id, url=text)
        try:
            user = get_or_create_user(
                user_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name
            )

            can_operate, remaining, plan_type = check_operations_quota(message.from_user.id)
            if not can_operate:
                await processing_msg.edit_text(
                    MSG_QUOTA_EXCEEDED.format(plan_type=plan_type, remaining=remaining),
                    reply_markup=build_subscribe_keyboard()
                )
                log_event("quota_exceeded", user_id=message.from_user.id, plan_type=plan_type, remaining=remaining)
                return

            page_text = await extract_text_from_url(text)
            if not page_text or not page_text.strip():
                await processing_msg.edit_text(
                    MSG_LINK_EXTRACT_FAIL.format(url=text)
                )
                log_event("link_extract_fail", user_id=message.from_user.id, url=text)
                return

            link_words = len(page_text.split())
            link_read_time = format_read_time(link_words)
            base_progress = (
                f"🌐 Страница: {text}\n"
                f"📝 Символов: {len(page_text)} | Слов: {link_words}\n"
                f"⏱️ Читать самому: ~{link_read_time}\n"
                "🤖 Делаю саммари"
            )
            try:
                await processing_msg.edit_text(base_progress + "...")
            except Exception:
                await processing_msg.edit_text(base_progress + "...")

            stop_animation = asyncio.Event()

            async def _stop_animation_task():
                if stop_animation.is_set():
                    return
                stop_animation.set()
                try:
                    await animation_task
                except Exception:
                    animation_task.cancel()

            async def _animate_progress():
                dots = [".", "..", "..."]
                idx = 0
                while not stop_animation.is_set():
                    try:
                        await processing_msg.edit_text(f"{base_progress}{dots[idx]}")
                    except Exception:
                        pass
                    idx = (idx + 1) % len(dots)
                    await asyncio.sleep(1.2)

            animation_task = asyncio.create_task(_animate_progress())

            MAX_CHUNK_SIZE = 50 * 1024  # 50 KB
            OVERLAP_SIZE = 0

            chunks = create_sliding_window_chunks(page_text, MAX_CHUNK_SIZE, OVERLAP_SIZE)
            if not chunks:
                await _stop_animation_task()
                await processing_msg.edit_text(MSG_CHUNK_FAIL)
                return

            tasks = []
            for chunk in chunks:
                await asyncio.sleep(0.05)
                tasks.append(summarize_text_chunk(chunk))
            summaries = await asyncio.gather(*tasks)

            combined_summaries = "\n\n".join(summaries)
            if len(combined_summaries.encode('utf-8')) <= 25 * 1024:
                user_message = (
                    f"Создай итоговое саммари страницы {text} "
                    f"на основе следующих сокращенных частей:\n\n{combined_summaries}"
                )
                final_summary = await make_api_request(
                    messages=[{"role": "user", "content": user_message}],
                    system_prompt=SUMMARY_PROMPT,
                    temperature=0.7
                )
            else:
                final_summary = await recursive_summarize(summaries, text)

            await _stop_animation_task()

            summary_data = parse_summary_json(final_summary)
            if summary_data:
                title = text
                main_topic = summary_data.get("main_topic") or ""
                key_points = summary_data.get("key_points") or ""
                important_info = summary_data.get("important_info") or ""
                header = f"✅ {escape_markdown(title)}\n\n"
                message_text = header + (
                    f"*1. Основная тема страницы:*\n{escape_markdown(main_topic)}\n\n"
                    f"*2. Ключевые моменты и выводы:*\n{escape_markdown(key_points)}\n\n"
                    f"*3. Важная информация:*\n{escape_markdown(important_info)}\n\n"
                ) + SUMMARY_SIGNATURE
                parse_mode = "Markdown"
            else:
                header = f"✅ {escape_markdown(text)}\n\n"
                message_text = header + escape_markdown(final_summary) + "\n\n" + SUMMARY_SIGNATURE
                parse_mode = "Markdown"

            # Сохраняем текст страницы для одноразового вопроса
            try:
                save_document_content(message.from_user.id, text, "URL", page_text)
            except Exception as e:
                logger.warning("Failed to save link content for Q&A: %s", e)

            log_operation(
                user_id=message.from_user.id,
                operation_type='link_summary',
                file_name=text,
                file_type='URL'
            )
            consume_generation(message.from_user.id)

            referrer_id = mark_referral_first_generation(message.from_user.id)
            if referrer_id:
                add_bonus_quota(referrer_id, 2)
                try:
                    await bot.send_message(referrer_id, MSG_REF_AUTHOR_USED)
                except Exception as e:
                    logger.warning("Notify referrer first generation failed: %s", e)

            await processing_msg.edit_text(
                message_text,
                parse_mode=parse_mode,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Вопрос по документу", callback_data="ask_doc")]
                    ]
                )
            )
            try:
                pass
            except Exception:
                pass
            log_event("link_summary_ok", user_id=message.from_user.id, url=text)
        except Exception as e:
            logger.exception("Error while processing link")
            if 'stop_animation' in locals():
                try:
                    await _stop_animation_task()
                except Exception:
                    pass
            await processing_msg.edit_text(MSG_ERROR)
    else:
        await message.answer(MSG_FILE_PROMPT)


@dp.callback_query(F.data == "share")
async def handle_share(callback: CallbackQuery):
    """Выдаёт реферальную ссылку для коллеги."""
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username

    code = encode_ref_code(callback.from_user.id)
    share_link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await callback.message.answer(
        MSG_SHARE_PROMPT.format(share_link=share_link)
    )
    log_event("share_link_requested", user_id=callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "ask_doc")
async def handle_ask_doc(callback: CallbackQuery):
    """Включает режим одного вопроса по последнему документу пользователя."""
    pending = get_pending_document(callback.from_user.id)
    if not pending or not pending.get("content"):
        await callback.message.answer("Нет сохранённого документа. Отправьте файл или ссылку заново.")
        await callback.answer()
        return
    question_sessions[callback.from_user.id] = pending["id"]
    await callback.message.answer("Задайте один вопрос по документу одним сообщением. После ответа текст будет удалён.")
    await callback.answer()


@dp.message()
async def handle_message(message: Message):
    """Обработчик всех остальных сообщений"""
    # Если пользователь в режиме вопроса — отвечаем и выходим
    if message.text and message.from_user.id in question_sessions:
        doc_id = question_sessions.pop(message.from_user.id, None)
        if doc_id:
            doc = get_document_by_id(doc_id)
            if not doc or not doc.get("content"):
                await message.answer("Не нашёл текст документа. Отправьте файл ещё раз.")
                return
            try:
                answer = await answer_question_with_document(doc["content"], message.text)
                await message.answer(answer)
            except Exception:
                logger.exception("Failed to answer question for document %s", doc_id)
                await message.answer("❌ Не удалось ответить на вопрос. Попробуйте позже.")
            finally:
                try:
                    mark_document_answered(doc_id)
                except Exception as e:
                    logger.warning("Failed to mark document answered: %s", e)
            return
    await message.answer(MSG_FILE_UNKNOWN)


@dp.callback_query(F.data.in_(["sub_basic", "sub_pro"]))
async def handle_subscription_choice(callback: CallbackQuery):
    choice = callback.data
    if choice == "sub_basic":
        plan = "basic"
    elif choice == "sub_pro":
        plan = "pro"

    log_event("subscription_click", user_id=callback.from_user.id, plan=plan)

    payment = await create_yookassa_payment(callback.from_user.id, plan)
    if not payment:
        log_event("subscription_payment_create_fail", user_id=callback.from_user.id, plan=plan)
        await callback.message.answer(MSG_SUBSCRIBE_FAILED)
        await callback.answer()
        return
    payment_id, confirm_url = payment
    log_event("subscription_payment_created", user_id=callback.from_user.id, plan=plan, payment_id=payment_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=confirm_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data=f"paychk:{payment_id}:{plan}")]
        ]
    )
    await callback.message.answer(
        MSG_SUBSCRIBE_CREATED.format(plan=plan.upper()),
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("paychk:"))
async def handle_payment_check(callback: CallbackQuery):
    try:
        _, payment_id, plan = callback.data.split(":", 2)
    except ValueError:
        await callback.answer(MSG_PAYMENT_BAD_DATA)
        log_event("payment_check_bad_data", user_id=callback.from_user.id, raw=callback.data)
        return
    status = await finalize_payment(payment_id, callback.from_user.id, plan)
    if not status:
        log_event("payment_check_fail", user_id=callback.from_user.id, payment_id=payment_id, plan=plan)
        await callback.message.answer(MSG_PAYMENT_CHECK_FAIL)
        await callback.answer()
        return
    if status == "succeeded":
        log_event("payment_succeeded", user_id=callback.from_user.id, payment_id=payment_id, plan=plan)
        await callback.message.answer(
            MSG_PAYMENT_SUCCESS.format(plan=plan.upper())
        )
    elif status in ("pending", "waiting_for_capture"):
        log_event("payment_pending", user_id=callback.from_user.id, payment_id=payment_id, plan=plan, status=status)
        await callback.message.answer(MSG_PAYMENT_PENDING)
    else:
        log_event("payment_other_status", user_id=callback.from_user.id, payment_id=payment_id, plan=plan, status=status)
        await callback.message.answer(MSG_PAYMENT_OTHER_STATUS.format(status=status))
    await callback.answer()


async def main():
    """Основная функция для запуска бота"""
    # Инициализируем базу данных
    init_database()
    logger.info("Database initialized")
    
    # Запускаем polling
    await dp.start_polling(bot)


if __name__ == '__main__':

    asyncio.run(main())
