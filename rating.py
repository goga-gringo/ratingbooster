######################################################
# 1. ИМПОРТЫ И НАСТРОЙКИ
######################################################

import asyncio
import logging
import os
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

# Загружаем .env файл (для локального запуска)
load_dotenv()

TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "rating_bot.db")

router = Router()


######################################################
# 2. ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ БАЗЫ ДАННЫХ
######################################################


def get_db():
    # Автоматически создаем папку для базы данных (нужно для /data/ на Railway)
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Создаем таблицы, если их еще нет
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        lang TEXT DEFAULT 'ru',
        current_table TEXT DEFAULT NULL,
        is_banned INTEGER DEFAULT 0,
        last_msg_ids TEXT DEFAULT '',
        notifications_enabled INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS deals (
        deal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        gift TEXT,
        buyer_table TEXT DEFAULT NULL,
        status TEXT DEFAULT 'pending',
        completed_at TIMESTAMP DEFAULT NULL
    )
    """)

    # 2. Автоматическая миграция колонок для имеющихся баз
    cursor.execute("PRAGMA table_info(users)")
    u_columns = [col[1] for col in cursor.fetchall()]

    if "last_msg_ids" not in u_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN last_msg_ids TEXT DEFAULT ''"
        )
    if "notifications_enabled" not in u_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN notifications_enabled INTEGER DEFAULT 1"
        )
    if "created_at" not in u_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP")
        cursor.execute(
            "UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )

    cursor.execute("PRAGMA table_info(deals)")
    d_columns = [col[1] for col in cursor.fetchall()]
    if "buyer_table" not in d_columns:
        cursor.execute(
            "ALTER TABLE deals ADD COLUMN buyer_table TEXT DEFAULT NULL"
        )

    conn.commit()
    conn.close()


init_db()


######################################################
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И РАБОТА С БД
######################################################


def get_saved_msg_ids(user_id: int) -> list[int]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT last_msg_ids FROM users WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return [int(x) for x in row[0].split(",") if x.isdigit()]
    return []


def save_msg_ids(user_id: int, msg_ids: list[int]):
    conn = get_db()
    cursor = conn.cursor()
    ids_str = ",".join(str(i) for i in msg_ids)
    cursor.execute(
        "UPDATE users SET last_msg_ids = ? WHERE user_id = ?",
        (ids_str, user_id),
    )
    conn.commit()
    conn.close()


def get_lang_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇷🇺 Русский"), KeyboardButton(text="🇬🇧 English")]
        ],
        resize_keyboard=True,
    )


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 Общая комната")],
            [
                KeyboardButton(text="👤 Мой профиль"),
                KeyboardButton(text="📜 История сделок"),
            ],
            [
                KeyboardButton(text="⚙️ Настройки"),
                KeyboardButton(text="📖 Правила"),
            ],
        ],
        resize_keyboard=True,
    )


def get_back_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Назад")]],
        resize_keyboard=True,
    )


######################################################
# ТОЧНЫЙ СПИСОК ПОДАРКОВ
######################################################

GIFTS_LIST = [
    "🌹 Роза (25 ⭐️)",
    "❤️ Сердечко (25 ⭐️)",
    "🎁 Подарок (25 ⭐️)",
    "🐻 Мишка (50 ⭐️)",
    "🍰 Торт (50 ⭐️)",
    "💐 Цветы (50 ⭐️)",
    "🍾 Шампанское (50 ⭐️)",
    "🚀 Ракета (100 ⭐️)",
    "💍 Кольцо (100 ⭐️)",
    "💎 Кристалл (100 ⭐️)",
    "🏆 Кубок (100 ⭐️)",
]


def get_room_empty_keyboard():
    keyboard = []
    row = []

    # Кнопки по 2 в ряд с текстом "🪑 Сесть [Подарок]"
    for gift in GIFTS_LIST:
        row.append(KeyboardButton(text=f"🪑 Сесть {gift}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([KeyboardButton(text="🔙 Назад")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_room_sitting_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚪 Выйти из-за стола")],
            [KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True,
    )


def get_active_deal(user_id: int):
    """Возвращает активную сделку или открытый спор."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT deal_id, buyer_id, seller_id, gift, status 
        FROM deals 
        WHERE (buyer_id = ? OR seller_id = ?) 
          AND status IN ('pending', 'active', 'waiting_confirm', 'disputed')
        ORDER BY deal_id DESC LIMIT 1
    """,
        (user_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def get_rank_info(completed_deals: int) -> tuple:
    if completed_deals < 5:
        return "Новичок", "🆕"
    elif completed_deals < 20:
        return "Торговец", "💼"
    elif completed_deals < 50:
        return "Коммерсант", "💰"
    elif completed_deals < 100:
        return "Бизнесмен", "🎩"
    elif completed_deals < 200:
        return "Магнат", "👑"
    elif completed_deals < 400:
        return "Олигарх", "💎"
    else:
        return "Легенда", "⚡"


def get_user_stats(user_id: int) -> tuple:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*) FROM deals 
        WHERE (buyer_id = ? OR seller_id = ?) 
          AND status = 'completed' 
          AND completed_at <= datetime('now', '-21 days')
    """,
        (user_id, user_id),
    )
    matured_deals = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*) FROM deals 
        WHERE (buyer_id = ? OR seller_id = ?) 
          AND status = 'completed' 
          AND completed_at > datetime('now', '-21 days')
    """,
        (user_id, user_id),
    )
    hold_deals = cursor.fetchone()[0]

    conn.close()
    return matured_deals, hold_deals


def get_user_detailed_stats(user_id: int) -> tuple:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT 
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_count,
            SUM(CASE WHEN status IN ('disputed', 'closed_by_admin') THEN 1 ELSE 0 END) as dispute_count,
            SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled_count
        FROM deals
        WHERE buyer_id = ? OR seller_id = ?
    """,
        (user_id, user_id),
    )

    row = cursor.fetchone()
    conn.close()

    completed = row[0] if row and row[0] else 0
    disputes = row[1] if row and row[1] else 0
    cancelled = row[2] if row and row[2] else 0

    return completed, disputes, cancelled


