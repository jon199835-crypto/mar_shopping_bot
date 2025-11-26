import asyncio
from typing import Dict, Any, Optional
import re
import io
import datetime
import requests

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.types import BufferedInputFile  # у тебя уже есть
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

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
from aiogram.filters import Command

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, KeepInFrame
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# -------------------------------------------
# НАСТРОЙКИ
# -------------------------------------------
BOT_TOKEN = "8514888342:AAGYavxKcgOaEmtHFSydpFze3x9Uw_bh5SE"
SPREADSHEET_ID = "1eGaXQK4L8pL1uaT_T1rBBnu_6b14aVnGo2ImkdYR6tw"
SHEET_NAME = "База"  # название листа в таблице
ADMIN_ID = 1750883753
PAGE_SIZE = 5  # сколько товаров показывать на одной странице в каталоге модели

# -------------------------------------------
# GOOGLE SHEETS
# -------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

# -------------------------------------------
# КЭШИ и ХРАНИЛИЩА
# -------------------------------------------

# user_id -> { article -> { name, price_opt(int), qty(int) } }
USER_CARTS: Dict[int, Dict[str, Dict[str, Any]]] = {}

# user_id -> article (ожидаем, что юзер введёт количество руками)
PENDING_QTY: Dict[int, str] = {}

# article -> file_id (фото в телеге, чтобы слать мгновенно)
PHOTO_CACHE: Dict[str, str] = {}

# user_id — кто уже видел приветствие
FIRST_VISIT = set()

# -------------------------------------------
# ПОМОЩНИКИ
# -------------------------------------------

def get_column_indexes() -> Dict[str, int]:
    """
    Берём первую строку как заголовок и находим столбцы:
    Артикул | Название | Опт | РРЦ | Фото
    """
    header = sheet.row_values(1)
    col_map: Dict[str, int] = {}

    for idx, title in enumerate(header):
        t = title.strip().lower()
        if t == "артикул":
            col_map["article"] = idx
        elif t == "название":
            col_map["name"] = idx
        elif t == "опт":
            col_map["opt"] = idx
        elif t == "ррц":
            col_map["rrc"] = idx
        elif t == "фото":
            col_map["photo"] = idx
        elif t == "наличие":
            col_map["stock"] = idx
        elif t == "модель":
            col_map["model"] = idx

    return col_map


COL = get_column_indexes()

# -------------------------------------------
# ЛОКАЛЬНЫЙ КЭШ GOOGLE SHEETS (обновление раз в 60 сек)
# -------------------------------------------
import time

DB_CACHE = []          # здесь хранится вся таблица
DB_LAST_UPDATE = 0     # время последнего обновления

def load_db():
    """
    Обновляет таблицу не чаще 1 раза в 60 секунд.
    Возвращает локальный кэш.
    """
    global DB_CACHE, DB_LAST_UPDATE

    now = time.time()
    # обновляем только если прошло > 60 секунд
    if now - DB_LAST_UPDATE > 60 or not DB_CACHE:
        try:
            DB_CACHE = sheet.get_all_values()
            DB_LAST_UPDATE = now
        except Exception as e:
            print("Ошибка обновления Google Sheets:", e)

    return DB_CACHE

def parse_price_to_int(price_str: str) -> int:
    """Превращаем '34 042' → 34042."""
    cleaned = price_str.replace(" ", "").replace("\xa0", "")
    return int(cleaned) if cleaned.isdigit() else 0


def resolve_real_url(url: str) -> str:
    """
    Раскручиваем редиректы (Ozon/WB/CDN) до конечного URL.
    Если не вышло — возвращаем исходный.
    """
    try:
        r = requests.get(url, allow_redirects=True, timeout=7)
        return r.url
    except Exception:
        return url


