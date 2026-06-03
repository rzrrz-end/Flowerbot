import asyncio
import json
import logging
import os
import httpx
import urllib.parse
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.web_app import check_webapp_signature

from config import BOT_TOKEN, ADMIN_IDS, WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET
from database import (
    init_db, create_order, get_user_bonus, add_bonus, deduct_bonus,
    update_order_status, get_orders_by_user, get_order, sync_products,
    get_welcome_bonus_status, set_welcome_bonus_given,
    get_support_sent_status, set_support_sent_status, get_all_user_ids
)
from tbank import create_payment

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
WEBAPP_URL = "https://bot.vereskflowers.ru"

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# ---------- Форматирование уведомлений ----------
def format_order_message(order_id, total, name, phone, address, delivery_text, comment, cart, payment_method):
    cart_text = ""
    for item in cart:
        cart_text += f"• {item['name']} x{item['quantity']} = {item['price']*item['quantity']} ₽\n"
    payment_text = "Наличные при получении" if payment_method == "cash" else "Онлайн (Т-Банк)"
    return (
        f"🛒 <b>Заказ #{order_id}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Покупатель:</b> {name}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"📍 <b>Адрес:</b> {address}\n"
        f"🚚 <b>Доставка:</b> {delivery_text}\n"
        f"💬 <b>Комментарий:</b> {comment or 'нет'}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Состав заказа:</b>\n"
        f"{cart_text}"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Сумма:</b> {total} ₽\n"
        f"💳 <b>Оплата:</b> {payment_text}"
    )

# ---------- Синхронизация с WooCommerce ----------
WOO_API_URL = "https://vereskflowers.ru/wp-json/wc/v3/orders"

async def create_woocommerce_order(order_data: dict):
    line_items = []
    for item in order_data.get("cart", []):
        li = {
            "product_id": item.get("product_id", item["id"]),
            "quantity": item["quantity"]
        }
        if item.get("variation_id"):
            li["variation_id"] = item["variation_id"]
        line_items.append(li)

    payload = {
        "payment_method": "tbank",
        "payment_method_title": "Онлайн-оплата (Т-Банк)" if order_data.get("payment_method") == "card" else "Наличные",
        "set_paid": False,
        "billing": {"first_name": order_data.get("customer_name", ""), "phone": order_data.get("customer_phone", "")},
        "shipping": {"first_name": order_data.get("customer_name", ""), "address_1": order_data.get("customer_address", "")},
        "line_items": line_items,
        "customer_note": order_data.get("comment", ""),
        "meta_data": [{"key": "_telegram_order_id", "value": str(order_data.get("order_id"))}]
    }
    async with httpx.AsyncClient(auth=(WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET)) as client:
        resp = await client.post(WOO_API_URL, json=payload)
        data = resp.json()
        logging.info(f"Ответ WooCommerce: {data}")
        if resp.status_code not in (200, 201):
            raise Exception(f"Ошибка WC API: {data}")

# ---------- Сообщение поддержки (отложенное) ----------
async def send_support_message(chat_id: int):
    await asyncio.sleep(300)
    try:
        msg = await bot.send_message(chat_id,
            "🆘 Если возникли трудности, обратитесь в Тех. Поддержку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📞 Поддержка", url="https://t.me/ваш_чат_поддержки")]
            ])
        )
        await bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id)
        await set_support_sent_status(chat_id)
    except Exception as e:
        logging.warning(f"Не удалось отправить/закрепить сообщение поддержки: {e}")

# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    welcome_given = await get_welcome_bonus_status(user_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌸 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])

    if not welcome_given:
        await add_bonus(user_id, 500, "Приветственные бонусы")
        await set_welcome_bonus_given(user_id)
        await message.answer(
            "🌼 Добро пожаловать!\nВам начислено 500 бонусов.\n\nНажмите кнопку, чтобы начать покупки.",
            reply_markup=kb
        )
    else:
        await message.answer("🌼 С возвращением!\nНажмите кнопку, чтобы начать покупки.", reply_markup=kb)

    try:
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonWebApp(text="🛍 Магазин", web_app=WebAppInfo(url=WEBAPP_URL)))
    except:
        pass

    support_sent = await get_support_sent_status(user_id)
    if not support_sent:
        asyncio.create_task(send_support_message(message.chat.id))

@dp.message(Command("bonus"))
async def cmd_bonus(message: types.Message):
    balance = await get_user_bonus(message.from_user.id)
    await message.answer(f"🎁 Ваш бонусный баланс: {balance} ₽")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users = await get_all_user_ids()
    await message.answer(f"👥 Всего пользователей бота: {len(users)}")

