import asyncio
import re
import io
import datetime
import time
import json
import requests
from typing import Dict, Any, Optional, List

# -------------------------------------------
# НАСТРОЙКИ
# -------------------------------------------
BOT_TOKEN = "8514888342:AAGYavxKcgOaEmtHFSydpFze3x9Uw_bh5SE"
ADMIN_ID = 1750883753
PAGE_SIZE = 5

# источник базы товаров
PRODUCTS_URL = "https://raw.githubusercontent.com/jon199835-crypto/mar_shopping_bot/main/products.json"

# КЭШ JSON-файла из GitHub
DB_CACHE: List[Dict[str, Any]] = []
DB_LAST_UPDATE = 0

# -------------------------------------------
# AIoGram
# -------------------------------------------
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import Command

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

from openpyxl import load_workbook

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image,
    Table, TableStyle
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# -------------------------------------------
# ХРАНЕНИЕ ПОЛЬЗОВАТЕЛЕЙ
# -------------------------------------------

USER_CARTS: Dict[int, Dict[str, Dict[str, Any]]] = {}
PENDING_QTY: Dict[int, str] = {}
PHOTO_CACHE: Dict[str, str] = {}
FIRST_VISIT = set()

# -------------------------------------------
# МЕНЮ
# -------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔎 Найти артикул")],
        [KeyboardButton(text="🧺 Корзина"), KeyboardButton(text="📄 Оформить заказ")],
        [KeyboardButton(text="📚 Инструкция"), KeyboardButton(text="📞 Контакты")],
        [KeyboardButton(text="📂 Каталог моделей")],
        [KeyboardButton(text="📤 Загрузить Excel")],
    ],
    resize_keyboard=True
)

# -------------------------------------------
# ЗАГРУЗКА JSON С GitHub
# -------------------------------------------

def load_db() -> List[Dict[str, Any]]:
    """Кэшируем products.json на 60 сек."""
    global DB_CACHE, DB_LAST_UPDATE

    now = time.time()
    if now - DB_LAST_UPDATE > 60 or not DB_CACHE:
        try:
            resp = requests.get(PRODUCTS_URL, timeout=5)
            DB_CACHE = json.loads(resp.text)
            DB_LAST_UPDATE = now
            print("DB обновлена")
        except Exception as e:
            print("Ошибка загрузки JSON:", e)

    return DB_CACHE


def get_product_by_article(article_query: str):
    db = load_db()
    for p in db:
        if p["article"].lower() == article_query.lower():
            return p
    return None


def get_products_by_model(model_name: str):
    db = load_db()
    return [p for p in db if p["model"].lower() == model_name.lower()]


def get_all_models():
    db = load_db()
    return sorted(set(p["model"] for p in db if p.get("model")))


# -------------------------------------------
# ПОМОЩНИКИ
# -------------------------------------------

def parse_price_to_int(x: str) -> int:
    x = x.replace(" ", "").replace("\xa0", "")
    return int(x) if x.isdigit() else 0


def resolve_real_url(url: str) -> str:
    try:
        r = requests.get(url, allow_redirects=True, timeout=7)
        return r.url
    except:
        return url


# -------------------------------------------
# КАРТОЧКИ ТОВАРОВ
# -------------------------------------------

async def send_product_card(message: Message, product: Dict[str, Any]):
    article = product["article"]
    name = product["name"]
    photo_url = product["photo_url"]
    opt_price = product["opt_price"]

    caption = (
        f"📦 *{name}*\n"
        f"🆔 `{article}`\n"
        f"📦 Наличие: *{product['stock']} шт*\n"
        f"💰 Опт: *{opt_price} ₽*"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕1", callback_data=f"add_1_{article}"),
                InlineKeyboardButton(text="➕2", callback_data=f"add_2_{article}"),
                InlineKeyboardButton(text="➕5", callback_data=f"add_5_{article}"),
                InlineKeyboardButton(text="➕10", callback_data=f"add_10_{article}"),
            ],
            [InlineKeyboardButton(text="✏️ Ввести количество", callback_data=f"add_manual_{article}")],
            [InlineKeyboardButton(text="🧺 Открыть корзину", callback_data="open_cart")],
        ]
    )

    # cached photo
    if article in PHOTO_CACHE:
        file_id = PHOTO_CACHE[article]
        try:
            await message.answer_document(file_id, caption=caption, parse_mode="Markdown", reply_markup=kb)
            return
        except:
            del PHOTO_CACHE[article]

    # download image
    if photo_url.startswith("http"):
        try:
            url = resolve_real_url(photo_url)
            r = requests.get(url, timeout=7)
            img = io.BytesIO(r.content)
        except:
            await message.answer(caption, parse_mode="Markdown", reply_markup=kb)
            return

        thumb = None
        if PILImage:
            try:
                im = PILImage.open(img)
                im.thumbnail((200, 120))
                t = io.BytesIO()
                im.save(t, format="JPEG")
                t.seek(0)
                thumb = t.getvalue()
            except:
                pass

        img.seek(0)
        sent = await message.answer_document(
            BufferedInputFile(img.getvalue(), filename=f"{article}.jpg"),
            thumb=BufferedInputFile(thumb, filename=f"{article}_thumb.jpg") if thumb else None,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb
        )
        if sent.document:
            PHOTO_CACHE[article] = sent.document.file_id
        return

    await message.answer(caption, parse_mode="Markdown", reply_markup=kb)


