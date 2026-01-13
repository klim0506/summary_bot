import asyncio
import io
import os
from typing import Dict, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv

from file_extractors import (
    extract_files_from_zip,
    ZipPackSession,
)

# Загружаем переменные окружения
load_dotenv()

# Токен отдельного бота для архивации
BOT_TOKEN_ZIP = os.getenv("BOT_TOKEN_ZIP")

bot = Bot(token=BOT_TOKEN_ZIP)
dp = Dispatcher()

# В памяти держим сессии упаковки по пользователю
pack_sessions: Dict[int, ZipPackSession] = {}
pending_finish: Dict[int, ZipPackSession] = {}
pending_custom_name: Dict[int, bool] = {}


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Архив-бот.\n"
        "Команды:\n"
        "/start_pack — начать сбор файлов в ZIP\n"
        "/finish — завершить и отправить ZIP\n"
        "/cancel — отменить сбор\n"
        "Отправьте ZIP, чтобы разархивировать и получить файлы."
    )


@dp.message(Command("start_pack"))
async def start_pack(message: Message):
    user_id = message.from_user.id
    pack_sessions[user_id] = ZipPackSession()
    await message.answer("✅ Сессия упаковки начата. Шлите файлы/документы. /finish чтобы собрать ZIP, /cancel чтобы отменить.")


@dp.message(Command("cancel"))
async def cancel_pack(message: Message):
    user_id = message.from_user.id
    if user_id in pack_sessions:
        del pack_sessions[user_id]
        await message.answer("🚫 Сессия упаковки отменена.")
    else:
        await message.answer("Нет активной сессии. Используйте /start_pack.")


@dp.message(Command("finish"))
async def finish_pack(message: Message):
    user_id = message.from_user.id
    session = pack_sessions.get(user_id)
    if not session:
        await message.answer("Нет активной сессии. Используйте /start_pack.")
        return

    pending_finish[user_id] = session
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сохранить как https://t.me/fast_unzip_bot ", callback_data="name:https://t.me/fast_unzip_bot ")],
        ]
    )
    await message.answer("Можно сразу отправить имя архива текстом, или выбрать кнопку по умолчанию:", reply_markup=kb)


@dp.message(F.document)
async def handle_document(message: Message):
    document = message.document
    if not document:
        await message.answer("❌ Не удалось получить документ.")
        return

    filename = document.file_name or "unknown"
    ext = filename.lower().split(".")[-1] if "." in filename else ""

    # Распаковка ZIP по месту
    if ext == "zip":
        processing = await message.answer(f"📦 Обрабатываю архив: {filename}")
        try:
            tg_file = await bot.get_file(document.file_id)
            file_content = await bot.download_file(tg_file.file_path)
            file_bytes = file_content.read()

            files = extract_files_from_zip(file_bytes)
            if not files:
                await processing.edit_text("❌ Не удалось извлечь файлы из архива.")
                return

            sent = 0
            skipped = 0
            for fname, fdata in files:
                if len(fdata) > 15 * 1024 * 1024:
                    skipped += 1
                    continue
                try:
                    await bot.send_document(
                        chat_id=message.chat.id,
                        document=BufferedInputFile(fdata, filename=fname),
                    )
                    sent += 1
                except Exception as e:
                    print(f"Ошибка отправки файла {fname}: {e}")
                    skipped += 1

            await processing.edit_text(
                f"✅ Архив {filename} обработан. Отправлено: {sent}, пропущено: {skipped}."
            )
        except Exception as e:
            print(f"Ошибка при распаковке zip: {e}")
            await processing.edit_text("❌ Ошибка при распаковке.")
        return

    # Иначе — попытка добавить в активную сессию упаковки
    session = pack_sessions.get(message.from_user.id)
    if not session:
        await message.answer("Нет активной сессии. Используйте /start_pack чтобы собрать ZIP.")
        return

    try:
        tg_file = await bot.get_file(document.file_id)
        file_content = await bot.download_file(tg_file.file_path)
        file_bytes = file_content.read()

        added = session.add_file(filename, file_bytes)
        if added:
            await message.answer(
                f"✅ Добавлено: {filename}. "
                f"Всего файлов: {session.count}, суммарно: {session.total_size // 1024} КБ."
            )
        else:
            await message.answer("❌ Превышены лимиты для упаковки (файл или общий размер).")
    except Exception as e:
        print(f"Ошибка при добавлении файла в сессию: {e}")
        await message.answer("❌ Ошибка при добавлении файла.")


def _build_and_send_zip(user_id: int, chat_id: int, filename: str):
    session = pending_finish.get(user_id) or pack_sessions.get(user_id)
    if not session:
        return None
    zip_bytes, stats = session.build_zip()
    if user_id in pack_sessions:
        del pack_sessions[user_id]
    if user_id in pending_finish:
        del pending_finish[user_id]
    if user_id in pending_custom_name:
        del pending_custom_name[user_id]
    if not zip_bytes:
        return {"error": "empty", "stats": stats}
    doc = BufferedInputFile(zip_bytes, filename=filename)
    return {"doc": doc, "stats": stats}


@dp.callback_query(F.data.startswith("name:"))
async def handle_name_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    choice = callback.data.split(":", 1)[1]
    if user_id not in pending_finish:
        await callback.answer("Нет сессии упаковки.")
        return
    filename = choice.strip()
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    result = _build_and_send_zip(user_id, callback.message.chat.id, filename)
    if not result or result.get("error") == "empty":
        await callback.message.answer("❌ Нет файлов для упаковки.")
        await callback.answer()
        return
    stats = result["stats"]
    await callback.message.answer(
        f"Собран ZIP. Добавлено файлов: {stats['added']}, пропущено: {stats['skipped']}, всего: {stats['total']}."
    )
    await bot.send_document(chat_id=callback.message.chat.id, document=result["doc"])
    await callback.answer()


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    if user_id in pending_finish:
        name = message.text.strip()
        if not name.lower().endswith(".zip"):
            name += ".zip"
        result = _build_and_send_zip(user_id, message.chat.id, name)
        if not result or result.get("error") == "empty":
            await message.answer("❌ Нет файлов для упаковки.")
            return
        stats = result["stats"]
        await message.answer(
            f"Собран ZIP. Добавлено файлов: {stats['added']}, пропущено: {stats['skipped']}, всего: {stats['total']}."
        )
        await bot.send_document(chat_id=message.chat.id, document=result["doc"])
        return
    await message.answer(
        "Отправьте файл для добавления в ZIP (если сессия активна) или ZIP-архив для распаковки.\n"
        "Команды: /start_pack /finish /cancel"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    await message.answer(
        "Отправьте файл для добавления в ZIP (если сессия активна) или ZIP-архив для распаковки.\n"
        "Команды: /start_pack /finish /cancel"
    )


@dp.message()
async def fallback(message: Message):
    await message.answer("Неизвестный тип. Используйте /start_pack или отправьте ZIP.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