def get_product_by_article(article_query: str) -> Optional[Dict[str, Any]]:
    values = load_db()

    for row in values[1:]:
        if len(row) <= COL["article"]:
            continue

        article = row[COL["article"]].strip()

        if article.lower() == article_query.strip().lower():

            name = row[COL["name"]] if len(row) > COL["name"] else article
            opt_price = row[COL["opt"]] if len(row) > COL["opt"] else "0"
            rrc_price = row[COL["rrc"]] if len(row) > COL["rrc"] else "0"
            photo_url = row[COL["photo"]] if "photo" in COL and len(row) > COL["photo"] else ""

            stock_raw = row[COL["stock"]] if "stock" in COL and len(row) > COL["stock"] else "0"
            stock = int(stock_raw) if stock_raw.isdigit() else 0

            return {
                "article": article,
                "name": name,
                "opt_price": opt_price,
                "rrc_price": rrc_price,
                "photo_url": photo_url,
                "stock": stock,
            }

    return None
def get_products_by_model(model_name: str):
    """
    Возвращает список товаров по модели снегохода.
    """
    values = load_db()
    result = []

    for row in values[1:]:
        # пропускаем битые строки
        if len(row) <= COL["model"]:
            continue

        model = row[COL["model"]].strip().lower()
        if model != model_name.lower():
            continue

        # собираем товар
        article = row[COL["article"]].strip()
        name = row[COL["name"]] if len(row) > COL["name"] else article
        opt_price = row[COL["opt"]] if len(row) > COL["opt"] else "0"
        rrc_price = row[COL["rrc"]] if len(row) > COL["rrc"] else "0"
        photo_url = row[COL["photo"]] if "photo" in COL and len(row) > COL["photo"] else ""

        stock_raw = row[COL["stock"]] if "stock" in COL and len(row) > COL["stock"] else "0"
        stock = int(stock_raw) if stock_raw.isdigit() else 0

        result.append({
            "article": article,
            "name": name,
            "opt_price": opt_price,
            "rrc_price": rrc_price,
            "photo_url": photo_url,
            "stock": stock,
            "model": model_name,
        })

    return result

def add_to_cart(user_id: int, product: Dict[str, Any], qty: int) -> bool:
    """
    Возвращает True — успешно, False — превышение наличия.
    """
    if qty <= 0:
        return False

    stock = product["stock"]
    article = product["article"]

    if user_id not in USER_CARTS:
        USER_CARTS[user_id] = {}

    current_qty = USER_CARTS[user_id].get(article, {}).get("qty", 0)

    if current_qty + qty > stock:
        return False

    # Если хватает — добавляем
    name = product["name"]
    opt_price_int = parse_price_to_int(product["opt_price"])

    if article not in USER_CARTS[user_id]:
        USER_CARTS[user_id][article] = {
            "name": name,
            "price_opt": opt_price_int,
            "qty": 0,
        }

    USER_CARTS[user_id][article]["qty"] += qty
    return True

    


def change_cart_qty(user_id: int, article: str, delta: int) -> None:
    """Меняем количество товара в корзине на delta."""
    if user_id not in USER_CARTS:
        return
    if article not in USER_CARTS[user_id]:
        return

    USER_CARTS[user_id][article]["qty"] += delta
    if USER_CARTS[user_id][article]["qty"] <= 0:
        del USER_CARTS[user_id][article]


def parse_article_and_qty(text: str) -> (str, Optional[int]):
    """
    Поддерживаем форматы:
    - '8512-153-19'
    - '8512-153-19 x 3' / '8512-153-19 х 3'
    - '8512-153-19 * 5'
    - '8512-153-19 10'
    """
    s = text.strip()
    s_lower = s.lower().replace("х", "x")

    # артикул x 3 или * 3
    m = re.match(r"^(.+?)\s*[x\*]\s*(\d+)$", s_lower)
    if m:
        article = m.group(1).strip()
        qty = int(m.group(2))
        return article, qty

    # артикул 3 (через пробел)
    m2 = re.match(r"^(.+)\s+(\d+)$", s)
    if m2:
        article = m2.group(1).strip()
        qty = int(m2.group(2))
        return article, qty

    # только артикул
    return s, None
def get_all_models():
    values = load_db()
    models = set()

    for row in values[1:]:
        if len(row) > COL["model"]:
            m = row[COL["model"]].strip()
            if m:
                models.add(m)

    return sorted(models)
