import asyncio
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TOKEN = "8946706996:AAECoDtaevF4fIKgUH0_2f1u4LktgRySiLs"

# ⬇️ ВСТАВЬТE СЮДА ID ВАШЕЙ ГРУППЫ (узнайте командой /chatid)
GROUP_ID = -1003466396737  # ← ЗАМЕНИТЕ на ID вашей группы

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- База данных ---
conn = sqlite3.connect("tasks.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    creator_id INTEGER,
    assignee_id INTEGER,
    deadline TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT
)
""")
conn.commit()


# --- Проверка: участник ли группы ---
async def is_team_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        return False


# --- Состояния для пошагового создания задачи ---
class NewTask(StatesGroup):
    text = State()
    assignee = State()
    deadline = State()


# --- Узнать ID чата (временная команда, можно удалить после настройки) ---
@dp.message(Command("chatid"))
async def chat_id(message: Message):
    await message.answer(f"ID этого чата: `{message.chat.id}`", parse_mode="Markdown")


# --- Регистрация пользователя ---
@dp.message(Command("start"))
async def start(message: Message):
    cur.execute(
        "INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)",
        (message.from_user.id, message.from_user.full_name)
    )
    conn.commit()
    await message.answer(
        "👋 Привет! Я бот для управления задачами.\n\n"
        "Команды:\n"
        "/new — создать задачу\n"
        "/tasks — список активных задач\n"
        "/my — мои задачи\n"
        "/team — участники команды"
    )


# --- Список участников ---
@dp.message(Command("team"))
async def team(message: Message):
    if not await is_team_member(message.from_user.id):
        await message.answer("⛔ Доступ только для участников команды.")
        return

    cur.execute("SELECT name FROM users")
    rows = cur.fetchall()
    if not rows:
        await message.answer("Пока никто не зарегистрирован.")
        return
    names = "\n".join(f"• {name}" for (name,) in rows)
    await message.answer(f"👥 Участники команды:\n{names}")


# --- Шаг 1: текст задачи ---
@dp.message(Command("new"))
async def new_task(message: Message, state: FSMContext):
    if not await is_team_member(message.from_user.id):
        await message.answer("⛔ Доступ только для участников команды.")
        return

    await message.answer("✍️ Напишите текст задачи:")
    await state.set_state(NewTask.text)


# --- Шаг 2: выбор исполнителя ---
@dp.message(NewTask.text)
async def choose_assignee(message: Message, state: FSMContext):
    await state.update_data(text=message.text)

    cur.execute("SELECT user_id, name FROM users")
    users = cur.fetchall()
    if not users:
        await message.answer("Нет зарегистрированных пользователей. "
                             "Попросите команду нажать /start")
        await state.clear()
        return

    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"assignee_{uid}")]
        for uid, name in users
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("👤 Кому назначить задачу?", reply_markup=kb)
    await state.set_state(NewTask.assignee)


# --- Шаг 3: выбран исполнитель, спрашиваем дедлайн ---
@dp.callback_query(NewTask.assignee, F.data.startswith("assignee_"))
async def choose_deadline(callback: CallbackQuery, state: FSMContext):
    assignee_id = int(callback.data.split("_")[1])
    await state.update_data(assignee_id=assignee_id)

    await callback.message.answer(
        "📅 Укажите дедлайн в формате:\n"
        "`ДД.ММ.ГГГГ ЧЧ:ММ`\n\n"
        "Например: `25.12.2025 18:00`\n\n"
        "Или напишите `нет`, если дедлайн не нужен.",
        parse_mode="Markdown"
    )
    await state.set_state(NewTask.deadline)
    await callback.answer()


# --- Шаг 4: сохраняем задачу ---
@dp.message(NewTask.deadline)
async def save_task(message: Message, state: FSMContext):
    data = await state.get_data()
    deadline = None
    dt = None

    if message.text.strip().lower() != "нет":
        try:
            dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
            if dt < datetime.now():
                await message.answer("⚠️ Дата уже прошла. Введите дату в будущем.")
                return
            deadline = dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            await message.answer(
                "❌ Неверный формат. Пример: `25.12.2025 18:00`\n"
                "Попробуйте ещё раз или напишите `нет`.",
                parse_mode="Markdown"
            )
            return

    cur.execute(
        """INSERT INTO tasks (text, creator_id, assignee_id, deadline, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (data["text"], message.from_user.id, data["assignee_id"],
         deadline, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    conn.commit()
    task_id = cur.lastrowid
    await state.clear()

    # Имя исполнителя
    cur.execute("SELECT name FROM users WHERE user_id=?", (data["assignee_id"],))
    assignee_name = cur.fetchone()[0]

    deadline_text = f"\n📅 Дедлайн: {deadline}" if deadline else ""
    await message.answer(
        f"✅ Задача #{task_id} создана!\n"
        f"👤 Исполнитель: {assignee_name}{deadline_text}"
    )

    # Уведомление исполнителю
    try:
        await bot.send_message(
            data["assignee_id"],
            f"🆕 Вам назначена задача #{task_id}:\n{data['text']}{deadline_text}\n\n"
            f"Создал: {message.from_user.full_name}"
        )
    except Exception:
        pass

    # Планируем напоминания
    if deadline:
        schedule_reminders(task_id, data["assignee_id"], data["text"], dt)


# --- Планирование напоминаний ---
def schedule_reminders(task_id, assignee_id, text, deadline_dt):
    now = datetime.now()

    reminders = [
        (deadline_dt - timedelta(days=1), "⏰ Напоминание: до дедлайна остался 1 день!"),
        (deadline_dt - timedelta(hours=1), "⏰ Напоминание: до дедлайна остался 1 час!"),
        (deadline_dt, "🔴 Дедлайн наступил!"),
    ]

    for remind_time, msg in reminders:
        if remind_time > now:
            scheduler.add_job(
                send_reminder,
                "date",
                run_date=remind_time,
                args=[task_id, assignee_id, text, msg],
                id=f"task_{task_id}_{remind_time.timestamp()}",
                replace_existing=True
            )


async def send_reminder(task_id, assignee_id, text, msg):
    # Проверяем, не выполнена ли задача
    cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row or row[0] == "done":
        return  # задача уже выполнена — не напоминаем

    try:
        await bot.send_message(
            assignee_id,
            f"{msg}\n\nЗадача #{task_id}:\n{text}"
        )
    except Exception:
        pass


# --- Список всех активных задач ---
@dp.message(Command("tasks"))
async def list_tasks(message: Message):
    if not await is_team_member(message.from_user.id):
        await message.answer("⛔ Доступ только для участников команды.")
        return

    cur.execute("""
        SELECT t.id, t.text, t.deadline, u.name
        FROM tasks t LEFT JOIN users u ON t.assignee_id = u.user_id
        WHERE t.status='open'
    """)
    rows = cur.fetchall()
    if not rows:
        await message.answer("📭 Активных задач нет.")
        return

    for task_id, text, deadline, name in rows:
        dl = f"\n📅 Дедлайн: {deadline}" if deadline else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_{task_id}")]
        ])
        await message.answer(
            f"📌 Задача #{task_id}:\n{text}\n👤 {name}{dl}",
            reply_markup=kb
        )