def is_user_banned(user_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


async def replace_screen(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: ReplyKeyboardMarkup = None,
    ikb: InlineKeyboardMarkup = None,
    user_message: Message = None,
):
    """
    Единая точка "переключения страницы" для всего бота.
    Логика простая, как переход между экранами:
      1. Сначала показываем новый экран (чтобы в чате не было пустоты).
      2. Затем ОДНИМ запросом удаляем и старый экран, и нажатие пользователя.
    За одно переключение отправляется максимум 2 сообщения бота
    (основной текст + подвал с кнопками "Назад"/меню), независимо от того,
    сколько данных внутри — это и убирает "вереницу сообщений".
    """
    if is_user_banned(user_id):
        if user_message:
            try:
                await user_message.answer(
                    "⛔ Вы заблокированы за нарушение правил сервиса."
                )
            except Exception:
                pass
        return

    old_msg_ids = get_saved_msg_ids(user_id)
    new_msg_ids = []

    # 1. Отправляем новый "экран"
    main_markup = ikb if ikb else reply_markup
    msg = await bot.send_message(
        user_id, text, reply_markup=main_markup, parse_mode="Markdown"
    )
    new_msg_ids.append(msg.message_id)

    if ikb and reply_markup:
        msg_footer = await bot.send_message(
            user_id, "👇 Выберите действие:", reply_markup=reply_markup
        )
        new_msg_ids.append(msg_footer.message_id)

    # 2. Удаляем старый экран + сообщение пользователя ОДНИМ пакетным запросом
    ids_to_delete = list(old_msg_ids)
    if user_message:
        ids_to_delete.append(user_message.message_id)

    if ids_to_delete:
        try:
            await bot.delete_messages(chat_id=user_id, message_ids=ids_to_delete)
        except Exception:
            # Фолбэк для старых версий aiogram/Bot API без пакетного удаления
            for mid in ids_to_delete:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=mid)
                except Exception:
                    pass

    # 3. Сохраняем ID нового экрана
    save_msg_ids(user_id, new_msg_ids)


async def send_single(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
    ikb: InlineKeyboardMarkup = None,
):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        await message.answer(
            "⛔ Вы заблокированы за нарушение правил сервиса."
        )
        return
    await replace_screen(
        message.bot,
        user_id,
        text,
        reply_markup=reply_markup,
        ikb=ikb,
        user_message=message,
    )


######################################################
# 4. ОБНОВЛЕНИЕ ОБЩЕЙ КОМНАТЫ
######################################################