async def send_model_page(message: Message, model: str, page: int):
    """
    Показывает одну страницу товаров по выбранной модели.
    """
    products = get_products_by_model(model)
    if not products:
        await message.answer("❌ Для этой модели запчастей не найдено.")
        return

    total = len(products)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE  # всего страниц

    if page < 1:
        page = 1
    if page > pages:
        page = pages

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_products = products[start:end]

    # заголовок страницы
    await message.answer(
        f"📂 Запчасти для *{model}* (стр. {page}/{pages}):",
        parse_mode="Markdown"
    )

    # товары
    for p in page_products:
        await send_product_card(message, p)

    # навигация по страницам
    if pages > 1:
        buttons = []
        if page > 1:
            buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"modelpage_{page-1}_{model}"
                )
            )
        if page < pages:
            buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"modelpage_{page+1}_{model}"
                )
            )

        kb = InlineKeyboardMarkup(inline_keyboard=[buttons])

        await message.answer(
            f"Страница {page}/{pages}",
            reply_markup=kb
        )
# -------------------------------------------
# ОТОБРАЖЕНИЕ ТОВАРА
# -------------------------------------------

async def send_product_card(message: Message, product: Dict[str, Any]) -> None:
    """
    Карточка товара — фото отправляется как документ,
    миниатюра 200x120, не растягивается Telegram’ом.
    """
    article = product["article"]
    name = product["name"]
    opt_price_str = product["opt_price"]
    photo_url = product["photo_url"].strip()

    caption = (
        f"📦 *{name}*\n"
        f"🆔 Артикул: `{article}`\n\n"
        f"📦 Наличие: *{product['stock']} шт*\n\n"
        f"💰 Опт: *{opt_price_str} ₽*"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕1", callback_data=f"add_1_{article}"),
                InlineKeyboardButton(text="➕2", callback_data=f"add_2_{article}"),
                InlineKeyboardButton(text="➕5", callback_data=f"add_5_{article}"),
                InlineKeyboardButton(text="➕10", callback_data=f"add_10_{article}"),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Ввести количество",
                    callback_data=f"add_manual_{article}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧺 Открыть корзину",
                    callback_data="open_cart",
                )
            ],
        ]
    )

    # Если фото уже есть в кэше — отправляем мгновенно
    if article in PHOTO_CACHE:
        file_id = PHOTO_CACHE[article]
        try:
            await message.answer_document(
                file_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=kb
            )
            return
        except:
            del PHOTO_CACHE[article]

    # Качаем фото
    if photo_url.startswith("http"):
        real_url = resolve_real_url(photo_url)
        try:
            resp = requests.get(real_url, timeout=7)
            img_bytes = io.BytesIO(resp.content)
        except:
            await message.answer(caption, parse_mode="Markdown", reply_markup=kb)
            return

        # Создаём миниатюру 200x120
        thumb_bytes = None
        if PILImage is not None:
            try:
                im = PILImage.open(img_bytes)
                im.thumbnail((200, 120))
                thumb_io = io.BytesIO()
                im.save(thumb_io, format="JPEG")
                thumb_io.seek(0)
                thumb_bytes = thumb_io.getvalue()
            except:
                pass

        img_bytes.seek(0)

        # Отправляем как документ с миниатюрой
        sent = await message.answer_document(
            document=BufferedInputFile(img_bytes.getvalue(), filename=f"{article}.jpg"),
            thumb=BufferedInputFile(thumb_bytes, filename=f"{article}_thumb.jpg") if thumb_bytes else None,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb,
        )

        # Сохраняем file_id
        if sent.document:
            PHOTO_CACHE[article] = sent.document.file_id

        return

    # Если нет фото вовсе
    await message.answer(caption, parse_mode="Markdown", reply_markup=kb)


# -------------------------------------------
# ОТОБРАЖЕНИЕ КОРЗИНЫ (вариант A: каждый товар отдельным сообщением)
# -------------------------------------------

