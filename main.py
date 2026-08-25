import asyncio
import logging
import sys
import sqlite3
import secrets
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    MenuButtonWebApp,
    WebAppInfo,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# =====================================================
# КОНФИГУРАЦИЯ
# =====================================================

BOT_TOKEN = "ТВОЙ_ТОКЕН_БОТА"
WEBAPP_URL = "https://твой-сайт.com/index.html"

# =====================================================
# БАЗА ДАННЫХ
# =====================================================

DB_NAME = "users.db"

def init_db():
    """Создаёт таблицы пользователей и рефералов"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 1000.0,
            username TEXT,
            first_name TEXT,
            ref_code TEXT UNIQUE,
            invited_by INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица реферальных начислений (для истории)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            reward REAL DEFAULT 10.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referrer_id) REFERENCES users(user_id),
            FOREIGN KEY (referred_id) REFERENCES users(user_id)
        )
    """)
    
    conn.commit()
    conn.close()
    logging.info("✅ База данных инициализирована")

def get_user(user_id: int):
    """Получает данные пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def create_user(user_id: int, username: str = None, first_name: str = None, invited_by: int = None):
    """Создаёт нового пользователя с уникальным реферальным кодом"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Генерируем уникальный реферальный код
    ref_code = secrets.token_hex(8)
    while True:
        cursor.execute("SELECT ref_code FROM users WHERE ref_code = ?", (ref_code,))
        if not cursor.fetchone():
            break
        ref_code = secrets.token_hex(8)
    
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name, ref_code, invited_by)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, username, first_name, ref_code, invited_by))
    
    conn.commit()
    conn.close()
    return ref_code

def get_user_by_ref_code(ref_code: str):
    """Находит пользователя по реферальному коду"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE ref_code = ?", (ref_code,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_balance(user_id: int) -> float:
    """Получает баланс пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 1000.0

def update_balance(user_id: int, amount: float):
    """Обновляет баланс пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, user_id))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, amount))
    conn.commit()
    conn.close()

def add_referral(referrer_id: int, referred_id: int, reward: float = 10.0):
    """Добавляет реферальную запись и начисляет награду"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Проверяем, не было ли уже такого реферала
    cursor.execute("SELECT id FROM referrals WHERE referrer_id = ? AND referred_id = ?", (referrer_id, referred_id))
    if cursor.fetchone():
        conn.close()
        return False
    
    # Добавляем запись
    cursor.execute("""
        INSERT INTO referrals (referrer_id, referred_id, reward)
        VALUES (?, ?, ?)
    """, (referrer_id, referred_id, reward))
    
    # Начисляем награду рефереру
    referrer_balance = get_balance(referrer_id)
    update_balance(referrer_id, referrer_balance + reward)
    
    # Начисляем бонус новому пользователю
    referred_balance = get_balance(referred_id)
    update_balance(referred_id, referred_balance + reward)
    
    conn.commit()
    conn.close()
    return True

def get_referrals_count(user_id: int) -> int:
    """Считает количество приглашённых пользователей"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def get_referral_link(user_id: int) -> str:
    """Генерирует реферальную ссылку для пользователя"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT ref_code FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return f"https://t.me/{(await bot.me()).username}?start=ref_{result[0]}"
    return None

# =====================================================
# ЛОГИРОВАНИЕ
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =====================================================
# ИНИЦИАЛИЗАЦИЯ
# =====================================================

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# =====================================================
# ОБРАБОТЧИКИ КОМАНД
# =====================================================

