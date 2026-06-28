import asyncio
import html
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Final

from aiohttp import web
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIGURAÇÕES
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

LOG_GROUP_ID: Final[int] = -1003754061774
SUPPORT_URL: Final[str] = "https://t.me/SXP_suporte"
WEBHOOK_PATH: Final[str] = "/telegram-webhook"
BLOCKED_USERS_FILE: Final[str] = os.getenv("BLOCKED_USERS_FILE", "blocked_users.json")
ADMINS_FILE: Final[str] = os.getenv("ADMINS_FILE", "admins.json")
AUTO_BLOCK_EXPLICIT_DOCUMENT: Final[bool] = os.getenv("AUTO_BLOCK_EXPLICIT_DOCUMENT", "1") == "1"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(admin_id.strip()) for admin_id in ADMIN_IDS_RAW.split(",") if admin_id.strip().isdigit()}

# Custom emojis premium: coloque IDs reais do Telegram nas ENV se quiser usar depois.
# Sem custom_emoji_id real, o bot usa emojis normais para não quebrar o envio.
PREMIUM_EMOJI_OK = os.getenv("PREMIUM_EMOJI_OK", "")
PREMIUM_EMOJI_LOCK = os.getenv("PREMIUM_EMOJI_LOCK", "")

TERMS, GENERO, DATA, FOTO, VIDEO = range(5)

DATE_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/\d{4}$"
)

PENDING_REMINDER_SECONDS: Final[int] = 900

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# HELPERS
# =========================
def esc(text: object) -> str:
    return html.escape(str(text))


def html_link(label: str, url: str) -> str:
    # Telegram não permite escolher cor do link; a cor vem do app do usuário.
    # Esse helper cria URL clicável com HTML seguro.
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def looks_like_explicit_or_nude_image(image_bytes: bytes) -> bool:
    """Detector local simples para bloquear imagens com muita pele.

    Não substitui IA profissional, mas ajuda a barrar nudez/genitália óbvia enviada no lugar do documento.
    Foi deixado conservador para reduzir falso positivo em RG/CNH com rosto.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((320, 320))
        pixels = list(image.getdata())
        if not pixels:
            return False

        skin_pixels = 0
        for r, g, b in pixels:
            max_rgb = max(r, g, b)
            min_rgb = min(r, g, b)
            # Regra clássica aproximada de tom de pele em RGB.
            if r > 95 and g > 40 and b > 20 and (max_rgb - min_rgb) > 15 and abs(r - g) > 15 and r > g and r > b:
                skin_pixels += 1

        skin_ratio = skin_pixels / len(pixels)
        return skin_ratio >= 0.42
    except Exception as e:
        logger.error(f"Erro ao analisar possível nudez/documento: {e}")
        return False


def looks_like_document_image(image_bytes: bytes) -> bool:
    """Validação local básica de documento.

    Sem API externa/OCR, nenhum bot consegue garantir 100% que é RG/CNH.
    Aqui barramos imagens muito pequenas/estranhas; documentos reais normalmente passam.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        width, height = image.size
        if width < 350 or height < 220:
            return False

        ratio = width / height if height else 0
        inverse_ratio = height / width if width else 0

        # Aceita foto de documento em paisagem/retrato, inclusive foto tirada pelo celular.
        return 0.45 <= ratio <= 2.35 or 0.45 <= inverse_ratio <= 2.35
    except Exception as e:
        logger.error(f"Erro ao validar imagem de documento: {e}")
        return False


async def download_photo_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    telegram_file = await context.bot.get_file(file_id)
    data = await telegram_file.download_as_bytearray(
        read_timeout=60,
        write_timeout=60,
        connect_timeout=60,
    )
    return bytes(data)