async def send_cart(message_or_cb_msg: Message, user_id: int, edit: bool = False) -> None:
    """
    Корзина:
    — Каждый товар отдельным сообщением с кнопками +/-.
    — Отдельное финальное сообщение с итогом и кнопками.
    """
    cart = USER_CARTS.get(user_id, {})

    if not cart:
        await message_or_cb_msg.answer("🧺 Корзина пуста.")
        return

    # Если вызываем из callback и хотим "обновить" — удалим одно старое сообщение,
    # новое состояние корзины появится ниже.
    if edit:
        try:
            await message_or_cb_msg.delete()
        except Exception:
            pass

    total = 0

    # 1️⃣ Товары по одному
    for article, item in cart.items():
        qty = item["qty"]
        price = item["price_opt"]
        name = item["name"]
        subtotal = qty * price
        total += subtotal

        text = (
            f"🔹 *{name}*\n"
            f"🆔 `{article}`\n"
            f"Кол-во: *{qty}* × {price} ₽ = *{subtotal} ₽*"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data=f"cart_minus_{article}"),
                InlineKeyboardButton(text="➕", callback_data=f"cart_plus_{article}")
            ]
        ])

        await message_or_cb_msg.answer(text, parse_mode="Markdown", reply_markup=kb)

    # 2️⃣ Финальный блок с итогом + кнопки очистки/оформления
    total_text = f"💰 *Итого: {total} ₽*"

    kb_total = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart_clear"),
        ],
        [
            InlineKeyboardButton(text="📄 Оформить заказ", callback_data="checkout"),
        ]
    ])

    await message_or_cb_msg.answer(total_text, parse_mode="Markdown", reply_markup=kb_total)


# -------------------------------------------
# TELEGRAM BOT
# -------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id

    # Первый вход — показываем приветствие
    if user_id not in FIRST_VISIT:
        FIRST_VISIT.add(user_id)

        await message.answer(
            "👋 Привет! Я бот для заказа запчастей.\n\n"
            "🔎 Чтобы начать — просто отправьте артикул, например:\n"
            "`8512-153-19`\n\n"
            "Или используйте главное меню ниже 👇",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU
        )
        return

    # Повторный вход — просто показываем меню
    await message.answer(
        "Вы снова в боте 😊\nВыберите действие:",
        reply_markup=MAIN_MENU
    )
# -------------------------------------------
# ГЛАВНОЕ МЕНЮ — ОБРАБОТЧИКИ КНОПОК
# -------------------------------------------

@dp.message(F.text == "🔎 Найти артикул")
async def btn_find_article(message: Message):
    await message.answer(
        "Введите артикул, например:\n`8512-153-19`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🧺 Корзина")
async def btn_cart(message: Message):
    await send_cart(message, message.from_user.id)

@dp.message(F.text == "📄 Оформить заказ")
async def btn_checkout(message: Message):
    fake_callback = type("obj", (object,), {"from_user": message.from_user, "message": message})
    await checkout_handler(fake_callback)

@dp.message(F.text == "📚 Инструкция")
async def btn_instruction(message: Message):
    await message.answer(
        "📚 *Как пользоваться ботом:*\n\n"
        "1️⃣ Введите артикул\n"
        "2️⃣ Добавьте количество\n"
        "3️⃣ Откройте корзину\n"
        "4️⃣ Нажмите «Оформить заказ»\n\n"
        "Бот сформирует PDF и отправит менеджеру.",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📞 Контакты")
async def btn_contacts(message: Message):
    await message.answer(
        "📞 *Контакты:*\n\n"
        "Менеджер: @evgenijtuzikov\n"
        "Телефон: +7...\n"
        "Работаем ежедневно 10:00–21:00",
        parse_mode="Markdown"
    )
@dp.message(F.text == "📂 Каталог моделей")
async def show_model_catalog(message: Message):
    models = get_all_models()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=m, callback_data=f"model_{m}")]
            for m in models
        ]
    )

    await message.answer(
        "Выберите модель снегохода:",
        reply_markup=kb
    )


# ⬇️ Обработчик должен быть СРАЗУ ПОСЛЕ функции, без лишних отступов

@dp.message(F.text == "📤 Загрузить Excel")
async def btn_upload_excel(message: Message):
    await message.answer(
        "📤 *Загрузка Excel-прайса*\n\n"
        "Отправьте файл формата *.xlsx*, содержащий:\n"
        "`Артикул | Количество`\n\n"
        "Пример:\n"
        "`8512-153-19 | 3`\n"
        "`3B4-23311-00 | 1`\n\n"
        "После загрузки я автоматически добавлю товары в корзину.",
        parse_mode="Markdown"
    )