# -------------------------------------------
# КОРЗИНА
# -------------------------------------------

async def send_cart(msg: Message, user_id: int, edit=False):
    cart = USER_CARTS.get(user_id, {})
    if not cart:
        await msg.answer("🧺 Корзина пуста.")
        return

    if edit:
        try:
            await msg.delete()
        except:
            pass

    total = 0

    for article, item in cart.items():
        name = item["name"]
        qty = item["qty"]
        price = item["price_opt"]
        subtotal = price * qty
        total += subtotal

        caption = (
            f"🔹 *{name}*\n"
            f"`{article}`\n"
            f"Кол-во: *{qty}* × {price} ₽ = *{subtotal} ₽*"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➖", callback_data=f"cart_minus_{article}"),
                    InlineKeyboardButton(text="➕", callback_data=f"cart_plus_{article}")
                ]
            ]
        )

        await msg.answer(caption, parse_mode="Markdown", reply_markup=kb)

    kb_total = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart_clear")],
            [InlineKeyboardButton(text="📄 Оформить заказ", callback_data="checkout")]
        ]
    )

    await msg.answer(f"💰 *Итого: {total} ₽*", parse_mode="Markdown", reply_markup=kb_total)


def add_to_cart(user_id: int, product, qty: int) -> bool:
    if qty <= 0:
        return False

    stock = product["stock"]
    if qty > stock:
        return False

    article = product["article"]

    if user_id not in USER_CARTS:
        USER_CARTS[user_id] = {}

    if article not in USER_CARTS[user_id]:
        USER_CARTS[user_id][article] = {
            "name": product["name"],
            "price_opt": parse_price_to_int(product["opt_price"]),
            "qty": 0
        }

    if USER_CARTS[user_id][article]["qty"] + qty > stock:
        return False

    USER_CARTS[user_id][article]["qty"] += qty
    return True


def change_cart_qty(user_id: int, article: str, delta: int):
    if user_id in USER_CARTS and article in USER_CARTS[user_id]:
        USER_CARTS[user_id][article]["qty"] += delta
        if USER_CARTS[user_id][article]["qty"] <= 0:
            del USER_CARTS[user_id][article]


# -------------------------------------------
# TELEGRAM BOT
# -------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id

    if user_id not in FIRST_VISIT:
        FIRST_VISIT.add(user_id)
        await message.answer(
            "👋 Привет! Я бот для заказа запчастей.\n\n"
            "Введите артикул, например:\n`8512-153-19`\n",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU
        )
    else:
        await message.answer("Снова привет 👋", reply_markup=MAIN_MENU)


# -------------------------------------------
# ПОИСК
# -------------------------------------------

@dp.message(F.text == "🔎 Найти артикул")
async def ask_article(message: Message):
    await message.answer("Введите артикул:")


def parse_article_and_qty(text: str):
    s = text.strip()
    low = s.lower().replace("х", "x")

    m = re.match(r"^(.+?)\s*[x\*]\s*(\d+)$", low)
    if m:
        return m.group(1).strip(), int(m.group(2))

    m2 = re.match(r"^(.+)\s+(\d+)$", s)
    if m2:
        return m2.group(1), int(m2.group(2))

    return s, None