def load_blocked_users() -> dict:
    try:
        with open(BLOCKED_USERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Erro ao carregar lista de bloqueados: {e}")
        return {}


def save_blocked_users(blocked_users: dict) -> None:
    try:
        with open(BLOCKED_USERS_FILE, "w", encoding="utf-8") as file:
            json.dump(blocked_users, file, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erro ao salvar lista de bloqueados: {e}")


def load_extra_admins() -> set[int]:
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, list):
                return {int(item) for item in data if str(item).isdigit()}
            return set()
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.error(f"Erro ao carregar admins extras: {e}")
        return set()


def save_extra_admins(admins: set[int]) -> None:
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as file:
            json.dump(sorted(admins), file, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erro ao salvar admins extras: {e}")


def all_admin_ids() -> set[int]:
    return set(ADMIN_IDS) | load_extra_admins()


def add_admin(user_id: int) -> None:
    admins = load_extra_admins()
    admins.add(user_id)
    save_extra_admins(admins)


def remove_admin(user_id: int) -> bool:
    admins = load_extra_admins()
    if user_id not in admins:
        return False
    admins.remove(user_id)
    save_extra_admins(admins)
    return True


def is_user_blocked(user_id: int) -> bool:
    return str(user_id) in load_blocked_users()


def block_user(user_id: int, reason: str, admin_user) -> None:
    blocked_users = load_blocked_users()
    blocked_users[str(user_id)] = {
        "reason": reason,
        "blocked_by": admin_name(admin_user),
        "blocked_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    save_blocked_users(blocked_users)


def unblock_user(user_id: int) -> bool:
    blocked_users = load_blocked_users()

    if str(user_id) not in blocked_users:
        return False

    blocked_users.pop(str(user_id), None)
    save_blocked_users(blocked_users)
    return True


def is_admin_user(user) -> bool:
    admins = all_admin_ids()
    if not admins:
        return True
    return user and user.id in admins


async def notify_blocked_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>"

    if update.callback_query:
        await update.callback_query.answer("Seu acesso ao bot foi bloqueado.", show_alert=True)
        try:
            await update.callback_query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
    elif update.message:
        await update.message.reply_text(text=text, parse_mode=ParseMode.HTML)


def blocked_guard(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user

        if user and is_user_blocked(user.id):
            await notify_blocked_access(update, context)
            context.user_data.clear()
            return ConversationHandler.END

        return await func(update, context, *args, **kwargs)

    return wrapper


async def delete_last_step_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_id = context.user_data.pop("last_step_message_id", None)

    if not message_id:
        return

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=message_id,
        )
    except Exception as e:
        logger.error(f"Erro ao apagar mensagem da etapa anterior: {e}")


def save_step_message_id_from_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query and update.callback_query.message:
        context.user_data["last_step_message_id"] = update.callback_query.message.message_id


async def send_pending_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await asyncio.sleep(PENDING_REMINDER_SECONDS)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ <b>Sua verificação está pendente.</b>\n\n"
                "Você iniciou o processo, mas ainda não concluiu todas as etapas. "
                "Continue enviando o que foi solicitado para finalizar sua verificação."
            ),
            parse_mode=ParseMode.HTML,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Erro ao enviar lembrete de verificação pendente: {e}")


def cancel_pending_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    task = context.user_data.get("pending_reminder_task")

    if task and not task.done():
        task.cancel()

    context.user_data.pop("pending_reminder_task", None)


def schedule_pending_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cancel_pending_reminder(context)

    chat_id = update.effective_chat.id

    context.user_data["pending_reminder_task"] = asyncio.create_task(
        send_pending_reminder(context, chat_id)
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("QUERO SER VERIFICADA 🔒", callback_data="start_verification")]
    ])


def terms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CONCORDO E DESEJO CONTINUAR", callback_data="agree_terms")]
    ])


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Feminino", callback_data="gender_Feminino"),
            InlineKeyboardButton("Masculino", callback_data="gender_Masculino"),
        ],
        [
            InlineKeyboardButton("Trans Feminino", callback_data="gender_Trans Feminino"),
            InlineKeyboardButton("Trans Masculino", callback_data="gender_Trans Masculino"),
        ],
        [
            InlineKeyboardButton("Não-binário", callback_data="gender_Não-binário"),
        ],
        [
            InlineKeyboardButton("Prefiro não informar", callback_data="gender_Prefiro não informar"),
        ],
    ])


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Suporte Sexy Prime", url=SUPPORT_URL)]
    ])


def approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Aprovar", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Rejeitar", callback_data=f"reject_{user_id}"),
        ],
        [
            InlineKeyboardButton("🚫 Bloquear", callback_data=f"block_{user_id}"),
        ],
    ])


def is_real_date(date_text: str) -> bool:
    if not DATE_REGEX.match(date_text):
        return False
    try:
        datetime.strptime(date_text, "%d/%m/%Y")
        return True
    except ValueError:
        return False


def calculate_age(date_text: str) -> int:
    birth_date = datetime.strptime(date_text, "%d/%m/%Y").date()
    today = datetime.now().date()

    age = today.year - birth_date.year

    if (today.month, today.day) < (birth_date.month, birth_date.day):
        age -= 1

    return age