# -------------------------------------------
# ОБЩИЙ ОБРАБОТЧИК СООБЩЕНИЙ
# -------------------------------------------
@dp.message(F.document)
async def handle_excel_upload(message: Message):
    """
    Обработка Excel-файла:
    Поддерживаем .xlsx, парсим артикул + количество, добавляем в корзину.
    """
    user_id = message.from_user.id
    file = message.document

    # Проверяем расширение
    if not file.file_name.lower().endswith(".xlsx"):
        await message.answer("Пожалуйста, отправьте файл Excel в формате .xlsx")
        return

    # Скачиваем файл
    file_bytes = await bot.download(file)
    file_bytes.seek(0)

    try:
        wb = load_workbook(file_bytes, data_only=True)
        ws = wb.active
    except Exception:
        await message.answer("Не удалось прочитать Excel-файл 😔")
        return

    added = 0
    errors = []

    # Ищем колонки
    header_map = {}
    first_row = [str(c.value).strip().lower() if c.value else "" for c in ws[1]]

    for idx, title in enumerate(first_row):
        if "артикул" in title:
            header_map["article"] = idx
        if "кол" in title:
            header_map["qty"] = idx

    # Если шапки нет — предполагаем A=Артикул, B=Кол-во
    if not header_map:
        header_map = {"article": 0, "qty": 1}

        # Обрабатываем строки
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[header_map["article"]]:
            continue

        raw_article = row[header_map["article"]]

        # 1) Если Excel записал артикул как число (84300.0 или 84300)
        if isinstance(raw_article, (int, float)):
            raw_article = str(raw_article).rstrip(".0")

        # 2) Превращаем в строку
        article = str(raw_article).strip()

        # 3) Восстанавливаем потерянный ведущий ноль перед дефисом
        # пример — Excel сделал '8-4300' → нужно '08-4300'
        if "-" in article and article.replace("-", "").isdigit():
            left, right = article.split("-", 1)

            if len(left) == 1:
                left = "0" + left

            article = f"{left}-{right}"

        qty_raw = row[header_map["qty"]]

        # Количество
        try:
            qty = int(qty_raw)
            if qty <= 0:
                raise ValueError
        except:
            errors.append(f"{article} — неверное количество")
            continue

        # Ищем товар в базе
        product = get_product_by_article(article)
        if not product:
            errors.append(f"{article} — товар не найден")
            continue

        # Пытаемся добавить
        ok = add_to_cart(user_id, product, qty)
        if not ok:
            errors.append(f"{article} — недостаточно на складе ({product['stock']})")
            continue

        added += 1

    # Вывод результатов
    msg = f"📥 Загрузка Excel завершена!\n\n"
    msg += f"✅ Добавлено позиций: *{added}*\n"

    if errors:
        msg += "\n⚠️ Ошибки:\n" + "\n".join(f"• {e}" for e in errors)

    await message.answer(msg, parse_mode="Markdown")

    if added > 0:
        await send_cart(message, user_id)

@dp.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # 1) если ждём ручной ввод количества
    if user_id in PENDING_QTY:
        article = PENDING_QTY[user_id]
        try:
            qty = int(text)
            if qty <= 0:
                await message.answer("Количество должно быть больше нуля.")
                return
        except ValueError:
            await message.answer("Введите, пожалуйста, целое число, например: 5")
            return

        product = get_product_by_article(article)
        if not product:
            await message.answer("Не смог найти товар, попробуйте ещё раз.")
            del PENDING_QTY[user_id]
            return

        ok = add_to_cart(user_id, product, qty)
        if not ok:
            await message.answer(f"❗ Доступно только {product['stock']} шт")
            return

        del PENDING_QTY[user_id]

        await message.answer(
            f"✅ Добавлено {qty} шт товара *{product['name']}* "
            f"(арт. `{product['article']}`) в корзину.",
            parse_mode="Markdown",
        )
        await send_cart(message, user_id)
        return

    # 2) обычное сообщение → парсим артикул и количество
    article_query, qty = parse_article_and_qty(text)
    product = get_product_by_article(article_query)

    if not product:
        await message.answer("❌ Артикул не найден.")
        return

    # если количество указано → сразу в корзину
    if qty is not None:
        if qty <= 0:
            await message.answer("Количество должно быть больше нуля.")
            return

        add_to_cart(user_id, product, qty)
        await message.answer(
            f"✅ Добавлено {qty} шт *{product['name']}* "
            f"(арт. `{product['article']}`) в корзину.",
            parse_mode="Markdown",
        )
        await send_cart(message, user_id)
        return

    # иначе просто показываем карточку товара
    await send_product_card(message, product)

