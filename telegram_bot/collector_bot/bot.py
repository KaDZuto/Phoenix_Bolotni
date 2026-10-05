"""Main bot logic and handlers for Phoenix_Bolotni Collector Bot using aiogram 3."""
import json
import logging
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ContentType,
    FSInputFile,
    Message,
    ReplyKeyboardRemove,
)

from .archive import download_from_archive, send_to_archive
from .audio_convert import TempAudioProcessor, convert_to_pipeline_wav, get_audio_duration
from .auth import generate_audio_token
from .batch_export import export_batch
from .config import Config, config as default_config
from .db import Database
from .keyboards import (
    SpeciesCatalog,
    get_annotation_choice_keyboard,
    get_consent_keyboard,
    get_location_reply_keyboard,
    get_skip_inline_keyboard,
    get_species_keyboard,
)

logger = logging.getLogger(__name__)


class CollectorStates(StatesGroup):
    waiting_for_proposed_species = State()
    waiting_for_location_or_comment = State()


def setup_bot_router(bot: Bot, db: Database, catalog: SpeciesCatalog, cfg: Config) -> Router:
    router = Router(name="collector_router")

    # Helper to check admin permission
    def is_admin(user_id: int) -> bool:
        return not cfg.ADMIN_USER_IDS or user_id in cfg.ADMIN_USER_IDS

    # --- 1. /start & Consent ---
    @router.message(CommandStart())
    async def cmd_start(message: Message):
        user = message.from_user
        if not user:
            return

        is_consented = db.is_user_consented(user.id, validity_days=cfg.CONSENT_VALIDITY_DAYS)
        if not is_consented:
            text = (
                "👋 <b>Добро пожаловать в бот сбора данных Phoenix_Bolotni!</b>\n\n"
                "Этот бот собирает аудиозаписи голосов птиц фауны России "
                "(особенно околоводных и болотных видов) для открытого научного датасета "
                "и обучения нейросетевых моделей.\n\n"
                "⚖️ <b>Юридическое согласие:</b>\n"
                "Отправляя аудиозаписи, вы подтверждаете свое согласие на их безвозмездное "
                "использование для обучения моделей машинного обучения и публикации в открытом датасете.\n\n"
                "Пожалуйста, подтвердите согласие перед началом отправки:"
            )
            await message.answer(text, reply_markup=get_consent_keyboard(), parse_mode="HTML")
            return

        await message.answer(
            "✅ <b>Вы авторизованы для отправки записей!</b>\n\n"
            "🎙 <b>Как отправить запись:</b>\n"
            "Запишите голосовое сообщение прямо в Telegram или прикрепите аудиофайл "
            "(.ogg, .opus, .wav, .mp3, .m4a).\n\n"
            "После отправки вы сможете:\n"
            "• Быстро выбрать вид из списка фауны РФ\n"
            "• Либо открыть веб-плеер (Mini App) для точной разметки промежутка песни на спектрограмме.\n\n"
            "Команды:\n"
            "/stats — статистика собранных видов\n"
            "/undo — отменить последнюю присланную запись\n"
            "/help — подробная справка",
            parse_mode="HTML"
        )

    @router.callback_query(F.data == "consent_agree")
    async def on_consent_agree(callback: CallbackQuery):
        user = callback.from_user
        db.set_user_consent(user.id, user.username, user.first_name)
        await callback.answer("Согласие принято!")
        if callback.message and isinstance(callback.message, Message):
            await callback.message.edit_text(
                "✅ <b>Спасибо! Ваше согласие успешно зарегистрировано.</b>\n\n"
                "Теперь запишите голосовое сообщение (нажмите и удерживайте микрофон в Telegram) "
                "или отправьте аудиофайл.",
                parse_mode="HTML"
            )

    # --- 2. Audio & Voice Reception ---
    @router.message(F.voice | F.audio | (F.document & F.document.mime_type.startswith("audio/")))
    async def on_audio_received(message: Message, state: FSMContext):
        user = message.from_user
        if not user:
            return

        # Check consent
        if not db.is_user_consented(user.id, validity_days=cfg.CONSENT_VALIDITY_DAYS):
            await message.answer(
                "⚠️ Перед отправкой записей необходимо подтвердить согласие на использование данных.",
                reply_markup=get_consent_keyboard()
            )
            return

        # Extract file info
        file_id = ""
        duration = None
        if message.voice:
            file_id = message.voice.file_id
            duration = float(message.voice.duration) if message.voice.duration else None
        elif message.audio:
            file_id = message.audio.file_id
            duration = float(message.audio.duration) if message.audio.duration else None
        elif message.document:
            file_id = message.document.file_id

        # Save incoming submission to DB
        sub_info = db.create_incoming_submission(
            user_id=user.id,
            username=user.username,
            file_id=file_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            duration_sec=duration
        )
        sub_id = sub_info["id"]

        # Build WebApp URL with signed token
        webapp_url = None
        if cfg.WEBAPP_URL:
            token = generate_audio_token(sub_id, cfg.PROXY_SECRET)
            audio_proxy_url = f"{cfg.PROXY_URL_BASE}/{sub_id}?sig={token}"
            params = {
                "sub_id": str(sub_id),
                "sig": token,
                "audio": audio_proxy_url,
                "dur": str(duration or 0)
            }
            webapp_url = f"{cfg.WEBAPP_URL}#{urllib.parse.urlencode(params)}"

        kb = get_annotation_choice_keyboard(sub_id, webapp_url=webapp_url)
        dur_text = f" (длительность: {duration:.1f} с)" if duration else ""
        await message.answer(
            f"🎵 Запись получена{dur_text}!\n\n"
            "Выберите способ разметки:",
            reply_markup=kb
        )

    # --- 3. Fast Annotation Flow ---
    @router.callback_query(F.data.startswith("fast_ann:"))
    async def on_fast_annotation_click(callback: CallbackQuery):
        sub_id = int(callback.data.split(":")[1])
        kb = get_species_keyboard(catalog, sub_id=sub_id, page=0, habitat="all")
        await callback.message.edit_text(
            "📋 <b>Выберите вид птицы из списка фауны РФ:</b>\n"
            "Используйте фильтры биотопов и кнопки навигации.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("sp_p:"))
    async def on_species_pagination(callback: CallbackQuery):
        parts = callback.data.split(":")
        sub_id = int(parts[1])
        page = int(parts[2])
        habitat = parts[3]
        kb = get_species_keyboard(catalog, sub_id=sub_id, page=page, habitat=habitat)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await callback.answer()

    @router.callback_query(F.data == "sp_noop")
    async def on_species_noop(callback: CallbackQuery):
        await callback.answer()

    @router.callback_query(F.data.startswith("sp_sel:"))
    async def on_species_selected(callback: CallbackQuery, state: FSMContext):
        parts = callback.data.split(":")
        sub_id = int(parts[1])
        species_slug = parts[2]

        sub = db.get_submission_by_id(sub_id)
        if not sub:
            await callback.answer("Ошибка: запись не найдена.", show_alert=True)
            return

        await callback.message.edit_text("⏳ <i>Обработка и конвертация аудио в 32 кГц моно...</i>", parse_mode="HTML")
        await callback.answer()

        try:
            await process_and_archive_submission(
                bot=bot,
                db=db,
                sub=sub,
                species_slug=species_slug,
                cfg=cfg,
                annotation_type="fast"
            )
        except Exception as e:
            logger.error(f"Error processing submission {sub_id}: {e}", exc_info=True)
            await callback.message.edit_text(f"❌ Ошибка обработки аудио: {e}")
            return

        species_name = catalog.get_species_name(species_slug)
        await state.update_data(active_sub_id=sub_id)
        await state.set_state(CollectorStates.waiting_for_location_or_comment)

        await callback.message.answer(
            f"✅ Записано: <b>{species_name}</b>, спасибо!\n\n"
            "При желании вы можете отправить геопозицию (кнопка ниже) "
            "или написать комментарий к записи (например, биотоп или поведение птицы).\n\n"
            "<i>/undo — если ошиблись и хотите удалить запись.</i>",
            reply_markup=get_location_reply_keyboard(),
            parse_mode="HTML"
        )

        # Check auto-batch
        await check_and_run_auto_batch(bot, db, cfg)

    # --- 4. Custom Species ("Другой вид") ---
    @router.callback_query(F.data.startswith("sp_other:"))
    async def on_species_other(callback: CallbackQuery, state: FSMContext):
        sub_id = int(callback.data.split(":")[1])
        await state.update_data(active_sub_id=sub_id)
        await state.set_state(CollectorStates.waiting_for_proposed_species)

        await callback.message.edit_text(
            "➕ <b>Добавление нового вида</b>\n\n"
            "Пожалуйста, напишите название вида в ответном сообщении "
            "(по-русски и/или на латыни).\n\n"
            "<i>Запись будет помечена как предложенная и проверена экспертом вручную.</i>",
            parse_mode="HTML"
        )
        await callback.answer()

    @router.message(CollectorStates.waiting_for_proposed_species, F.text)
    async def on_proposed_species_entered(message: Message, state: FSMContext):
        proposed_name = message.text.strip()
        data = await state.get_data()
        sub_id = data.get("active_sub_id")
        if not sub_id:
            await message.answer("Ошибка контекста. Пожалуйста, отправьте аудио заново.")
            await state.clear()
            return

        sub = db.get_submission_by_id(sub_id)
        if not sub:
            await message.answer("Запись не найдена.")
            await state.clear()
            return

        status_msg = await message.answer("⏳ <i>Конвертация и сохранение предложенного вида...</i>", parse_mode="HTML")

        try:
            await process_and_archive_submission(
                bot=bot,
                db=db,
                sub=sub,
                species_slug="_proposed",
                proposed_name=proposed_name,
                cfg=cfg,
                annotation_type="fast"
            )
        except Exception as e:
            logger.error(f"Error processing proposed submission {sub_id}: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка обработки: {e}")
            await state.clear()
            return

        await status_msg.delete()
        await state.update_data(active_sub_id=sub_id)
        await state.set_state(CollectorStates.waiting_for_location_or_comment)

        await message.answer(
            f"✅ Записано как предложенный вид: <b>«{proposed_name}»</b>!\n\n"
            "Эксперт проверит его и добавит в официальный whitelist.\n"
            "Вы можете отправить геопозицию или комментарий (или нажать «Пропустить»).\n\n"
            "<i>/undo — если ошиблись.</i>",
            reply_markup=get_location_reply_keyboard(),
            parse_mode="HTML"
        )

        await check_and_run_auto_batch(bot, db, cfg)

    # --- 5. Optional Location & Comment ---
    @router.message(CollectorStates.waiting_for_location_or_comment, F.location)
    async def on_location_received(message: Message, state: FSMContext):
        loc = message.location
        data = await state.get_data()
        sub_id = data.get("active_sub_id")
        if sub_id and loc:
            with db._get_connection() as conn:
                conn.execute(
                    "UPDATE submissions SET lat = ?, lon = ? WHERE id = ?",
                    (loc.latitude, loc.longitude, sub_id)
                )
                conn.commit()
        await state.clear()
        await message.answer("📍 Геопозиция сохранена. Спасибо за вклад!", reply_markup=ReplyKeyboardRemove())

    @router.message(CollectorStates.waiting_for_location_or_comment, F.text == "⏩ Пропустить")
    async def on_skip_location_or_comment(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Принято! Ждём новых записей.", reply_markup=ReplyKeyboardRemove())

    @router.message(CollectorStates.waiting_for_location_or_comment, F.text)
    async def on_comment_received(message: Message, state: FSMContext):
        comment_text = message.text.strip()
        data = await state.get_data()
        sub_id = data.get("active_sub_id")
        if sub_id:
            with db._get_connection() as conn:
                conn.execute(
                    "UPDATE submissions SET comment = ? WHERE id = ?",
                    (comment_text, sub_id)
                )
                conn.commit()
        await state.clear()
        await message.answer("💬 Комментарий сохранён. Спасибо!", reply_markup=ReplyKeyboardRemove())

    # --- 6. WebApp Data (Mini App Precise Annotation) ---
    @router.message(F.web_app_data)
    async def on_web_app_data(message: Message):
        raw_data = message.web_app_data.data
        try:
            payload = json.loads(raw_data)
            sub_id = int(payload.get("submission_id"))
            species_slug = payload.get("species_slug") or "_uncertain"
            proposed_name = payload.get("proposed_name")
            start_sec = float(payload.get("start_sec", 0.0))
            end_sec = float(payload.get("end_sec", 0.0))
        except Exception as e:
            await message.answer(f"⚠️ Ошибка разбора данных разметки: {e}")
            return

        sub = db.get_submission_by_id(sub_id)
        if not sub:
            await message.answer("Запись не найдена в базе данных.")
            return

        status_msg = await message.answer(
            f"✂️ Вырезаем фрагмент [{start_sec:.2f}s - {end_sec:.2f}s] и конвертируем в 32 кГц моно..."
        )

        try:
            await process_and_archive_submission(
                bot=bot,
                db=db,
                sub=sub,
                species_slug=species_slug,
                proposed_name=proposed_name,
                cfg=cfg,
                annotation_type="precise_webapp",
                start_sec=start_sec,
                end_sec=end_sec
            )
        except Exception as e:
            logger.error(f"Error slicing audio for sub {sub_id}: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка нарезки: {e}")
            return

        species_display = proposed_name if proposed_name else catalog.get_species_name(species_slug)
        await status_msg.edit_text(
            f"✅ <b>Фрагмент успешно размечен!</b>\n"
            f"Вид: <b>{species_display}</b>\n"
            f"Интервал: <code>{start_sec:.2f}с – {end_sec:.2f}с</code>\n\n"
            f"Запись сохранена в архив. /undo — если ошиблись.",
            parse_mode="HTML"
        )

        await check_and_run_auto_batch(bot, db, cfg)

    # --- 7. User Commands: /undo, /stats, /help ---
    @router.message(Command("undo"))
    async def cmd_undo(message: Message):
        user = message.from_user
        if not user:
            return
        undone = db.undo_last_pending_submission(user.id)
        if undone:
            name = undone.get("proposed_name") or catalog.get_species_name(undone.get("species_slug", ""))
            await message.answer(
                f"↩️ Ваша последняя запись (вид: <b>{name}</b>) успешно отменена.",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                "❌ Нет записей, доступных для отмены. "
                "(Записи, уже отправленные в архивный пакет на проверку, отменить нельзя)."
            )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message):
        counts = db.get_species_counts()
        if not counts:
            await message.answer("📊 База записей пока пуста. Будьте первыми!")
            return

        lines = ["📊 <b>Собранные записи по видам (по возрастанию):</b>\n"]
        for slug, cnt in counts:
            name = catalog.get_species_name(slug)
            lines.append(f"• <b>{name}</b>: <code>{cnt}</code>")

        proposed = db.get_proposed_species()
        if proposed:
            lines.append("\n➕ <b>Предложенные новые виды:</b>")
            for name, cnt in proposed:
                lines.append(f"• <i>{name}</i>: <code>{cnt}</code>")

        pending_cnt = db.count_pending()
        lines.append(f"\n⏳ Ожидают батч-экспорта: <b>{pending_cnt}/{cfg.BATCH_SIZE}</b>")

        # Contributor's own score
        trust = db.get_user_trust_stats(message.from_user.id)
        lines.append(
            f"\n👤 <b>Ваш вклад:</b> отправлено {trust['total']}, "
            f"принято экспертами: {trust['accepted']} (рейтинг доверия: {int(trust['trust_rate'] * 100)}%)"
        )

        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("help"))
    async def cmd_help(message: Message):
        help_text = (
            "📖 <b>Инструкция Phoenix_Bolotni Collector:</b>\n\n"
            "1. Запишите голос птицы в Telegram (голосовое сообщение) или прикрепите файл.\n"
            "2. Выберите быструю разметку (для чистых записей) или откройте Mini App плеер, "
            "чтобы пальцем выделить отрезок с песней на спектрограмме.\n"
            "3. Если нужного вида нет в списке, нажмите «➕ Другой вид» и напишите название.\n"
            "4. Каждые 10 записей автоматически упаковываются в датасет и отправляются экспертам.\n\n"
            "Команды:\n"
            "/start — начало работы\n"
            "/stats — статистика и редкие виды\n"
            "/undo — отменить последнюю отправку\n"
        )
        if is_admin(message.from_user.id):
            help_text += (
                "\n👑 <b>Админ-команды:</b>\n"
                "/export — принудительно собрать батч-архив сейчас\n"
                "/new_species_requests — список предложенных видов\n"
                "/reload_whitelist — перезагрузить whitelist без рестарта\n"
                "/review &lt;id&gt; approve/reject — экспертная оценка записи\n"
            )
        await message.answer(help_text, parse_mode="HTML")

    # --- 8. Admin Commands ---
    @router.message(Command("export"))
    async def cmd_export(message: Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Команда доступна только администраторам.")
            return

        status_msg = await message.answer("📦 <i>Сборка пакета записей и упаковка в ZIP...</i>", parse_mode="HTML")
        result = await export_batch(
            bot=bot,
            db=db,
            admin_chat_id=cfg.ADMIN_CHAT_ID or message.chat.id,
            limit=None,
            temp_dir=cfg.TEMP_DIR
        )
        if result:
            await status_msg.edit_text(
                f"✅ Экспорт завершён!\n"
                f"Пакет: <code>{result['batch_id']}</code>\n"
                f"Записей: {result['records_count']}",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text("ℹ️ Нет ожидающих записей для экспорта.")

    @router.message(Command("new_species_requests"))
    async def cmd_new_species_requests(message: Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Команда доступна только администраторам.")
            return

        proposed = db.get_proposed_species()
        if not proposed:
            await message.answer("ℹ️ Нет предложенных новых видов.")
            return

        lines = ["📝 <b>Запросы на добавление новых видов:</b>\n"]
        for name, cnt in proposed:
            lines.append(f"• <b>{name}</b>: <code>{cnt}</code> записей")
        lines.append("\nПосле добавления вида в whitelist модели используйте /reload_whitelist.")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("reload_whitelist"))
    async def cmd_reload_whitelist(message: Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Команда доступна только администраторам.")
            return

        count = catalog.reload()
        await message.answer(
            f"🔄 <b>Whitelist успешно обновлен!</b>\n"
            f"Загружено видов: <b>{count}</b>.",
            parse_mode="HTML"
        )

    @router.message(Command("review"))
    async def cmd_review(message: Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Команда доступна только администраторам.")
            return

        parts = message.text.strip().split()
        if len(parts) < 3:
            await message.answer("Использование: <code>/review &lt;submission_id&gt; approve|reject</code>", parse_mode="HTML")
            return

        try:
            sub_id = int(parts[1])
            verdict = parts[2].lower()
            is_approved = (verdict in ("approve", "1", "yes", "true", "ok"))
            success = db.review_submission(sub_id, is_approved)
            if success:
                v_text = "Одобрено (+1 в рейтинг)" if is_approved else "Отклонено"
                await message.answer(f"✅ Статус записи #{sub_id} обновлен: <b>{v_text}</b>", parse_mode="HTML")
            else:
                await message.answer(f"❌ Запись #{sub_id} не найдена.")
        except ValueError:
            await message.answer("Ошибка: ID записи должен быть числом.")

    @router.message(Command("top_contributors"))
    async def cmd_top_contributors(message: Message):
        top = db.get_top_contributors(limit=10)
        if not top:
            await message.answer("📊 Рейтинг контрибьюторов пока пуст.")
            return

        lines = ["🏆 <b>Топ контрибьюторов по проверенным записям:</b>\n"]
        for idx, user in enumerate(top, 1):
            rate_pct = int(user["trust_rate"] * 100)
            lines.append(
                f"{idx}. <b>{user['name']}</b> — принято: <b>{user['accepted']}</b> / {user['total']} "
                f"(доверие: <code>{rate_pct}%</code>)"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("trust"))
    async def cmd_trust(message: Message):
        parts = message.text.strip().split()
        target_id = message.from_user.id
        if len(parts) > 1:
            if not is_admin(message.from_user.id):
                await message.answer("⛔ Просмотр чужого рейтинга доступен только администраторам.")
                return
            if parts[1].isdigit():
                target_id = int(parts[1])

        trust = db.get_user_trust_stats(target_id)
        rate_pct = int(trust["trust_rate"] * 100)
        text = (
            f"👤 <b>Метрика доверия контрибьютора (ID: {target_id}):</b>\n\n"
            f"• Всего записей: <b>{trust['total']}</b>\n"
            f"• Принято экспертами: <b>{trust['accepted']}</b>\n"
            f"• Отклонено экспертами: <b>{trust['rejected']}</b>\n"
            f"• Рейтинг доверия: <b>{rate_pct}%</b>\n\n"
            f"<i>Рейтинг повышается при подтверждении экспертом соответствия вида присланной записи.</i>"
        )
        await message.answer(text, parse_mode="HTML")

    return router



async def process_and_archive_submission(
    bot: Bot,
    db: Database,
    sub: Dict[str, Any],
    species_slug: str,
    cfg: Config,
    proposed_name: Optional[str] = None,
    annotation_type: str = "fast",
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None
) -> None:
    """
    Downloads raw incoming audio to temporary directory,
    converts/slices to 32kHz mono WAV via ffmpeg,
    uploads resulting WAV to the archive channel,
    and GUARANTEES immediate deletion of temporary local files.
    """
    file_id = sub["telegram_file_id"]
    sub_id = sub["id"]

    with TempAudioProcessor(base_dir=cfg.TEMP_DIR) as tap:
        raw_input = tap.create_path(f"input_{sub_id}.tmp")
        processed_wav = tap.create_path(f"{sub['uuid']}.wav")

        # Download from Telegram (or mock if token is empty)
        if cfg.BOT_TOKEN:
            file_info = await bot.get_file(file_id)
            await bot.download_file(file_info.file_path, raw_input)
        else:
            # Test / simulated audio
            raw_input.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x00}\x00\x00\x00\xfa\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")

        # Convert and slice
        convert_to_pipeline_wav(
            input_path=raw_input,
            output_path=processed_wav,
            target_sr=cfg.TARGET_SR,
            mono=cfg.MONO,
            start_sec=start_sec,
            end_sec=end_sec
        )

        final_duration = get_audio_duration(processed_wav)

        # Upload to archive channel
        caption = f"Bird clip {sub['uuid']}: {species_slug}"
        if proposed_name:
            caption += f" (proposed: {proposed_name})"
        archive_file_id, archive_msg_id = await send_to_archive(
            bot=bot,
            channel_id=cfg.ARCHIVE_CHANNEL_ID,
            file_path=processed_wav,
            caption=caption
        )

        # Finalize submission in SQLite
        db.finalize_submission(
            sub_id=sub_id,
            species_slug=species_slug,
            archive_file_id=archive_file_id,
            archive_message_id=archive_msg_id,
            proposed_name=proposed_name,
            duration_sec=final_duration,
            annotation_type=annotation_type,
            start_sec=start_sec,
            end_sec=end_sec
        )


async def check_and_run_auto_batch(bot: Bot, db: Database, cfg: Config) -> None:
    """Checks if pending submissions count reached BATCH_SIZE and triggers export."""
    pending_count = db.count_pending()
    if pending_count >= cfg.BATCH_SIZE:
        logger.info(f"Auto-batch threshold reached ({pending_count}/{cfg.BATCH_SIZE}). Triggering export...")
        try:
            await export_batch(
                bot=bot,
                db=db,
                admin_chat_id=cfg.ADMIN_CHAT_ID,
                limit=cfg.BATCH_SIZE,
                temp_dir=cfg.TEMP_DIR
            )
        except Exception as e:
            logger.error(f"Auto-batch export failed: {e}", exc_info=True)