async def refresh_user_room(
    bot: Bot, storage, user_id: int, user_message: Message = None
):
    if is_user_banned(user_id):
        return

    active_deal = get_active_deal(user_id)
    if active_deal:
        deal_id, buyer_id, seller_id, gift, status = active_deal

        conn = get_db()
        cursor = conn.cursor()
        other_id = seller_id if user_id == buyer_id else buyer_id
        cursor.execute(
            "SELECT username, first_name FROM users WHERE user_id = ?",
            (other_id,),
        )
        other_row = cursor.fetchone()
        conn.close()

        other_name = (
            f"@{other_row[0]}"
            if other_row and other_row[0]
            else (other_row[1] if other_row else f"ID: {other_id}")
        )

        card_text = ""
        ikb = None

        if status == "pending":
            if user_id == seller_id:
                b_completed, b_disputes, b_cancelled = get_user_detailed_stats(
                    buyer_id
                )

                card_text = (
                    f"📩 **Вам предложили сделку №{deal_id}!**\n\n"
                    f"📦 **Товар/Стол:** {gift}\n"
                    f"👤 **Покупатель:** {other_name}\n\n"
                    f"📊 **Статистика покупателя:**\n"
                    f"├ ✅ Успешных сделок: **{b_completed}**\n"
                    f"├ 🚨 Споров: **{b_disputes}**\n"
                    f"└ ❌ Отменённых сделок: **{b_cancelled}**\n\n"
                    f"Вы хотите принять предложение?"
                )
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Принять",
                                callback_data=f"accept_deal_{deal_id}",
                            ),
                            InlineKeyboardButton(
                                text="❌ Отклонить",
                                callback_data=f"decline_deal_{deal_id}",
                            ),
                        ]
                    ]
                )
            else:
                card_text = f"⏳ **Сделка №{deal_id} в ожидании**\n\nВы предложили сделку за **{gift}** пользователю {other_name}.\nОжидаем ответа продавца..."
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="❌ Отменить сделку",
                                callback_data=f"decline_deal_{deal_id}",
                            )
                        ]
                    ]
                )

        elif status == "active":
            if user_id == buyer_id:
                card_text = f"🤝 **Сделка №{deal_id} активна!**\n\nОтправьте **{gift}** пользователю {other_name} в Telegram и нажмите кнопку ниже:"
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="↗️ Я отправил подарок",
                                callback_data=f"sent_gift_{deal_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🚨 Претензия",
                                callback_data=f"dispute_{deal_id}",
                            )
                        ],
                    ]
                )
            else:
                card_text = f"🤝 **Сделка №{deal_id} активна!**\n\nОжидаем отправки товара **{gift}** от покупателя {other_name}."
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🚨 Претензия",
                                callback_data=f"dispute_{deal_id}",
                            )
                        ]
                    ]
                )

        elif status == "waiting_confirm":
            if user_id == seller_id:
                card_text = f"🎁 **Сделка №{deal_id}**\n\nПокупатель {other_name} отметил, что отправил вам **{gift}**!\nВы получили подарок?"
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Подтвердить получение",
                                callback_data=f"confirm_deal_{deal_id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="⚠️ Претензия (Не получил)",
                                callback_data=f"dispute_{deal_id}",
                            )
                        ],
                    ]
                )
            else:
                card_text = f"⏳ **Сделка №{deal_id}**\n\nУведомление о передаче **{gift}** отправлено пользователю {other_name}.\nОжидаем подтверждения..."
                ikb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🚨 Претензия",
                                callback_data=f"dispute_{deal_id}",
                            )
                        ]
                    ]
                )

        elif status == "disputed":
            card_text = (
                f"🚨 **ПО СДЕЛКЕ №{deal_id} ОТКРЫТ СПОР!**\n\n"
                f"📦 **Товар:** {gift}\n"
                f"👤 **Участник:** {other_name}\n\n"
                f"⏳ Ваш аккаунт временно ограничен в торговле до решения администратора."
            )
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Наша группа / Поддержка",
                            url="https://t.me/+KXMR9c-XF4E3MWMy",
                        )
                    ]
                ]
            )

        await replace_screen(
            bot,
            user_id,
            card_text,
            reply_markup=get_back_keyboard(),
            ikb=ikb,
            user_message=user_message,
        )
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT first_name, username, user_id, current_table FROM users WHERE current_table IS NOT NULL"
    )
    sitters = cursor.fetchall()
    conn.close()

    header_text = "******************************************************\n\n                                     ОБЩИЙ СТОЛ:\n\n*******************************************************"
    text_blocks = [header_text]

    is_sitting = False
    offer_buttons = []

    if not sitters:
        text_blocks.append("                                            ПУСТО")
    else:
        for row in sitters:
            f_name, u_name, uid, table = row
            if uid == user_id:
                is_sitting = True

            display_user = f"@{u_name}" if u_name else f"ID: {uid}"
            matured_deals, _ = get_user_stats(uid)
            rank_name, rank_emoji = get_rank_info(matured_deals)

            card_text = f"*************\n🟢 {f_name} ({display_user}) | Сделки: {matured_deals} | {rank_emoji} {rank_name} | {table}\n*************"
            text_blocks.append(card_text)

            if uid != user_id:
                offer_buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"🤝 Сделка — {f_name}",
                            callback_data=f"offer_deal_{uid}",
                        )
                    ]
                )

    full_text = "\n\n".join(text_blocks)
    room_ikb = InlineKeyboardMarkup(inline_keyboard=offer_buttons) if offer_buttons else None

    kb = (
        get_room_sitting_keyboard() if is_sitting else get_room_empty_keyboard()
    )

    await replace_screen(
        bot,
        user_id,
        full_text,
        reply_markup=kb,
        ikb=room_ikb,
        user_message=user_message,
    )


######################################################
# 5. ХЭНДЛЕРЫ: СТАРТ И МЕНЮ
######################################################


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return

    conn = get_db()
    cursor = conn.cursor()
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Без имени"
    username = message.from_user.username

    cursor.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
            (user_id, first_name, username),
        )
        conn.commit()
        await send_single(
            message,
            state,
            "🌍 Привет! Выбери язык:\n\n🌍 Hello! Choose your language:",
            get_lang_keyboard(),
        )
    else:
        cursor.execute(
            "UPDATE users SET first_name = ?, username = ? WHERE user_id = ?",
            (first_name, username, user_id),
        )
        conn.commit()
        await send_single(
            message,
            state,
            "******************\n\nГЛАВНОЕ МЕНЮ\n\n******************",
            get_main_keyboard(),
        )
    conn.close()


