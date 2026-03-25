import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import asyncpg
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pool = None
user_states = {}

# ---------------- DB ----------------

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            name TEXT,
            active_trip_id INT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id SERIAL PRIMARY KEY,
            title TEXT,
            currency TEXT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS trip_members (
            trip_id INT,
            user_id BIGINT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            trip_id INT,
            payer_id BIGINT,
            amount FLOAT,
            note TEXT
        );
        """)

# ---------------- HELPERS ----------------

async def get_user(user_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", user_id)

async def create_user(user_id, name):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO users (telegram_id, name)
        VALUES ($1,$2)
        ON CONFLICT DO NOTHING
        """, user_id, name)

async def get_name(user_id):
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT name FROM users WHERE telegram_id=$1", user_id
        )
        return user["name"] if user and user["name"] else f"User {user_id}"

async def set_active_trip(user_id, trip_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET active_trip_id=$1 WHERE telegram_id=$2",
            trip_id, user_id
        )

# ---------------- UI ----------------

def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✈️ Create trip")],
            [KeyboardButton(text="🔗 Join last trip")],
            [KeyboardButton(text="➕ Add expense")],
            [KeyboardButton(text="📊 Calculate debts")]
        ],
        resize_keyboard=True
    )

# ---------------- START ----------------

@dp.message(Command("start"))
async def start(message: types.Message):
    user = await get_user(message.from_user.id)

    if not user:
        user_states[message.from_user.id] = {"step": "name"}
        await message.answer("👋 Welcome!\n\nEnter your name:")
        return

    await message.answer("🚀 TripSplit ready", reply_markup=menu())

# ---------------- MAIN HANDLER ----------------

@dp.message()
async def handler(message: types.Message):

    user_id = message.from_user.id

    # === STATES FIRST (ВАЖНО) ===
    if user_id in user_states:
        state = user_states[user_id]

        # NAME
        if state.get("step") == "name":
            await create_user(user_id, message.text)
            user_states.pop(user_id)
            await message.answer(f"✅ Welcome, {message.text}!", reply_markup=menu())
            return

        # TITLE
        if state.get("step") == "title":
            state["title"] = message.text
            state["step"] = "currency"
            await message.answer("💱 Enter currency (USD / EUR):")
            return

        # CURRENCY (🔥 ТВОЯ ПРОБЛЕМА ТУТ)
        if state.get("step") == "currency":
            async with pool.acquire() as conn:
                trip_id = await conn.fetchval(
                    "INSERT INTO trips (title,currency) VALUES ($1,$2) RETURNING id",
                    state["title"], message.text
                )

                await conn.execute(
                    "INSERT INTO trip_members (trip_id,user_id) VALUES ($1,$2)",
                    trip_id, user_id
                )

            await set_active_trip(user_id, trip_id)
            user_states.pop(user_id)

            await message.answer("✅ Trip created! Others can now join.")
            return

        # AMOUNT
        if state.get("step") == "amount":
            try:
                state["amount"] = float(message.text)
            except:
                await message.answer("❗ Enter a number")
                return

            state["step"] = "note"
            await message.answer("📝 Enter comment:")
            return

        # NOTE
        if state.get("step") == "note":
            async with pool.acquire() as conn:
                user = await get_user(user_id)
                trip_id = user["active_trip_id"]

                await conn.execute(
                    "INSERT INTO expenses (trip_id,payer_id,amount,note) VALUES ($1,$2,$3,$4)",
                    trip_id, user_id, state["amount"], message.text
                )

            user_states.pop(user_id)
            await message.answer("✅ Expense added")
            return

    # === BUTTONS AFTER STATES ===

    if message.text == "✈️ Create trip":
        user_states[user_id] = {"step": "title"}
        await message.answer("🧳 Enter trip name:")
        return

    if message.text == "🔗 Join last trip":
        async with pool.acquire() as conn:
            trip = await conn.fetchrow("SELECT * FROM trips ORDER BY id DESC LIMIT 1")

            if not trip:
                await message.answer("❗ No trips yet")
                return

            await conn.execute(
                "INSERT INTO trip_members (trip_id,user_id) VALUES ($1,$2)",
                trip["id"], user_id
            )

        await set_active_trip(user_id, trip["id"])
        await message.answer(f"✅ Joined: {trip['title']}")
        return

    if message.text == "➕ Add expense":
        user = await get_user(user_id)

        if not user or not user["active_trip_id"]:
            await message.answer("❗ Create or join a trip first")
            return

        user_states[user_id] = {"step": "amount"}
        await message.answer("💰 Enter amount:")
        return

    if message.text == "📊 Calculate debts":
        await calculate_and_notify(message)
        return

# ---------------- CALCULATE ----------------

async def calculate_and_notify(message):

    user_id = message.from_user.id

    async with pool.acquire() as conn:

        user = await get_user(user_id)

        if not user or not user["active_trip_id"]:
            await message.answer("❗ No active trip")
            return

        trip_id = user["active_trip_id"]

        expenses = await conn.fetch(
            "SELECT * FROM expenses WHERE trip_id=$1",
            trip_id
        )

        members = await conn.fetch(
            "SELECT user_id FROM trip_members WHERE trip_id=$1",
            trip_id
        )

        balances = {m["user_id"]: 0 for m in members}

        for e in expenses:
            share = e["amount"] / len(members)

            for member in members:
                balances[member["user_id"]] -= share

            balances[e["payer_id"]] += e["amount"]

    creditors = []
    debtors = []

    for uid, bal in balances.items():
        if bal > 0:
            creditors.append([uid, bal])
        elif bal < 0:
            debtors.append([uid, -bal])

    result = ""

    for d_uid, d_amt in debtors:
        for c in creditors:
            if d_amt == 0:
                break

            c_uid, c_amt = c
            pay = min(d_amt, c_amt)

            if pay > 0:
                name_from = await get_name(d_uid)
                name_to = await get_name(c_uid)

                result += f"{name_from} → {name_to}: {round(pay,2)}\n"

                c[1] -= pay
                d_amt -= pay

    await message.answer(result or "🎉 Nobody owes anyone")

# ---------------- RUN ----------------

async def main():
    print("🔥 BOT STARTED 🔥")

    await init_db()

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())