@dp.message(Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BroadcastState.waiting_for_message)
    await message.answer("📣 Введите текст для рассылки всем пользователям.\n/отмена – отменить.")

@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast_text(message: types.Message, state: FSMContext):
    if message.text == '/отмена':
        await state.clear()
        await message.answer("✅ Рассылка отменена.")
        return
    text = message.text
    users = await get_all_user_ids()
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Рассылка завершена. Отправлено {count} пользователям.")
    await state.clear()

# ---------- WebApp ----------
@dp.message(lambda message: message.web_app_data is not None)
async def web_app_data_handler(message: types.Message):
    data = json.loads(message.web_app_data.data)
    logging.info(f"Получены данные из мини-приложения: {data}")

    if data.get("action") == "payment_url":
        url = data.get("url")
        if url:
            await message.answer("Ссылка на оплату готова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить заказ", url=url)]
                ]))
        return
    if data.get("action") != "checkout":
        return

    user_id = message.from_user.id
    cart = data.get("cart", [])
    total = data.get("total", 0)
    name = data.get("customer_name", "")
    phone = data.get("customer_phone", "")
    address = data.get("customer_address", "")
    comment = data.get("comment", "")
    delivery_method = data.get("delivery_method", "pickup")
    delivery_fee = data.get("delivery_fee", 0)
    bonus_used = data.get("bonus_used", 0)
    payment_method = data.get("payment_method", "cash")

    if not name or not phone or not cart:
        await message.answer("❌ Заполните все обязательные поля (имя, телефон).")
        return
    if delivery_method != "pickup" and not address:
        await message.answer("❌ Для доставки укажите адрес.")
        return
    if bonus_used > 0 and not await deduct_bonus(user_id, bonus_used, f"Списание по заказу"):
        await message.answer("❌ Не удалось списать бонусы.")
        return

    items_json = json.dumps(cart, ensure_ascii=False)
    order_id = await create_order(user_id, items_json, total, name, phone, address,
                                  comment, delivery_method, bonus_used, delivery_fee)

    delivery_names = {
        "pickup": "Самовывоз",
        "mkad": "Доставка в пределах МКАД",
        "express": "Экспресс доставка 120 мин",
        "outside_mkad": "Доставка за МКАД"
    }
    delivery_text = delivery_names.get(delivery_method, delivery_method)

    if payment_method == "cash":
        await update_order_status(order_id, "awaiting_cash")
        await message.answer(f"✅ Заказ #{order_id} принят! Сумма: {total} ₽. Наличные при получении.")
        await bot.send_message(user_id, f"✅ Ваш заказ №{order_id} на сумму {total} ₽ принят! Ожидайте связи от менеджера.")
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, format_order_message(order_id, total, name, phone, address, delivery_text, comment, cart, payment_method), parse_mode="HTML")
        await create_woocommerce_order({"cart":cart,"total":total,"customer_name":name,"customer_phone":phone,"customer_address":address,"comment":comment,"delivery_method":delivery_method,"payment_method":payment_method,"order_id":order_id})
        return

    try:
        payment_url = await create_payment(str(order_id), total, f"Оплата заказа #{order_id}", cart=cart, customer_email=data.get("customer_email",""), customer_phone=phone)
        await message.answer(f"✅ Заказ #{order_id} создан! Сумма к оплате: {total} ₽.\nНажмите кнопку для оплаты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить заказ", url=payment_url)]
            ]))
        await bot.send_message(user_id, f"✅ Ваш заказ №{order_id} на сумму {total} ₽ ожидает оплаты.")
        await create_woocommerce_order({"cart":cart,"total":total,"customer_name":name,"customer_phone":phone,"customer_address":address,"comment":comment,"delivery_method":delivery_method,"payment_method":payment_method,"order_id":order_id})
    except Exception as e:
        if bonus_used > 0:
            await add_bonus(user_id, bonus_used, "Возврат бонусов (ошибка оплаты)")
        await message.answer(f"❌ Ошибка создания платежа: {e}")

# ---------- API ----------
async def api_categories(request):
    import aiohttp
    url = f"https://vereskflowers.ru/wp-json/wc/v3/products/categories?consumer_key={WOO_CONSUMER_KEY}&consumer_secret={WOO_CONSUMER_SECRET}&per_page=100&hide_empty=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return web.json_response({"error": "Ошибка загрузки категорий"}, status=500)
            data = await resp.json()
    return web.json_response([{"id": c["id"], "name": c["name"]} for c in data])

