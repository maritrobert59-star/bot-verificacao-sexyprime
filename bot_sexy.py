import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime
from typing import Final

from aiohttp import web
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

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_FILE: Final[str] = "admins.json"
BLOCKED_FILE: Final[str] = "blocked_users.json"

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


def load_json_list(path: str) -> set[int]:
    try:
        if not os.path.exists(path):
            return set()
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return {int(item) for item in data}
    except Exception as e:
        logger.error(f"Erro ao carregar {path}: {e}")
        return set()


def save_json_list(path: str, values: set[int]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(sorted(values), file, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erro ao salvar {path}: {e}")


def get_admin_ids() -> set[int]:
    admins = load_json_list(ADMIN_FILE)
    if OWNER_ID:
        admins.add(OWNER_ID)
    return admins


def get_blocked_ids() -> set[int]:
    return load_json_list(BLOCKED_FILE)


def is_admin_user(user_id: int) -> bool:
    return int(user_id) in get_admin_ids()


def is_blocked_user(user_id: int) -> bool:
    return int(user_id) in get_blocked_ids()


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Administradores", callback_data="panel_admins")],
        [InlineKeyboardButton("🚫 Bloqueados", callback_data="panel_blocked")],
        [InlineKeyboardButton("📊 Estatísticas", callback_data="panel_stats")],
    ])


async def deny_if_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or not is_blocked_user(user.id):
        return False

    text = "🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>"

    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML)

    cancel_pending_reminder(context)
    context.user_data.clear()
    return True


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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    if await deny_if_blocked(update, context):
        return ConversationHandler.END

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


async def receive_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_blocked(update, context):
        return ConversationHandler.END

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
async def receive_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_blocked(update, context):
        return ConversationHandler.END

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


async def invalid_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Formato inválido. Por favor, envie sua data como no exemplo: 01/01/2000",
        parse_mode=ParseMode.HTML,
    )
    return DATA


# =========================
# FOTO DOCUMENTO
# =========================
async def receive_document_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_blocked(update, context):
        return ConversationHandler.END

    photo = update.message.photo[-1]

    await delete_last_step_message(update, context)

    user = update.effective_user
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


async def invalid_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Envio inválido. Por favor, envie apenas uma imagem do seu documento.",
        parse_mode=ParseMode.HTML,
    )
    return FOTO


# =========================
# VÍDEO
# =========================
async def receive_verification_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_blocked(update, context):
        return ConversationHandler.END

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


async def invalid_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Envio inválido. Por favor, envie apenas um arquivo de vídeo.",
        parse_mode=ParseMode.HTML,
    )
    return VIDEO