@dp.message(Command("start"))
async def start_command(message: Message) -> None:
    """Обработчик команды /start с поддержкой рефералов"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    # Проверяем, есть ли реферальный код в аргументах
    args = message.text.split()
    invited_by = None
    is_new_user = False
    
    # Создаём пользователя, если его нет
    user = get_user(user_id)
    if not user:
        # Проверяем реферальный код
        if len(args) > 1 and args[1].startswith("ref_"):
            ref_code = args[1][4:]  # убираем "ref_"
            referrer_id = get_user_by_ref_code(ref_code)
            if referrer_id and referrer_id != user_id:
                invited_by = referrer_id
        
        create_user(user_id, username, first_name, invited_by)
        is_new_user = True
        
        # Если есть реферер — начисляем бонусы
        if invited_by:
            success = add_referral(invited_by, user_id, 10.0)
            if success:
                # Уведомляем реферера
                try:
                    await bot.send_message(
                        invited_by,
                        f"🎉 <b>Новый реферал!</b>\n\n"
                        f"Пользователь {first_name} (ID: {user_id}) перешёл по вашей ссылке.\n"
                        f"💰 Вы получили +10 звёзд на баланс!\n"
                        f"📊 Всего приглашено: {get_referrals_count(invited_by)}"
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить реферера: {e}")
                
                # Уведомляем нового пользователя
                await message.answer(
                    f"🎉 <b>Добро пожаловать!</b>\n\n"
                    f"Вы перешли по реферальной ссылке!\n"
                    f"💰 Вам начислено +10 звёзд на баланс!\n\n"
                    f"Нажмите на кнопку ниже, чтобы открыть игры.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎮 Открыть игры",
                                web_app=WebAppInfo(url=WEBAPP_URL)
                            )]
                        ]
                    )
                )
                return
    
    # Обычное приветствие
    balance = get_balance(user_id)
    ref_count = get_referrals_count(user_id)
    ref_code = get_referral_link(user_id)
    
    await message.answer(
        f"🎮 <b>Добро пожаловать в DROP, {first_name}!</b>\n\n"
        f"💰 Ваш баланс: <b>{balance:.2f} звёзд</b>\n"
        f"👥 Приглашено друзей: <b>{ref_count}</b>\n\n"
        f"🔥 <b>Доступны игры:</b>\n"
        f"• ⚪ Шарик\n"
        f"• 🎟️ Билеты\n"
        f"• 📦 Кейсы\n"
        f"• 🎡 UPGRADE\n\n"
        f"📎 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{ref_code}</code>\n\n"
        f"💡 Приглашайте друзей и получайте по 10 звёзд за каждого!",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🎮 Открыть игры",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )],
                [InlineKeyboardButton(
                    text="📎 Скопировать ссылку",
                    callback_data=f"copy_ref_{ref_code}"
                )]
            ]
        )
    )

@dp.message(Command("game"))
async def game_command(message: Message) -> None:
    """Обработчик команды /game — сразу открывает игру"""
    user_id = message.from_user.id
    balance = get_balance(user_id)
    
    await message.answer(
        f"🎮 <b>Открываем игры...</b>\n"
        f"💰 Баланс: {balance:.2f} звёзд",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🎮 Открыть игры",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )]
            ]
        )
    )

@dp.message(Command("balance"))
async def balance_command(message: Message) -> None:
    """Обработчик команды /balance — показывает баланс"""
    user_id = message.from_user.id
    balance = get_balance(user_id)
    ref_count = get_referrals_count(user_id)
    
    await message.answer(
        f"💰 <b>Ваш баланс:</b> {balance:.2f} звёзд\n"
        f"👥 <b>Приглашено друзей:</b> {ref_count}"
    )

@dp.message(Command("profile"))
async def profile_command(message: Message) -> None:
    """Обработчик команды /profile — показывает профиль"""
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    balance = get_balance(user_id)
    ref_count = get_referrals_count(user_id)
    ref_code = get_referral_link(user_id)
    
    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: {first_name}\n"
        f"ID: {user_id}\n"
        f"💰 Баланс: {balance:.2f} звёзд\n"
        f"👥 Приглашено: {ref_count}\n\n"
        f"📎 <b>Реферальная ссылка:</b>\n"
        f"<code>{ref_code}</code>"
    )

@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    """Обработчик команды /help — справка"""
    await message.answer(
        "📖 <b>Помощь</b>\n\n"
        "/start — Главное меню\n"
        "/game — Открыть игры\n"
        "/balance — Показать баланс\n"
        "/profile — Профиль и реферальная ссылка\n"
        "/help — Эта справка\n\n"
        "💰 Играйте и выигрывайте!"
    )

# =====================================================
# CALLBACK HANDLERS
# =====================================================

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref_callback(callback: types.CallbackQuery):
    """Обработчик кнопки копирования реферальной ссылки"""
    ref_code = callback.data.replace("copy_ref_", "")
    await callback.answer(
        f"Ссылка скопирована! Поделитесь с друзьями.",
        show_alert=False
    )
    # В реальном боте можно использовать tg:// для копирования
    # Но лучше просто показать ссылку в сообщении

# =====================================================
# УСТАНОВКА КОМАНД И КНОПКИ МЕНЮ
# =====================================================

async def set_commands_and_menu():
    """Устанавливает команды бота и кнопку меню с WebApp"""
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="game", description="Открыть игры"),
        BotCommand(command="balance", description="Показать баланс"),
        BotCommand(command="profile", description="Профиль и реферальная ссылка"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="🎮 Играть",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    )
    logger.info("✅ Команды и кнопка меню установлены")

# =====================================================
# ЗАПУСК
# =====================================================

async def main() -> None:
    """Главная функция запуска бота"""
    logger.info("🚀 Запуск бота...")
    
    # Инициализируем базу данных
    init_db()
    
    # Устанавливаем команды и кнопку меню
    await set_commands_and_menu()
    
    # Запускаем polling
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")