async def api_products(request):
    import aiohttp
    category = request.rel_url.query.get('category')
    orderby = request.rel_url.query.get('orderby', 'id')
    order = request.rel_url.query.get('order', 'asc')
    all_products = []
    page = 1
    while True:
        url = f"https://vereskflowers.ru/wp-json/wc/v3/products?consumer_key={WOO_CONSUMER_KEY}&consumer_secret={WOO_CONSUMER_SECRET}&per_page=100&page={page}&status=publish"
        if category: url += f"&category={category}"
        if orderby == 'price': url += f"&orderby=price&order={order}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: break
                data = await resp.json()
                if not data: break
                all_products.extend(data)
        page += 1

    products = []
    for p in all_products:
        images = [img["src"] for img in p.get("images", [])] if p.get("images") else []
        if not images and p.get("image"): images = [p["image"]]
        price = int(float(p.get("price", 0))) if p.get("price") else 0
        sale_price = int(float(p["sale_price"])) if p.get("sale_price") else None
        regular_price = int(float(p["regular_price"])) if p.get("regular_price") else None
        products.append({
            "id": p["id"],
            "name": p["name"],
            "price": price,
            "sale_price": sale_price,
            "regular_price": regular_price,
            "description": (p.get("short_description") or p.get("description") or "").strip(),
            "image": images[0] if images else "",
            "images": json.dumps(images),
            "stock_status": p.get("stock_status", "instock")
        })
    await sync_products(products)
    for p in products: p['images'] = json.loads(p['images'])
    return web.json_response(products)

async def api_product_variations(request):
    import aiohttp
    product_id = request.rel_url.query.get('id')
    if not product_id:
        return web.json_response({"error": "product id required"}, status=400)
    url = f"https://vereskflowers.ru/wp-json/wc/v3/products/{product_id}/variations?consumer_key={WOO_CONSUMER_KEY}&consumer_secret={WOO_CONSUMER_SECRET}&per_page=100"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return web.json_response({"error": f"Ошибка загрузки вариаций: {resp.status}"}, status=500)
            data = await resp.json()
    unique_sizes = {}
    for v in data:
        size = ""
        for attr in v.get("attributes", []):
            if attr["name"].lower() in ("pa_size", "size", "размер"):
                size = attr["option"]
                break
        if not size:
            for attr in v.get("attributes", []):
                size = attr["option"]
                break
        size = size.strip().lower()
        if size and size not in unique_sizes:
            unique_sizes[size] = {
                "id": v["id"],
                "price": int(float(v["price"])),
                "size": size.capitalize(),
                "stock_status": v.get("stock_status", "instock")
            }
    variations = sorted(unique_sizes.values(), key=lambda x: x["size"])
    return web.json_response(variations)

async def api_bulk_variations(request):
    import aiohttp
    data = await request.json()
    product_ids = data.get("ids", [])
    if not product_ids:
        return web.json_response({"error": "ids required"}, status=400)
    result = {}
    for pid in product_ids:
        url = f"https://vereskflowers.ru/wp-json/wc/v3/products/{pid}/variations?consumer_key={WOO_CONSUMER_KEY}&consumer_secret={WOO_CONSUMER_SECRET}&per_page=100"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    variations = await resp.json()
            clean = []
            for v in variations:
                size = ""
                for attr in v.get("attributes", []):
                    if attr["name"].lower() in ("pa_size", "size", "размер"):
                        size = attr["option"]
                        break
                if not size:
                    for attr in v.get("attributes", []):
                        size = attr["option"]
                        break
                if size:
                    clean.append({"id": v["id"], "size": size, "price": int(float(v["price"]))})
            # убираем дубликаты, оставляя минимальную цену
            unique = {}
            for v in clean:
                key = v["size"].strip().lower()
                if key not in unique or v["price"] < unique[key]["price"]:
                    unique[key] = v
            result[str(pid)] = list(unique.values())
        except:
            pass
    return web.json_response(result)

async def api_user(request):
    user_id = request.rel_url.query.get('user_id')
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    user_id = int(user_id)
    balance = await get_user_bonus(user_id)
    orders = await get_orders_by_user(user_id)
    for o in orders: o['created_at'] = str(o['created_at'])
    return web.json_response({"bonus": balance, "orders": orders})