# =========================
# PAINEL / ADMINISTRAÇÃO
# =========================
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        text=f"Seu ID é: <code>{user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text(
            "Acesso negado.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        text=(
            "⚙️ <b>Painel Administrativo — Sexy Prime</b>\n\n"
            "Escolha uma opção abaixo.\n\n"
            "Comandos rápidos:\n"
            "<code>/addadmin ID</code>\n"
            "<code>/deladmin ID</code>\n"
            "<code>/bloquear ID</code>\n"
            "<code>/desbloquear ID</code>"
        ),
        reply_markup=admin_panel_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def handle_admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await query.answer("Acesso negado.", show_alert=True)
        return

    admins = get_admin_ids()
    blocked = get_blocked_ids()

    if query.data == "panel_admins":
        lista = "\n".join(f"• <code>{admin_id}</code>" for admin_id in sorted(admins)) or "Nenhum admin cadastrado."
        text = (
            "👑 <b>Administradores</b>\n\n"
            f"{lista}\n\n"
            "Para adicionar: <code>/addadmin ID</code>\n"
            "Para remover: <code>/deladmin ID</code>"
        )
    elif query.data == "panel_blocked":
        lista = "\n".join(f"• <code>{blocked_id}</code>" for blocked_id in sorted(blocked)) or "Nenhum usuário bloqueado."
        text = (
            "🚫 <b>Usuários bloqueados</b>\n\n"
            f"{lista}\n\n"
            "Para bloquear: <code>/bloquear ID</code>\n"
            "Para desbloquear: <code>/desbloquear ID</code>"
        )
    elif query.data == "panel_stats":
        text = (
            "📊 <b>Estatísticas</b>\n\n"
            f"Admins cadastrados: <b>{len(admins)}</b>\n"
            f"Usuários bloqueados: <b>{len(blocked)}</b>"
        )
    else:
        return

    await query.edit_message_text(
        text=text,
        reply_markup=admin_panel_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    if not context.args:
        await update.message.reply_text("Use: <code>/addadmin ID</code>", parse_mode=ParseMode.HTML)
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    admins = load_json_list(ADMIN_FILE)
    admins.add(new_admin_id)
    save_json_list(ADMIN_FILE, admins)

    await update.message.reply_text(
        f"✅ Admin adicionado: <code>{new_admin_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    if not context.args:
        await update.message.reply_text("Use: <code>/deladmin ID</code>", parse_mode=ParseMode.HTML)
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    if OWNER_ID and admin_id == OWNER_ID:
        await update.message.reply_text("O OWNER_ID não pode ser removido por comando.")
        return

    admins = load_json_list(ADMIN_FILE)
    admins.discard(admin_id)
    save_json_list(ADMIN_FILE, admins)

    await update.message.reply_text(
        f"✅ Admin removido: <code>{admin_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    admins = get_admin_ids()
    lista = "\n".join(f"• <code>{admin_id}</code>" for admin_id in sorted(admins)) or "Nenhum admin cadastrado."

    await update.message.reply_text(
        text=f"👑 <b>Administradores</b>\n\n{lista}",
        parse_mode=ParseMode.HTML,
    )


async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    if not context.args:
        await update.message.reply_text("Use: <code>/bloquear ID</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    blocked = get_blocked_ids()
    blocked.add(target_id)
    save_json_list(BLOCKED_FILE, blocked)

    await update.message.reply_text(
        f"🚫 Usuário bloqueado: <code>{target_id}</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Não foi possível avisar usuário bloqueado: {e}")


async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    if not context.args:
        await update.message.reply_text("Use: <code>/desbloquear ID</code>", parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    blocked = get_blocked_ids()
    blocked.discard(target_id)
    save_json_list(BLOCKED_FILE, blocked)

    await update.message.reply_text(
        f"✅ Usuário desbloqueado: <code>{target_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def list_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        await update.message.reply_text("Acesso negado.")
        return

    blocked = get_blocked_ids()
    lista = "\n".join(f"• <code>{blocked_id}</code>" for blocked_id in sorted(blocked)) or "Nenhum usuário bloqueado."

    await update.message.reply_text(
        text=f"🚫 <b>Usuários bloqueados</b>\n\n{lista}",
        parse_mode=ParseMode.HTML,
    )


# =========================
# APROVAÇÃO / REJEIÇÃO
# =========================
async def handle_admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not update.effective_user or not is_admin_user(update.effective_user.id):
        await query.answer("Acesso negado.", show_alert=True)
        return

    data = query.data

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
            blocked = get_blocked_ids()
            blocked.add(user_id)
            save_json_list(BLOCKED_FILE, blocked)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🚫 <b>Seu acesso ao bot foi bloqueado pela administração.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Não foi possível avisar usuário bloqueado: {e}")

            processed_text = (
                "<b>Solicitação processada</b>\n\n"
                "<b>Status:</b> 🚫 Bloqueada\n"
                f"<b>Por:</b> {esc(admin_display)}"
            )

        if query.message and query.message.caption is not None:
            await query.edit_message_caption(
                caption=processed_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        elif query.message:
            await query.edit_message_text(
                text=processed_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )

    except Exception as e:
        logger.error(f"Erro ao processar aprovação/rejeição: {e}")
        await query.answer("Não foi possível processar esta solicitação.", show_alert=True)


# =========================
# CANCELAMENTO
# =========================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ <b>Processo cancelado.</b>\n\nQuando quiser recomeçar, envie /start.",
        parse_mode=ParseMode.HTML,
    )

    cancel_pending_reminder(context)
    context.user_data.clear()
    return ConversationHandler.END


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

    application.add_handler(CallbackQueryHandler(handle_admin_approval, pattern=r"^(approve|reject|block)_\d+$"), group=-1)
    application.add_handler(CommandHandler("meuid", my_id))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("deladmin", del_admin))
    application.add_handler(CommandHandler("admins", list_admins))
    application.add_handler(CommandHandler("bloquear", block_user_command))
    application.add_handler(CommandHandler("desbloquear", unblock_user_command))
    application.add_handler(CommandHandler("bloqueados", list_blocked))
    application.add_handler(CallbackQueryHandler(handle_admin_panel_callback, pattern=r"^panel_"))
    application.add_handler(conv_handler)
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