@router.message(F.text.in_(["🇷🇺 Русский", "🇬🇧 English"]))
async def process_language(message: Message, state: FSMContext):
    conn = get_db()
    cursor = conn.cursor()
    lang = "ru" if message.text == "🇷🇺 Русский" else "en"
    cursor.execute(
        "UPDATE users SET lang = ? WHERE user_id = ?",
        (lang, message.from_user.id),
    )
    conn.commit()
    conn.close()

    await send_single(
        message,
        state,
        "******************\n\nГЛАВНОЕ МЕНЮ\n\n******************",
        get_main_keyboard(),
    )


@router.message(F.text == "🔙 Назад")
async def btn_back(message: Message, state: FSMContext):
    await send_single(
        message,
        state,
        "******************\n\nГЛАВНОЕ МЕНЮ\n\n******************",
        get_main_keyboard(),
    )


@router.message(F.text == "👤 Мой профиль")
async def btn_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT username, first_name, current_table FROM users WHERE user_id = ?",
        (user_id,),
    )
    user_row = cursor.fetchone()
    conn.close()

    username = (
        f"@{user_row[0]}" if (user_row and user_row[0]) else "Не установлен"
    )
    first_name = user_row[1] if user_row and user_row[1] else "Без имени"
    current_table = (
        user_row[2] if (user_row and user_row[2]) else "❌ Не за столом"
    )

    matured_deals, hold_deals = get_user_stats(user_id)
    completed, disputes, cancelled = get_user_detailed_stats(user_id)

    total_attempts = completed + disputes
    reliability = (
        f"{int((completed / total_attempts) * 100)}%"
        if total_attempts > 0
        else "100%"
    )

    rank_name, rank_emoji = get_rank_info(matured_deals)
    hold_str = f" (+{hold_deals} в холде)" if hold_deals > 0 else ""

    bot_username = (await message.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"

    profile_text = (
        f"👤 **ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ**\n"
        f"────────────────────\n"
        f"🆔 **ID:** `{user_id}`\n"
        f"👤 **Имя:** {first_name}\n"
        f"🌐 **Юзернейм:** {username}\n\n"
        f"🏆 **Звание:** {rank_emoji} **{rank_name}**\n"
        f"🛡 **Надежность:** **{reliability}**\n\n"
        f"📊 **Статистика сделок:**\n"
        f"├ ✅ Завершенные: **{matured_deals}**{hold_str}\n"
        f"├ 🚨 Споры: **{disputes}**\n"
        f"└ ❌ Отмененные: **{cancelled}**\n\n"
        f"📍 **Текущий статус:** {current_table}\n"
        f"────────────────────\n"
        f"🔗 **Ваша реферальная ссылка:**\n`{ref_link}`"
    )

    await send_single(message, state, profile_text, get_back_keyboard())


@router.message(F.text == "🌐 Общая комната")
async def btn_room(message: Message, state: FSMContext, bot: Bot):
    await refresh_user_room(
        bot, state.storage, message.from_user.id, user_message=message
    )


@router.message(F.text.startswith("🪑 Сесть"))
async def btn_sit(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if get_active_deal(user_id):
        await send_single(
            message,
            state,
            "⚠️ У вас есть незавершенная сделка или открытый спор! Вы не можете сесть за стол.",
            get_back_keyboard(),
        )
        return

    # Вырезаем префикс "🪑 Сесть ", чтобы сохранить чистое имя подарка
    table_name = message.text.replace("🪑 Сесть ", "").strip()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET current_table = ? WHERE user_id = ?",
        (table_name, user_id),
    )
    conn.commit()
    conn.close()

    await refresh_user_room(bot, state.storage, user_id, user_message=message)


@router.message(F.text == "🚪 Выйти из-за стола")
async def btn_leave_table(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET current_table = NULL WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()

    await refresh_user_room(
        bot, state.storage, user_id, user_message=message
    )


######################################################
# 6. ЛОГИКА P2P СДЕЛОК
######################################################


@router.callback_query(F.data.startswith("offer_deal_"))
async def process_offer_deal(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    buyer_id = callback.from_user.id
    seller_id = int(callback.data.split("_")[2])

    if is_user_banned(buyer_id):
        await callback.answer("Вы заблокированы.", show_alert=True)
        return

    if get_active_deal(buyer_id):
        await callback.answer(
            "У вас есть активная сделка или спор!", show_alert=True
        )
        return

    if get_active_deal(seller_id):
        await callback.answer(
            "У продавца есть активная сделка или спор!", show_alert=True
        )
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT current_table FROM users WHERE user_id = ?", (seller_id,)
    )
    seller_row = cursor.fetchone()

    cursor.execute(
        "SELECT current_table FROM users WHERE user_id = ?", (buyer_id,)
    )
    buyer_row = cursor.fetchone()

    if not seller_row or not seller_row[0]:
        await callback.answer(
            "Этот пользователь уже встал из-за стола!", show_alert=True
        )
        conn.close()
        return

    gift_name = seller_row[0]
    buyer_table_name = buyer_row[0] if buyer_row else None

    cursor.execute(
        "INSERT INTO deals (buyer_id, seller_id, gift, buyer_table, status) VALUES (?, ?, ?, ?, 'pending')",
        (buyer_id, seller_id, gift_name, buyer_table_name),
    )
    cursor.execute(
        "UPDATE users SET current_table = NULL WHERE user_id IN (?, ?)",
        (buyer_id, seller_id),
    )
    conn.commit()
    conn.close()

    await callback.answer("Предложение отправлено!")

    await refresh_user_room(bot, state.storage, buyer_id)
    await refresh_user_room(bot, state.storage, seller_id)


@router.callback_query(F.data.startswith("decline_deal_"))
async def process_decline_deal(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    deal_id = int(callback.data.split("_")[2])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id, gift, buyer_table, status FROM deals WHERE deal_id = ?",
        (deal_id,),
    )
    row = cursor.fetchone()

    if row:
        buyer_id, seller_id, gift, buyer_table, status = row
        cursor.execute(
            "UPDATE deals SET status = 'cancelled' WHERE deal_id = ?",
            (deal_id,),
        )

        if status == "pending":
            cursor.execute(
                "UPDATE users SET current_table = ? WHERE user_id = ?",
                (gift, seller_id),
            )
            if buyer_table:
                cursor.execute(
                    "UPDATE users SET current_table = ? WHERE user_id = ?",
                    (buyer_table, buyer_id),
                )

        conn.commit()
        conn.close()

        await callback.answer("Сделка отменена.")
        await refresh_user_room(bot, state.storage, buyer_id)
        await refresh_user_room(bot, state.storage, seller_id)
    else:
        conn.close()
        await callback.answer("Сделка не найдена.", show_alert=True)


@router.callback_query(F.data.startswith("accept_deal_"))
async def process_accept_deal(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    deal_id = int(callback.data.split("_")[2])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id FROM deals WHERE deal_id = ?", (deal_id,)
    )
    row = cursor.fetchone()

    if row:
        buyer_id, seller_id = row
        cursor.execute(
            "UPDATE deals SET status = 'active' WHERE deal_id = ?", (deal_id,)
        )
        conn.commit()
        conn.close()

        await callback.answer("Сделка принята!")
        await refresh_user_room(bot, state.storage, buyer_id)
        await refresh_user_room(bot, state.storage, seller_id)
    else:
        conn.close()


@router.callback_query(F.data.startswith("sent_gift_"))
async def process_sent_gift(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    deal_id = int(callback.data.split("_")[2])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id FROM deals WHERE deal_id = ?", (deal_id,)
    )
    row = cursor.fetchone()

    if row:
        buyer_id, seller_id = row
        cursor.execute(
            "UPDATE deals SET status = 'waiting_confirm' WHERE deal_id = ?",
            (deal_id,),
        )
        conn.commit()
        conn.close()

        await callback.answer("Уведомление отправлено продавцу!")
        await refresh_user_room(bot, state.storage, buyer_id)
        await refresh_user_room(bot, state.storage, seller_id)
    else:
        conn.close()


@router.callback_query(F.data.startswith("confirm_deal_"))
async def process_confirm_deal(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    deal_id = int(callback.data.split("_")[2])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id, gift FROM deals WHERE deal_id = ?",
        (deal_id,),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        await callback.answer("Сделка не найдена.")
        return

    buyer_id, seller_id, gift = row

    cursor.execute(
        """
        UPDATE deals 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
        WHERE deal_id = ?
    """,
        (deal_id,),
    )
    conn.commit()
    conn.close()

    await callback.answer("✅ Сделка завершена!")

    await refresh_user_room(bot, state.storage, buyer_id)
    await refresh_user_room(bot, state.storage, seller_id)


######################################################
# 7. ИСТОРИЯ СДЕЛОК С СКОЛЬЗЯЩЕЙ ПАГИНАЦИЕЙ
######################################################

PAGE_SIZE = 7


async def render_history_page(user_id: int, page: int = 1):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM deals WHERE buyer_id = ? OR seller_id = ?",
        (user_id, user_id),
    )
    total_deals = cursor.fetchone()[0]

    if total_deals == 0:
        conn.close()
        return "📜 У вас пока нет истории сделок.", None, 0

    total_pages = (total_deals + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE

    cursor.execute(
        """
        SELECT d.deal_id, d.buyer_id, d.seller_id, d.gift, d.status, d.completed_at,
               u.username, u.first_name
        FROM deals d
        LEFT JOIN users u ON u.user_id = CASE WHEN d.buyer_id = ? THEN d.seller_id ELSE d.buyer_id END
        WHERE d.buyer_id = ? OR d.seller_id = ?
        ORDER BY d.deal_id DESC
        LIMIT ? OFFSET ?
    """,
        (user_id, user_id, user_id, PAGE_SIZE, offset),
    )

    deals = cursor.fetchall()

    cursor.execute(
        """
        SELECT deal_id, status, completed_at FROM deals
        WHERE (buyer_id = ? OR seller_id = ?)
          AND status IN ('active', 'waiting_confirm', 'pending', 'completed')
    """,
        (user_id, user_id),
    )
    all_eligible = cursor.fetchall()

    has_active_or_hold = False
    for d_id, st, comp_at in all_eligible:
        if st in ("active", "waiting_confirm", "pending"):
            has_active_or_hold = True
            break
        elif st == "completed" and comp_at:
            cursor.execute(
                "SELECT CASE WHEN datetime(? , '+21 days') > datetime('now') THEN 1 ELSE 0 END",
                (comp_at,),
            )
            if cursor.fetchone()[0] == 1:
                has_active_or_hold = True
                break

    history_text = f"📜 **Ваша история сделок (Стр. {page}/{total_pages}):**\n\n"

    for deal in deals:
        (
            d_id,
            b_id,
            s_id,
            gift,
            status,
            completed_at,
            other_username,
            other_fname,
        ) = deal
        role = "Покупатель 🛒" if user_id == b_id else "Продавец 🎁"

        other_name = (
            f"@{other_username}"
            if other_username
            else (
                other_fname
                if other_fname
                else f"ID {s_id if user_id==b_id else b_id}"
            )
        )

        status_text = {
            "pending": "⏳ Ожидает ответа",
            "active": "🤝 Активна (Идет обмен)",
            "waiting_confirm": "🎁 Ожидает подтверждения",
            "completed": "✅ Завершена",
            "disputed": "🚨 Открыт спор",
            "closed_by_admin": "⚖️ Закрыта арбитражем",
            "cancelled": "❌ Отменена",
        }.get(status, status)

        history_text += f"🔹 **Сделка №{d_id}** | {role}\n"
        history_text += f"👤 Участник: {other_name}\n"
        history_text += f"📦 Товар: {gift}\n"
        history_text += f"📌 Статус: {status_text}\n"

        if status == "completed" and completed_at:
            cursor.execute(
                "SELECT CASE WHEN datetime(? , '+21 days') > datetime('now') THEN 1 ELSE 0 END",
                (completed_at,),
            )
            is_in_hold = cursor.fetchone()[0] == 1

            if is_in_hold:
                history_text += "⏳ *Холд активен (21 день)*\n"
            else:
                history_text += "🎉 *Сделка полностью подтверждена*\n"

        history_text += "────────────────────\n"

    conn.close()

    ikb_rows = []

    if has_active_or_hold:
        ikb_rows.append(
            [
                InlineKeyboardButton(
                    text="🚨 Пожаловаться на сделку",
                    callback_data="history_dispute",
                )
            ]
        )

    if total_pages > 1:
        page_buttons = []

        if page > 1:
            page_buttons.append(
                InlineKeyboardButton(
                    text="◀️", callback_data=f"history_page_{page - 1}"
                )
            )

        start_page = max(1, min(page - 1, total_pages - 2))
        end_page = min(total_pages, start_page + 2)
        start_page = max(1, end_page - 2)

        for p in range(start_page, end_page + 1):
            btn_text = f"• {p} •" if p == page else str(p)
            page_buttons.append(
                InlineKeyboardButton(
                    text=btn_text, callback_data=f"history_page_{p}"
                )
            )

        if page < total_pages:
            page_buttons.append(
                InlineKeyboardButton(
                    text="▶️", callback_data=f"history_page_{page + 1}"
                )
            )

        ikb_rows.append(page_buttons)

    ikb = InlineKeyboardMarkup(inline_keyboard=ikb_rows) if ikb_rows else None
    return history_text, ikb, total_deals


@router.message(F.text == "📜 История сделок")
async def btn_history(message: Message, state: FSMContext):
    user_id = message.from_user.id
    history_text, ikb, total_deals = await render_history_page(
        user_id, page=1
    )

    if total_deals == 0:
        await send_single(message, state, history_text, get_back_keyboard())
    else:
        await send_single(
            message, state, history_text, get_back_keyboard(), ikb=ikb
        )


@router.callback_query(F.data.startswith("history_page_"))
async def process_history_page(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    target_page = int(callback.data.split("_")[2])

    history_text, ikb, _ = await render_history_page(
        user_id, page=target_page
    )

    try:
        await callback.message.edit_text(
            history_text, reply_markup=ikb, parse_mode="Markdown"
        )
    except Exception:
        pass

    


@router.callback_query(F.data == "history_dispute")
async def process_history_dispute(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    await callback.answer() # 👈 1. МГНОВЕННО ОТВЕЧАЕМ ТЕЛЕГРАМУ (Крутилка на кнопке гаснет)

    user_id = callback.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    # ... дальнейшая работа с БД

    cursor.execute(
        """
        SELECT deal_id, gift, status, completed_at
        FROM deals
        WHERE (buyer_id = ? OR seller_id = ?)
          AND status IN ('completed', 'active', 'waiting_confirm', 'pending')
        ORDER BY deal_id DESC
    """,
        (user_id, user_id),
    )

    deals = cursor.fetchall()
    eligible_deals = []

    for d in deals:
        d_id, gift, st, comp_at = d
        if st in ("active", "waiting_confirm", "pending"):
            eligible_deals.append((d_id, gift))
        elif st == "completed" and comp_at:
            cursor.execute(
                "SELECT CASE WHEN datetime(? , '+21 days') > datetime('now') THEN 1 ELSE 0 END",
                (comp_at,),
            )
            if cursor.fetchone()[0] == 1:
                eligible_deals.append((d_id, gift))

    conn.close()

    if not eligible_deals:
        await callback.answer(
            "У вас нет сделок в холде для открытия спора.", show_alert=True
        )
        return

    if len(eligible_deals) == 1:
        d_id, gift = eligible_deals[0]
        callback.data = f"dispute_{d_id}"
        await process_dispute(callback, state, bot)
    else:
        ikb_buttons = [
            [
                InlineKeyboardButton(
                    text=f"🚨 Спор по №{d_id} ({gift})",
                    callback_data=f"dispute_{d_id}",
                )
            ]
            for d_id, gift in eligible_deals
        ]
        ikb = InlineKeyboardMarkup(inline_keyboard=ikb_buttons)

        msg = await callback.message.answer(
            "Выберите сделку, по которой хотите подать претензию:",
            reply_markup=ikb,
        )

        saved_ids = get_saved_msg_ids(user_id)
        saved_ids.append(msg.message_id)
        save_msg_ids(user_id, saved_ids)

        await callback.answer()


######################################################
# 8. ДИСПЕТЧЕР СПОРОВ И АДМИНКА
######################################################


@router.callback_query(F.data.startswith("dispute_"))
async def process_dispute(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    deal_id = int(callback.data.split("_")[1])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id, gift FROM deals WHERE deal_id = ?",
        (deal_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        await callback.answer("Сделка не найдена.")
        return

    buyer_id, seller_id, gift = row

    # Переводим сделку в статус спора и сбрасываем текущие столы участников
    cursor.execute(
        "UPDATE deals SET status = 'disputed' WHERE deal_id = ?", (deal_id,)
    )
    cursor.execute(
        "UPDATE users SET current_table = NULL WHERE user_id IN (?, ?)",
        (buyer_id, seller_id),
    )
    conn.commit()
    conn.close()

    alert_text = (
        f"🚨 **ОТКРЫТ СПОР ПО СДЕЛКЕ №{deal_id}**\n"
        f"Товар: {gift}\n"
        f"Покупатель ID: `{buyer_id}`\n"
        f"Продавец ID: `{seller_id}`\n"
        f"Инициатор спора: `{callback.from_user.id}`"
    )

    admin_ikb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Закрыть мирно",
                    callback_data=f"adm_resolve_{deal_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⛔ Забанить Покупателя",
                    callback_data=f"adm_ban_{buyer_id}_{deal_id}",
                ),
                InlineKeyboardButton(
                    text="⛔ Забанить Продавца",
                    callback_data=f"adm_ban_{seller_id}_{deal_id}",
                ),
            ],
        ]
    )

    try:
        await bot.send_message(
            ADMIN_ID, alert_text, reply_markup=admin_ikb, parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка отправки админу: {e}")

    await callback.answer("🚨 Спор открыт!")

    # Мгновенно переключаем экран общей комнаты у обоих участников на статус спора
    await refresh_user_room(bot, state.storage, buyer_id)
    await refresh_user_room(bot, state.storage, seller_id)


@router.callback_query(F.data.startswith("adm_ban_"))
async def process_admin_ban(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if callback.from_user.id != ADMIN_ID:
        return

    parts = callback.data.split("_")
    target_id = int(parts[2])
    deal_id = int(parts[3])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET is_banned = 1, current_table = NULL WHERE user_id = ?",
        (target_id,),
    )
    cursor.execute(
        "UPDATE deals SET status = 'closed_by_admin' WHERE deal_id = ?",
        (deal_id,),
    )
    conn.commit()
    conn.close()

    await callback.message.edit_text(
        f"⛔ Пользователь `{target_id}` забанен. Спор №{deal_id} закрыт.",
        parse_mode="Markdown",
    )
    try:
        await bot.send_message(
            target_id,
            "⛔ Вы были заблокированы администратором за нарушение правил.",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_resolve_"))
async def process_admin_resolve(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if callback.from_user.id != ADMIN_ID:
        return

    deal_id = int(callback.data.split("_")[2])

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT buyer_id, seller_id FROM deals WHERE deal_id = ?", (deal_id,)
    )
    row = cursor.fetchone()

    cursor.execute(
        "UPDATE deals SET status = 'closed_by_admin' WHERE deal_id = ?",
        (deal_id,),
    )
    conn.commit()
    conn.close()

    await callback.message.edit_text(f"✅ Спор №{deal_id} закрыт без банов.")

    # При закрытии спора обновляем экран комнаты для участников, разблокируя доступ
    if row:
        buyer_id, seller_id = row
        for uid in (buyer_id, seller_id):
            try:
                await bot.send_message(
                    uid,
                    f"✅ Спор по сделке №{deal_id} успешно закрыт администратором.",
                )
                await refresh_user_room(bot, state.storage, uid)
            except Exception:
                pass


######################################################
# 9. ИНТЕРАКТИВНЫЙ РАЗДЕЛ НАСТРОЕК
######################################################


def get_settings_keyboard(lang: str, notifs: int):
    lang_btn = "🌐 Язык: 🇷🇺 Русский" if lang == "ru" else "🌐 Language: 🇬🇧 English"
    notif_btn = "🔔 Уведомления: 🟢 Вкл" if notifs else "🔔 Уведомления: 🔴 Выкл"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=lang_btn, callback_data="set_toggle_lang")],
            [InlineKeyboardButton(text=notif_btn, callback_data="set_toggle_notif")],
        ]
    )


@router.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message, state: FSMContext):
    user_id = message.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT lang, notifications_enabled FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()

    lang = row[0] if row and row[0] else "ru"
    notifs = row[1] if row else 1

    settings_text = (
        "⚙️ **НАСТРОЙКИ ПРОФИЛЯ**\n\n"
        "Управляйте параметрами вашего аккаунта ниже:"
    )
    ikb = get_settings_keyboard(lang, notifs)

    await send_single(
        message, state, settings_text, get_back_keyboard(), ikb=ikb
    )