async def create_order_handler(request):
    data = await request.json()
    logging.info(f"Получен запрос на создание заказа: {data}")
    user_id = data.get('user_id')
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    cart = data.get('cart', [])
    total = data.get('total', 0)
    name = data.get('customer_name', '')
    phone = data.get('customer_phone', '')
    address = data.get('customer_address', '')
    comment = data.get('comment', '')
    delivery_method = data.get('delivery_method', 'pickup')
    delivery_fee = data.get('delivery_fee', 0)
    bonus_used = data.get('bonus_used', 0)
    payment_method = data.get('payment_method', 'cash')
    email = data.get('customer_email', '')

    if not name or not phone or not cart:
        return web.json_response({"error": "Не все данные заполнены"}, status=400)
    if delivery_method != "pickup" and not address:
        return web.json_response({"error": "Укажите адрес доставки"}, status=400)

    if bonus_used > 0 and not await deduct_bonus(user_id, bonus_used, f"Списание по заказу"):
        return web.json_response({"error": "Не удалось списать бонусы"}, status=400)

    items_json = json.dumps(cart, ensure_ascii=False)
    order_id = await create_order(user_id, items_json, total, name, phone, address, comment, delivery_method, bonus_used, delivery_fee)

    delivery_names = {"pickup":"Самовывоз","mkad":"Доставка в пределах МКАД","express":"Экспресс доставка 120 мин","outside_mkad":"Доставка за МКАД"}
    delivery_text = delivery_names.get(delivery_method, delivery_method)

    await create_woocommerce_order({"cart":cart,"total":total,"customer_name":name,"customer_phone":phone,"customer_address":address,"comment":comment,"delivery_method":delivery_method,"payment_method":payment_method,"order_id":order_id})

    if payment_method == "cash":
        await bot.send_message(user_id, f"✅ Ваш заказ №{order_id} на сумму {total} ₽ принят! Ожидайте связи от менеджера.")
        await update_order_status(order_id, "awaiting_cash")
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, format_order_message(order_id, total, name, phone, address, delivery_text, comment, cart, payment_method), parse_mode="HTML")
        return web.json_response({"order_id": order_id, "status": "cash"})

    payment_url = await create_payment(str(order_id), total, f"Оплата заказа #{order_id}", cart=cart, customer_email=email, customer_phone=phone)
    await bot.send_message(user_id, f"✅ Ваш заказ №{order_id} на сумму {total} ₽ ожидает оплаты.")
    return web.json_response({"payment_url": payment_url, "order_id": order_id})

async def tbank_webhook(request):
    data = await request.json()
    logging.info(f"Вебхук Т-Банка: {data}")
    if data.get("Status") == "CONFIRMED" and data.get("OrderId"):
        order_id = int(data["OrderId"])
        await update_order_status(order_id, "paid")
        order_info = await get_order(order_id)
        if order_info:
            user_id = order_info['user_id']
            total = order_info['total']
            bonus = int(total * 0.05)
            if bonus > 0:
                await add_bonus(user_id, bonus, f"5% от заказа #{order_id}")
            await bot.send_message(user_id, f"✅ Ваш заказ №{order_id} успешно оплачен. Мы начинаем сборку!")
            # уведомление админам
            cart = json.loads(order_info['items'])
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, format_order_message(order_id, total, order_info['customer_name'], order_info['customer_phone'], order_info['customer_address'], order_info.get('delivery_method',''), order_info.get('comment',''), cart, "card") + f"\n✅ Заказ оплачен. Начислено {bonus} бонусов.", parse_mode="HTML")
    return web.Response(status=200)

async def handle_webhook(request):
    update = types.Update(**(await request.json()))
    await dp.feed_update(bot, update)
    return web.Response()

async def serve_index(request):
    path = os.path.join(os.path.dirname(__file__), 'index.html')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except:
        return web.Response(text="Mini app is running")

async def on_startup():
    await init_db()
    await bot.set_webhook("https://bot.vereskflowers.ru/webhook")
    logging.info("Бот запущен (webhook)")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get('/', serve_index)
    app.router.add_get('/api/categories', api_categories)
    app.router.add_get('/api/products', api_products)
    app.router.add_get('/api/product_variations', api_product_variations)
    app.router.add_post('/api/bulk_variations', api_bulk_variations)
    app.router.add_get('/api/user', api_user)
    app.router.add_post('/api/create_order', create_order_handler)
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_post('/tbank-webhook', tbank_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logging.info("Веб-сервер запущен на порту 8080")

    await on_startup()
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