@dp.message()
async def search(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id

    if user_id in PENDING_QTY:
        article = PENDING_QTY[user_id]
        try:
            qty = int(text)
        except:
            await message.answer("Введите число.")
            return

        product = get_product_by_article(article)
        if not product:
            await message.answer("Ошибка.")
            del PENDING_QTY[user_id]
            return

        if not add_to_cart(user_id, product, qty):
            await message.answer("Недостаточно на складе.")
            return

        del PENDING_QTY[user_id]
        await send_cart(message, user_id)
        return

    article, qty = parse_article_and_qty(text)
    product = get_product_by_article(article)

    if not product:
        await message.answer("❌ Артикул не найден.")
        return

    if qty:
        if not add_to_cart(user_id, product, qty):
            await message.answer("Недостаточно на складе.")
            return
        await send_cart(message, user_id)
        return

    await send_product_card(message, product)


# -------------------------------------------
# CALLBACKS
# -------------------------------------------

@dp.callback_query(F.data.startswith("add_"))
async def cb_add(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if data.startswith("add_manual_"):
        article = data.replace("add_manual_", "")
        PENDING_QTY[user_id] = article
        await callback.message.answer(
            f"Введите количество для `{article}`:", parse_mode="Markdown"
        )
        await callback.answer()
        return

    m = re.match(r"add_(\d+)_(.+)", data)
    qty = int(m.group(1))
    article = m.group(2)

    product = get_product_by_article(article)
    if not product:
        return await callback.answer("Товар не найден.")

    if not add_to_cart(user_id, product, qty):
        return await callback.answer("Нет на складе.", show_alert=True)

    await callback.answer("Добавлено!")


@dp.callback_query(F.data == "open_cart")
async def cb_open_cart(callback: CallbackQuery):
    await callback.answer()
    await send_cart(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "cart_clear")
async def cb_cart_clear(callback: CallbackQuery):
    USER_CARTS[callback.from_user.id] = {}
    await callback.answer("Очищено.")
    await callback.message.answer("🧺 Корзина очищена.")


@dp.callback_query(F.data.startswith("cart_plus_"))
async def cb_cart_plus(callback: CallbackQuery):
    article = callback.data.replace("cart_plus_", "")
    user_id = callback.from_user.id

    product = get_product_by_article(article)
    if not product:
        return await callback.answer("Ошибка.")

    if not add_to_cart(user_id, product, 1):
        return await callback.answer("Нет на складе.", show_alert=True)

    await callback.answer("Добавлено")
    await send_cart(callback.message, user_id, edit=True)


@dp.callback_query(F.data.startswith("cart_minus_"))
async def cb_cart_minus(callback: CallbackQuery):
    article = callback.data.replace("cart_minus_", "")
    user_id = callback.from_user.id

    change_cart_qty(user_id, article, -1)
    await callback.answer("Уменьшено")
    await send_cart(callback.message, user_id, edit=True)


# -------------------------------------------
# CHECKOUT: PDF
# -------------------------------------------

@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery):
    user_id = callback.from_user.id
    cart = USER_CARTS.get(user_id, {})

    if not cart:
        return await callback.answer("Корзина пуста.", show_alert=True)

    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", "DejaVuSans-Bold.ttf"))

    styles = getSampleStyleSheet()
    for s in styles.byName:
        styles[s].fontName = "DejaVu"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)

    elems = []
    elems.append(Paragraph("<b>Заказ Моторешение</b>", styles["Title"]))
    elems.append(Spacer(1, 20))

    table_data = [["Фото", "Артикул", "Название", "Кол-во", "Цена", "Сумма"]]
    total = 0

    for article, item in cart.items():
        qty = item["qty"]
        price = item["price_opt"]
        subtotal = qty * price
        total += subtotal

        p = get_product_by_article(article)
        photo = p["photo_url"] if p else ""

        if photo.startswith("http"):
            try:
                r = requests.get(photo, timeout=5)
                img = Image(io.BytesIO(r.content), width=50, height=50)
            except:
                img = Paragraph("Нет фото", styles["Normal"])
        else:
            img = Paragraph("Нет фото", styles["Normal"])

        table_data.append([
            img, article, item["name"],
            Paragraph(str(qty), styles["Normal"]),
            Paragraph(f"{price} ₽", styles["Normal"]),
            Paragraph(f"{subtotal} ₽", styles["Normal"]),
        ])

    tbl = Table(table_data, colWidths=[50, 60, 180, 40, 50, 60])
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    elems.append(tbl)
    elems.append(Spacer(1, 20))
    elems.append(Paragraph(f"<b>Итого: {total} ₽</b>", styles["Heading2"]))

    doc.build(elems)

    buf.seek(0)
    pdf = buf.read()

    fil_user = BufferedInputFile(pdf, filename="order.pdf")
    fil_admin = BufferedInputFile(pdf, filename="order.pdf")

    await callback.message.answer_document(fil_user, caption="Ваш заказ готов!")
    await bot.send_document(ADMIN_ID, fil_admin, caption="Новый заказ!")

    await callback.answer("Готово!")


# -------------------------------------------
# RUN
# -------------------------------------------

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