def admin_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return f"admin {user.id}"


# =========================
# START
# =========================
@blocked_guard
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    user = update.effective_user
    nome = user.first_name if user and user.first_name else "modelo"

    await update.message.reply_text(
        text=(
            "🔒 <b>Verificação Oficial — Sexy Prime</b>\n\n"
            f"Seja bem-vinda <b>{esc(nome)}</b> ao bot de verificação da agência <b>Sexy Prime</b>.\n\n"
            "Este é o primeiro passo para sua oficialização em nossa agência."
        ),
        reply_markup=start_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    return TERMS


# =========================
# TERMOS
# =========================
@blocked_guard
async def show_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        text=(
            "<b>TERMOS E CONDIÇÕES — SEXY PRIME</b>\n\n"
            "Para manter o alto padrão de qualidade e profissionalismo da <b>Sexy Prime</b>, "
            "todas as modelos devem cumprir rigorosamente as seguintes diretrizes:\n\n"
            "💎 <b>ESTRUTURA DO PERFIL</b>\n"
            "• O perfil deve representar a modelo real.\n"
            "• Não aceitamos perfis fake, animes, desenhos, objetos ou fotos de terceiros.\n"
            "• O uso do card da <b>Sexy Prime</b> na bio é obrigatório.\n\n"
            "🤝 <b>CONDUTA</b>\n"
            "• Manter o perfil atualizado e profissional.\n"
            "• Respeitar a equipe e demais modelos.\n"
            "• Seguir as orientações da administração.\n\n"
            "⚖️ <b>REQUISITOS LEGAIS</b>\n"
            "• Ser maior de 18 anos.\n"
            "• Enviar documento oficial com foto.\n"
            "• Enviar vídeo de confirmação conforme solicitado.\n\n"
            "Se você leu e concorda, clique no botão abaixo."
        ),
        reply_markup=terms_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    return TERMS


# =========================
# GÊNERO
# =========================
@blocked_guard
async def ask_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        text=(
            "🚻 <b>Etapa 1 de 4 — Selecione seu gênero</b>\n\n"
            "Escolha uma das opções abaixo para continuar sua verificação."
        ),
        reply_markup=gender_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    save_step_message_id_from_query(update, context)
    schedule_pending_reminder(update, context)

    return GENERO


@blocked_guard
async def receive_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    gender = query.data.replace("gender_", "", 1)
    context.user_data["gender"] = gender

    await query.edit_message_text(
        text=(
            "📅 <b>Etapa 2 de 4 — Data de Nascimento</b>\n\n"
            f"<b>Gênero selecionado:</b> {esc(gender)}\n\n"
            "Agora envie sua data de nascimento no formato abaixo:\n\n"
            "<code>01/01/2000</code>"
        ),
        parse_mode=ParseMode.HTML,
    )

    save_step_message_id_from_query(update, context)
    schedule_pending_reminder(update, context)

    return DATA


# =========================
# DATA
# =========================
@blocked_guard
async def receive_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_text = update.message.text.strip()

    if not is_real_date(date_text):
        await update.message.reply_text(
            "Formato inválido. Por favor, envie sua data como no exemplo: 01/01/2000",
            parse_mode=ParseMode.HTML,
        )
        return DATA

    await delete_last_step_message(update, context)

    context.user_data["birth_date"] = date_text
    context.user_data["age"] = calculate_age(date_text)

    schedule_pending_reminder(update, context)

    msg = await update.message.reply_text(
        text=(
            "🪪 <b>Etapa 3 de 4 — Documento</b>\n\n"
            "Agora envie uma <b>foto da sua identidade</b> para comprovar a idade.\n\n"
            "<i>Aceitamos apenas imagem nesta etapa.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )

    context.user_data["last_step_message_id"] = msg.message_id

    return FOTO


@blocked_guard
async def invalid_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Formato inválido. Por favor, envie sua data como no exemplo: 01/01/2000",
        parse_mode=ParseMode.HTML,
    )
    return DATA


# =========================
# FOTO DOCUMENTO
# =========================
@blocked_guard
async def receive_document_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1]
    user = update.effective_user

    try:
        image_bytes = await download_photo_bytes(context, photo.file_id)

        if looks_like_explicit_or_nude_image(image_bytes):
            if AUTO_BLOCK_EXPLICIT_DOCUMENT:
                block_user(
                    user_id=user.id,
                    reason="Envio de imagem explícita/inadequada na etapa de documento.",
                    admin_user=user,
                )
                cancel_pending_reminder(context)
                context.user_data.clear()

                try:
                    await context.bot.send_message(
                        chat_id=LOG_GROUP_ID,
                        text=(
                            "🚫 <b>Usuário bloqueado automaticamente</b>\n\n"
                            f"<b>Nome:</b> {esc(user.full_name)}\n"
                            f"<b>ID:</b> <code>{user.id}</code>\n"
                            "<b>Motivo:</b> imagem explícita/inadequada enviada na etapa de documento."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.error(f"Erro ao avisar grupo sobre bloqueio automático: {e}")

                await update.message.reply_text(
                    "🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>",
                    parse_mode=ParseMode.HTML,
                )
                return ConversationHandler.END

            await update.message.reply_text(
                "🚫 <b>A imagem enviada não é aceita como documento.</b>\n\nEnvie uma foto legível do seu documento oficial com foto.",
                parse_mode=ParseMode.HTML,
            )
            return FOTO

        if not looks_like_document_image(image_bytes):
            await update.message.reply_text(
                "⚠️ <b>O arquivo enviado não parece ser um documento oficial legível.</b>\n\nEnvie uma foto nítida do seu RG, CNH ou documento oficial com foto.",
                parse_mode=ParseMode.HTML,
            )
            return FOTO
    except Exception as e:
        logger.error(f"Erro ao validar documento localmente: {e}")
        # Não trava o fluxo se a validação local falhar por erro de rede/download.

    await delete_last_step_message(update, context)

    birth_date = context.user_data.get("birth_date", "não informado")
    age = context.user_data.get("age", "não informado")
    gender = context.user_data.get("gender", "não informado")
    username = f"@{user.username}" if user.username else "sem @username"

    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID,
            text=(
                "<b>Nova solicitação de verificação</b>\n\n"
                f"<b>Nome:</b> {esc(user.full_name)}\n"
                f"<b>Username:</b> {esc(username)}\n"
                f"<b>ID:</b> <code>{user.id}</code>\n"
                f"<b>Gênero:</b> {esc(gender)}\n"
                f"<b>Data de nascimento:</b> <code>{esc(birth_date)}</code>\n"
                f"<b>Idade:</b> {esc(age)} anos\n"
                "<b>Etapa recebida:</b> Documento"
            ),
            parse_mode=ParseMode.HTML,
        )

        await context.bot.send_photo(
            chat_id=LOG_GROUP_ID,
            photo=photo.file_id,
            caption=(
                "<b>Documento recebido</b>\n"
                f"<b>Usuária:</b> {esc(user.full_name)}\n"
                f"<b>ID:</b> <code>{user.id}</code>"
            ),
            parse_mode=ParseMode.HTML,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=60,
        )
    except Exception as e:
        logger.error(f"Erro ao enviar foto/documento para o grupo: {e}")

    msg = await update.message.reply_text(
        text=(
            "🎥 <b>Etapa 4 de 4 — Vídeo de Confirmação</b>\n\n"
            "Agora envie um <b>vídeo seu</b> dizendo exatamente:\n\n"
            "<code>Desejo ser verificada na agência Sexy Prime</code>\n\n"
            "<i>Aceitamos apenas vídeo nesta etapa.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )

    context.user_data["last_step_message_id"] = msg.message_id

    schedule_pending_reminder(update, context)

    return VIDEO


@blocked_guard
async def invalid_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Envio inválido. Por favor, envie apenas uma imagem do seu documento.",
        parse_mode=ParseMode.HTML,
    )
    return FOTO


# =========================
# VÍDEO
# =========================
@blocked_guard
async def receive_verification_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    video = update.message.video

    await delete_last_step_message(update, context)

    user = update.effective_user
    birth_date = context.user_data.get("birth_date", "não informado")
    age = context.user_data.get("age", "não informado")
    gender = context.user_data.get("gender", "não informado")
    username = f"@{user.username}" if user.username else "sem @username"

    try:
        await context.bot.send_video(
            chat_id=LOG_GROUP_ID,
            video=video.file_id,
            caption=(
                "<b>Solicitação de verificação pendente</b>\n\n"
                f"<b>Nome:</b> {esc(user.full_name)}\n"
                f"<b>Username:</b> {esc(username)}\n"
                f"<b>ID:</b> <code>{user.id}</code>\n"
                f"<b>Gênero:</b> {esc(gender)}\n"
                f"<b>Data de nascimento:</b> <code>{esc(birth_date)}</code>\n"
                f"<b>Idade:</b> {esc(age)} anos\n\n"
                "<b>Ação:</b> escolha abaixo se deseja aprovar ou rejeitar."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=approval_keyboard(user.id),
            read_timeout=180,
            write_timeout=180,
            connect_timeout=180,
        )
    except Exception as e:
        logger.error(f"Erro ao enviar vídeo para o grupo: {e}")

    await update.message.reply_text(
        text=(
            "✅ <b>Verificação enviada!</b>\n\n"
            "Seus dados foram encaminhados para análise. Aguarde a aprovação da nossa equipe."
        ),
        parse_mode=ParseMode.HTML,
    )

    cancel_pending_reminder(context)
    context.user_data.clear()
    return ConversationHandler.END


@blocked_guard
async def invalid_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Envio inválido. Por favor, envie apenas um arquivo de vídeo.",
        parse_mode=ParseMode.HTML,
    )
    return VIDEO


# =========================
# APROVAÇÃO / REJEIÇÃO
# =========================
async def handle_admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    if not is_admin_user(update.effective_user):
        await query.answer("Você não tem permissão para executar esta ação.", show_alert=True)
        return

    if data.startswith("approve_"):
        action = "approve"
        user_id = int(data.replace("approve_", "", 1))
    elif data.startswith("reject_"):
        action = "reject"
        user_id = int(data.replace("reject_", "", 1))
    elif data.startswith("block_"):
        action = "block"
        user_id = int(data.replace("block_", "", 1))
    else:
        return

    admin_display = admin_name(update.effective_user)

    try:
        if action == "approve":
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ <b>Sua verificação foi aprovada!</b>\n\n"
                    "Toque no botão abaixo para chamar o suporte da Sexy Prime para montagem do card."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=support_keyboard(),
            )

            processed_text = (
                "<b>Solicitação processada</b>\n\n"
                "<b>Status:</b> ✅ Aprovada\n"
                f"<b>Por:</b> {esc(admin_display)}"
            )
        elif action == "reject":
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ <b>Infelizmente seu perfil não atende aos requisitos da nossa agência no momento.</b>",
                parse_mode=ParseMode.HTML,
            )

            processed_text = (
                "<b>Solicitação processada</b>\n\n"
                "<b>Status:</b> ❌ Rejeitada\n"
                f"<b>Por:</b> {esc(admin_display)}"
            )
        else:
            block_user(
                user_id=user_id,
                reason="Bloqueado pela administração durante a verificação.",
                admin_user=update.effective_user,
            )

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Erro ao avisar usuário bloqueado: {e}")

            processed_text = (
                "<b>Solicitação processada</b>\n\n"
                "<b>Status:</b> 🚫 Bloqueada\n"
                f"<b>Por:</b> {esc(admin_display)}"
            )

        await query.edit_message_caption(
            caption=processed_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    except Exception as e:
        logger.error(f"Erro ao processar aprovação/rejeição: {e}")
        await query.answer("Não foi possível processar esta solicitação.", show_alert=True)


# =========================
# CANCELAMENTO
# =========================
@blocked_guard
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ <b>Processo cancelado.</b>\n\nQuando quiser recomeçar, envie /start.",
        parse_mode=ParseMode.HTML,
    )

    cancel_pending_reminder(context)
    context.user_data.clear()
    return ConversationHandler.END




# =========================
# COMANDOS ADMIN
# =========================
async def admin_block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use: /bloquear ID_DO_USUARIO")
        return

    target_user_id = int(context.args[0])
    block_user(
        user_id=target_user_id,
        reason="Bloqueado manualmente pela administração.",
        admin_user=user,
    )

    await update.message.reply_text(
        f"🚫 Usuário <code>{target_user_id}</code> bloqueado com sucesso.",
        parse_mode=ParseMode.HTML,
    )


async def admin_unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use: /desbloquear ID_DO_USUARIO")
        return

    target_user_id = int(context.args[0])
    removed = unblock_user(target_user_id)

    if removed:
        await update.message.reply_text(
            f"✅ Usuário <code>{target_user_id}</code> desbloqueado com sucesso.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"O usuário <code>{target_user_id}</code> não estava bloqueado.",
            parse_mode=ParseMode.HTML,
        )


async def admin_blocked_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    blocked_users = load_blocked_users()

    if not blocked_users:
        await update.message.reply_text("Nenhum usuário bloqueado no momento.")
        return

    lines = ["🚫 <b>Usuários bloqueados</b>\n"]

    for user_id, data in blocked_users.items():
        lines.append(
            f"<code>{esc(user_id)}</code> — {esc(data.get('reason', 'sem motivo'))} "
            f"por {esc(data.get('blocked_by', 'admin'))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use: /addadmin ID_DO_ADMIN")
        return

    target_user_id = int(context.args[0])
    add_admin(target_user_id)

    await update.message.reply_text(
        f"✅ Admin <code>{target_user_id}</code> adicionado com sucesso.",
        parse_mode=ParseMode.HTML,
    )


async def admin_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use: /deladmin ID_DO_ADMIN")
        return

    target_user_id = int(context.args[0])
    removed = remove_admin(target_user_id)

    if removed:
        await update.message.reply_text(
            f"✅ Admin <code>{target_user_id}</code> removido com sucesso.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"O ID <code>{target_user_id}</code> não estava na lista de admins extras.",
            parse_mode=ParseMode.HTML,
        )


async def admin_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not is_admin_user(user):
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    admins = all_admin_ids()

    if not admins:
        await update.message.reply_text(
            "Nenhum admin fixo configurado. Enquanto ADMIN_IDS estiver vazio, qualquer pessoa consegue usar comandos admin. Configure ADMIN_IDS no Render.",
        )
        return

    lines = ["👑 <b>Admins do bot</b>\n"]
    for admin_id in sorted(admins):
        lines.append(f"<code>{admin_id}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# =========================
# ERROS
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro durante o processamento:", exc_info=context.error)


# =========================
# WEBHOOK SERVER
# =========================
async def healthcheck(request: web.Request) -> web.Response:
    return web.Response(text="Bot Sexy Prime online", status=200)


async def telegram_webhook(request: web.Request) -> web.Response:
    application: Application = request.app["bot_app"]

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.exception(f"Erro ao processar webhook: {e}")

    return web.Response(text="OK", status=200)


# =========================
# MAIN
# =========================
async def main():
    port = int(os.environ.get("PORT", 10000))

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TERMS: [
                CallbackQueryHandler(show_terms, pattern="^start_verification$"),
                CallbackQueryHandler(ask_gender, pattern="^agree_terms$"),
            ],
            GENERO: [
                CallbackQueryHandler(receive_gender, pattern=r"^gender_.+"),
            ],
            DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_birth_date),
                MessageHandler(~filters.TEXT, invalid_birth_date),
            ],
            FOTO: [
                MessageHandler(filters.PHOTO, receive_document_photo),
                MessageHandler(~filters.PHOTO, invalid_document),
            ],
            VIDEO: [
                MessageHandler(filters.VIDEO, receive_verification_video),
                MessageHandler(~filters.VIDEO, invalid_video),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("bloquear", admin_block_command))
    application.add_handler(CommandHandler("desbloquear", admin_unblock_command))
    application.add_handler(CommandHandler("bloqueados", admin_blocked_list_command))
    application.add_handler(CommandHandler("addadmin", admin_add_command))
    application.add_handler(CommandHandler("deladmin", admin_remove_command))
    application.add_handler(CommandHandler("admins", admin_list_command))
    application.add_handler(
        CallbackQueryHandler(handle_admin_approval, pattern=r"^(approve|reject|block)_\d+$")
    )
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)

    if WEBHOOK_URL:
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")
    else:
        logger.error("WEBHOOK_URL não encontrada. Configure WEBHOOK_URL no Render.")

    await application.start()

    web_app = web.Application()
    web_app["bot_app"] = application
    web_app.router.add_get("/", healthcheck)
    web_app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=port)

    try:
        await site.start()
        logger.info(f"Servidor web iniciado na porta {port}")
        logger.info(f"Webhook configurado em {WEBHOOK_URL}{WEBHOOK_PATH}")

        while True:
            await asyncio.sleep(3600)

    except OSError as e:
        logger.error(f"Erro ao vincular a porta PORT={port} no Render: {e}")
        raise

    finally:
        logger.info("Encerrando aplicação...")
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