@router.callback_query(F.data == "set_toggle_lang")
async def toggle_lang(callback: CallbackQuery):
    user_id = callback.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT lang, notifications_enabled FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()

    new_lang = "en" if row[0] == "ru" else "ru"
    cursor.execute(
        "UPDATE users SET lang = ? WHERE user_id = ?", (new_lang, user_id)
    )
    conn.commit()
    conn.close()

    ikb = get_settings_keyboard(new_lang, row[1])
    try:
        await callback.message.edit_reply_markup(reply_markup=ikb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "set_toggle_notif")
async def toggle_notif(callback: CallbackQuery):
    user_id = callback.from_user.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT lang, notifications_enabled FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()

    new_notif = 0 if row[1] else 1
    cursor.execute(
        "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
        (new_notif, user_id),
    )
    conn.commit()
    conn.close()

    ikb = get_settings_keyboard(row[0], new_notif)
    try:
        await callback.message.edit_reply_markup(reply_markup=ikb)
    except Exception:
        pass
    await callback.answer()


######################################################
# 10. РАЗДЕЛ ПРАВИЛА
######################################################


@router.message(F.text == "📖 Правила")
async def btn_rules(message: Message, state: FSMContext):
    rules_text = (
        "☀️ **ДОБРЫЙ ДЕНЬ!** ☀️\n\n"
        "Данная **P2P мини-игра** создана для простых людей, которые хотят прокачать "
        "свой рейтинг профилей в Telegram со 100% уверенностью, что всё пройдет удачно!\n\n"
        "📌 **Как работает общая комната:**\n"
        "1. Вы заходите в комнату, садитесь за стол и выставляете, какой подарок хотите получить "
        "*(естественно, из разряда обычных Telegram-подарков — NFT тут нет)*.\n"
        "2. Далее заходит покупатель, видит ваш стол, смотрит вашу статистику и принимает решение о сделке.\n"
        "3. Покупатель отправляет вам подарок, а вы обязаны его принять и **СОХРАНИТЬ У СЕБЯ В ПРОФИЛЕ**!\n"
        "4. После этого сделка держится на холде **21 день**. Когда покупателю начисляется рейтинг, "
        "сделка закрывается навсегда, а ваша репутация в боте повышается!\n"
        "5. В следующий раз вы снова можете сесть за стол и выставить желаемый подарок.\n\n"
        "💡 **Главная суть:**\n"
        "Суть здесь заключается не в покупке подарков, а в **заработке рейтинга**, поэтому роли распределены именно так! "
        "Тот, кто получает подарок, предоставляет услугу и гарантию его сохранения, тем самым обеспечивая рейтинг покупателю!\n\n"
        "⛔ **Честная игра:**\n"
        "Для тех, кто не следует правилам и попытается соскамить покупателя, предусмотрена кнопка **«Пожаловаться»**. "
        "Если обвинения подтверждаются, нарушитель получает **бан в боте навсегда**!\n\n"
        "💬 **Наше сообщество:**\n"
        "В нашей группе проходят все обсуждения, в том числе и разборы претензий!\n\n"
        "Переходите в группу по ссылке ниже 👇"
    )

    ikb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Наша группа / Обсуждения",
                    url="https://t.me/+KXMR9c-XF4E3MWMy",
                )
            ]
        ]
    )

    await send_single(
        message, state, rules_text, get_back_keyboard(), ikb=ikb
    )


######################################################
# 11. ЗАПУСК БОТА
######################################################


async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())