import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime
from typing import Final

from aiohttp import ClientSession, web
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
SXP_SITE_URL: Final[str] = os.getenv("SXP_SITE_URL", "").rstrip("/")
SXP_REFERRAL_SECRET: Final[str] = os.getenv("SXP_REFERRAL_SECRET", "")

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
        ]
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


def extract_referral_code(context: ContextTypes.DEFAULT_TYPE) -> str:
    if not context.args:
        return ""

    raw = str(context.args[0]).strip()
    if raw.startswith("ref_"):
        raw = raw[4:]

    raw = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return raw[:32]


async def notify_site_referral_conversion(context: ContextTypes.DEFAULT_TYPE, user_id: int, full_name: str, username: str, referral_code: str) -> None:
    if not referral_code:
        return

    if not SXP_SITE_URL or not SXP_REFERRAL_SECRET:
        logger.warning("Indicação detectada, mas SXP_SITE_URL ou SXP_REFERRAL_SECRET não estão configurados.")
        return

    payload = {
        "secret": SXP_REFERRAL_SECRET,
        "code": referral_code,
        "telegram_user_id": str(user_id),
        "full_name": full_name,
        "username": username,
        "source": "telegram_verification_bot",
    }

    try:
        async with ClientSession() as session:
            async with session.post(
                f"{SXP_SITE_URL}/api/referral_conversion.php",
                json=payload,
                timeout=25,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    logger.error(f"Erro ao registrar indicação no site: HTTP {response.status} | {text}")
                    return
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"raw": text}
                logger.info(f"Retorno da indicação: {data}")
    except Exception as e:
        logger.error(f"Falha ao chamar API de indicação: {e}")


# =========================
# START
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    referral_code = extract_referral_code(context)
    if referral_code:
        context.user_data["referral_code"] = referral_code

    user = update.effective_user
    nome = user.first_name if user and user.first_name else "modelo"

    await update.message.reply_text(
        text=(
            "🔒 <b>Verificação Oficial — Sexy Prime</b>\n\n"
            f"Seja bem-vinda <b>{esc(nome)}</b> ao bot de verificação da agência <b>Sexy Prime</b>.\n\n"
            "Este é o primeiro passo para sua oficialização em nossa agência."
            + (f"\n\n🎁 <b>Indicação recebida:</b> <code>{esc(referral_code)}</code>" if referral_code else "")
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
    photo = update.message.photo[-1]

    await delete_last_step_message(update, context)

    user = update.effective_user
    birth_date = context.user_data.get("birth_date", "não informado")
    age = context.user_data.get("age", "não informado")
    gender = context.user_data.get("gender", "não informado")
    username = f"@{user.username}" if user.username else "sem @username"
    referral_code = context.user_data.get("referral_code", "")

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
    video = update.message.video

    await delete_last_step_message(update, context)

    user = update.effective_user
    birth_date = context.user_data.get("birth_date", "não informado")
    age = context.user_data.get("age", "não informado")
    gender = context.user_data.get("gender", "não informado")
    username = f"@{user.username}" if user.username else "sem @username"
    referral_code = context.user_data.get("referral_code", "")

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
                f"<b>Idade:</b> {esc(age)} anos\n"
                f"<b>Indicação:</b> {esc(referral_code) if referral_code else 'sem indicação'}\n\n"
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


    if referral_code:
        context.application.bot_data[f"referral_{user.id}"] = {
            "code": referral_code,
            "full_name": user.full_name or "",
            "username": f"@{user.username}" if user.username else "",
            "created_at": datetime.now().isoformat(),
        }

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
# APROVAÇÃO / REJEIÇÃO
# =========================
async def handle_admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("approve_"):
        action = "approve"
        user_id = int(data.replace("approve_", "", 1))
    elif data.startswith("reject_"):
        action = "reject"
        user_id = int(data.replace("reject_", "", 1))
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


            referral_info = context.application.bot_data.pop(f"referral_{user_id}", {})
            referral_code = str(referral_info.get("code", ""))
            if referral_code:
                await notify_site_referral_conversion(
                    context=context,
                    user_id=user_id,
                    full_name=str(referral_info.get("full_name", "")),
                    username=str(referral_info.get("username", "")),
                    referral_code=referral_code,
                )

            processed_text = (
                "<b>Solicitação processada</b>\n\n"
                "<b>Status:</b> ✅ Aprovada\n"
                f"<b>Por:</b> {esc(admin_display)}"
            )
        else:
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

    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(handle_admin_approval, pattern=r"^(approve|reject)_\d+$")
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