# --- Мои задачи ---
@dp.message(Command("my"))
async def my_tasks(message: Message):
    if not await is_team_member(message.from_user.id):
        await message.answer("⛔ Доступ только для участников команды.")
        return

    cur.execute(
        "SELECT id, text, deadline FROM tasks WHERE assignee_id=? AND status='open'",
        (message.from_user.id,)
    )
    rows = cur.fetchall()
    if not rows:
        await message.answer("📭 У вас нет активных задач.")
        return

    for task_id, text, deadline in rows:
        dl = f"\n📅 Дедлайн: {deadline}" if deadline else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_{task_id}")]
        ])
        await message.answer(f"📌 Задача #{task_id}:\n{text}{dl}", reply_markup=kb)


# --- Отметка выполнения ---
@dp.callback_query(F.data.startswith("done_"))
async def task_done(callback: CallbackQuery):
    if not await is_team_member(callback.from_user.id):
        await callback.answer("⛔ Доступ только для участников команды.", show_alert=True)
        return

    task_id = int(callback.data.split("_")[1])
    cur.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    await callback.message.edit_text(f"✅ Задача #{task_id} выполнена!")

    # Уведомление всем
    cur.execute("SELECT user_id FROM users")
    for (user_id,) in cur.fetchall():
        try:
            await bot.send_message(
                user_id,
                f"🎉 Задача #{task_id} выполнена пользователем {callback.from_user.full_name}"
            )
        except Exception:
            pass
    await callback.answer()


async def main():
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