from openpyxl import load_workbook


# -------------------------------------------
# CALLBACK: ОТКРЫТЬ КОРЗИНУ
# -------------------------------------------
@dp.callback_query(F.data == "open_cart")
async def cb_open_cart(callback: CallbackQuery):
    await callback.answer()
    await send_cart(callback.message, callback.from_user.id)


# -------------------------------------------
# CALLBACK: ОЧИСТИТЬ КОРЗИНУ
# -------------------------------------------
@dp.callback_query(F.data == "cart_clear")
async def cb_cart_clear(callback: CallbackQuery):
    user_id = callback.from_user.id
    USER_CARTS[user_id] = {}
    await callback.answer("Корзина очищена.")
    await callback.message.answer("🧺 Корзина очищена.")


# -------------------------------------------
# CALLBACK: БЫСТРЫЕ КНОПКИ ДОБАВЛЕНИЯ (+1,+2,+5,+10)
# add_1_ARTICLE  / add_2_ARTICLE / add_5_... / add_10_...
# add_manual_ARTICLE
# -------------------------------------------
@dp.callback_query(F.data.startswith("add_"))
async def cb_add(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data  # пример: add_1_12345 или add_manual_12345

    # --- Ручной ввод количества ---
    if data.startswith("add_manual_"):
        article = data.replace("add_manual_", "", 1)
        PENDING_QTY[user_id] = article
        await callback.answer()
        await callback.message.answer(
            f"✏️ Введите количество для артикула `{article}`:",
            parse_mode="Markdown",
        )
        return

    # --- Быстрые кнопки ---
    m = re.match(r"^add_(\d+)_(.+)$", data)
    if not m:
        await callback.answer("Ошибка формата.", show_alert=True)
        return

    qty = int(m.group(1))
    article = m.group(2)

    # Ищем товар
    product = get_product_by_article(article)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    # Проверяем наличие
    stock = product.get("stock", 0)
    current_qty = USER_CARTS.get(user_id, {}).get(article, {}).get("qty", 0)

    if current_qty + qty > stock:
        await callback.answer(
            f"❗ На складе доступно только {stock} шт",
            show_alert=True
        )
        return

    # Добавляем в корзину
    add_to_cart(user_id, product, qty)
    await callback.answer(f"Добавлено {qty} шт в корзину!")

@dp.callback_query(F.data.startswith("model_"))
async def cb_show_model_parts(callback: CallbackQuery):
    model = callback.data.replace("model_", "")
    await callback.answer()
    await send_model_page(callback.message, model, page=1)


@dp.callback_query(F.data.startswith("modelpage_"))
async def cb_model_page(callback: CallbackQuery):
    data = callback.data
    _, page_str, model = data.split("_", 2)

    try:
        page = int(page_str)
    except ValueError:
        await callback.answer("Ошибка страницы.", show_alert=True)
        return

    try:
        await callback.message.delete()
    except:
        pass

    await callback.answer()
    await send_model_page(callback.message, model, page)
# -------------------------------------------
# CALLBACK: ПЛЮС / МИНУС В КОРЗИНЕ
# cart_plus_ARTICLE / cart_minus_ARTICLE
# -------------------------------------------
@dp.callback_query(F.data.startswith("cart_plus_"))
async def cb_cart_plus(callback: CallbackQuery):
    user_id = callback.from_user.id
    article = callback.data.replace("cart_plus_", "", 1)

    product = get_product_by_article(article)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    ok = add_to_cart(user_id, product, 1)
    if not ok:
        await callback.answer(f"❗ Доступно только {product['stock']} шт", show_alert=True)
        return

    await callback.answer("Увеличено.")
    await send_cart(callback.message, user_id, edit=True)


@dp.callback_query(F.data.startswith("cart_minus_"))
async def cb_cart_minus(callback: CallbackQuery):
    user_id = callback.from_user.id
    article = callback.data.replace("cart_minus_", "", 1)

    change_cart_qty(user_id, article, -1)

    await callback.answer("Уменьшено.")
    await send_cart(callback.message, user_id, edit=True)


# -------------------------------------------
# CALLBACK: ОФОРМИТЬ ЗАКАЗ (PDF)
# -------------------------------------------
# -------------------------------------------
# CALLBACK: ОФОРМИТЬ ЗАКАЗ (PDF)
# -------------------------------------------
@dp.callback_query(F.data == "checkout")
async def checkout_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    cart = USER_CARTS.get(user_id, {})

    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    # ---- Регистрируем кириллические шрифты ----
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", "DejaVuSans-Bold.ttf"))

    styles = getSampleStyleSheet()

    # Правим все базовые стили
    for s in styles.byName:
        styles[s].fontName = "DejaVu"

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title="Заказ Моторешение")

    elems = []

    # Заголовок
    elems.append(Paragraph("<b>Заказ Моторешение</b>", styles["Title"]))
    elems.append(Spacer(1, 12))

    elems.append(Paragraph(
        f"Дата: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}",
        styles["Normal"]
    ))
    user_label = callback.from_user.username or f"id {user_id}"
    elems.append(Paragraph(f"Клиент: @{user_label}", styles["Normal"]))
    elems.append(Spacer(1, 20))

        # ------------------ Таблица ------------------
    table_data = [
        [
            "Фото",
            "Артикул",
            "Название",
            "Кол-во",
            "Цена",
            "Сумма"
        ]
    ]

    total_sum = 0

    for article, item in cart.items():
        name = item["name"]
        qty = item["qty"]
        price = item["price_opt"]
        subtotal = qty * price
        total_sum += subtotal

        product = get_product_by_article(article)
        photo_url = product["photo_url"] if product else ""

        # ---- Фото 50x50 ----
        if photo_url.startswith("http"):
            try:
                resp = requests.get(photo_url, timeout=5)
                img_bytes = io.BytesIO(resp.content)
                img_obj = Image(img_bytes, width=50, height=50)
            except:
                img_obj = Paragraph("Нет фото", styles["Normal"])
        else:
            img_obj = Paragraph("Нет фото", styles["Normal"])

        # ---- Название с переносами ----
        name_paragraph = Paragraph(name, styles["Normal"])

        # ---- Добавляем строку ----
        table_data.append([
            img_obj,
            article,
            name_paragraph,
            Paragraph(f"{qty}", styles["Normal"]),
            Paragraph(f"{price} ₽", styles["Normal"]),
            Paragraph(f"{subtotal} ₽", styles["Normal"]),
        ])

    # Создаём таблицу
    table = Table(table_data, colWidths=[60, 55, 180, 50, 55, 60])

    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (3, 1), (-1, -1), "CENTER"),

        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),

        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
    ]))

    elems.append(table)
    elems.append(Spacer(1, 20))
    elems.append(Paragraph(f"<b>Итого: {total_sum} ₽</b>", styles["Heading2"]))

    # Сгенерировать PDF
    doc.build(elems)

    buffer.seek(0)
    pdf_bytes = buffer.getvalue()

    # Один и тот же контент в двух объектах для клиента и админа
    file_for_user = BufferedInputFile(pdf_bytes, filename="Заказ Моторешение.pdf")
    file_for_admin = BufferedInputFile(pdf_bytes, filename="Заказ Моторешение.pdf")

    # 1) Отправляем клиенту
    await callback.message.answer_document(
        document=file_for_user,
        caption="📄 Ваш заказ сформирован!",
    )

    # 2) Отправляем админу
    await bot.send_document(
        ADMIN_ID,
        document=file_for_admin,
        caption=(
            "📥 Новый заказ из бота\n"
            f"Клиент: {callback.from_user.full_name}\n"
            f"Username: @{callback.from_user.username}\n"
            f"ID: {callback.from_user.id}"
        ),
    )

    await callback.answer("PDF заказ сформирован!")


# -------------------------------------------
# RUN
# -------------------------------------------
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())