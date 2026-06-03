import aiosqlite
from typing import List, Dict, Any, Optional

DB_PATH = "shop.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                items TEXT,
                total INTEGER,
                status TEXT DEFAULT 'awaiting_payment',
                customer_name TEXT,
                customer_phone TEXT,
                customer_address TEXT,
                comment TEXT,
                delivery_method TEXT DEFAULT 'delivery',
                delivery_fee INTEGER DEFAULT 0,
                bonus_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_bonuses (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                welcome_bonus_given INTEGER DEFAULT 0,
                support_message_sent INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bonus_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                sale_price INTEGER,
                description TEXT,
                image TEXT,
                images TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER,
                product_id INTEGER,
                quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                address TEXT,
                is_default BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # миграции старых баз
        for col, dtype in [("delivery_fee", "INTEGER DEFAULT 0"),
                           ("welcome_bonus_given", "INTEGER DEFAULT 0"),
                           ("support_message_sent", "INTEGER DEFAULT 0")]:
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {dtype}")
            except:
                pass
            try:
                await db.execute(f"ALTER TABLE user_bonuses ADD COLUMN {col} {dtype}")
            except:
                pass
        await db.commit()

# ---------- Заказы ----------
async def create_order(user_id: int, items_json: str, total: int, name: str, phone: str,
                       address: str, comment: str = "", delivery_method: str = "delivery",
                       bonus_used: int = 0, delivery_fee: int = 0) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO orders (user_id, items, total, customer_name, customer_phone,
                               customer_address, comment, delivery_method, bonus_used, delivery_fee)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, items_json, total, name, phone, address, comment, delivery_method,
              bonus_used, delivery_fee))
        await db.commit()
        return cursor.lastrowid

async def get_order(order_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_orders_by_user(user_id: int, limit: int = 20) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def update_order_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()

# ---------- Бонусы ----------
async def get_user_bonus(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM user_bonuses WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def add_bonus(user_id: int, amount: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_bonuses (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?
        """, (user_id, amount, amount))
        await db.execute("INSERT INTO bonus_transactions (user_id, amount, reason) VALUES (?, ?, ?)", (user_id, amount, reason))
        await db.commit()

async def deduct_bonus(user_id: int, amount: int, reason: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        balance = await get_user_bonus(user_id)
        if balance < amount:
            return False
        await db.execute("UPDATE user_bonuses SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        await db.execute("INSERT INTO bonus_transactions (user_id, amount, reason) VALUES (?, ?, ?)", (user_id, -amount, reason))
        await db.commit()
        return True

async def get_welcome_bonus_status(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT welcome_bonus_given FROM user_bonuses WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def set_welcome_bonus_given(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO user_bonuses (user_id, balance, welcome_bonus_given) VALUES (?, 0, 1) ON CONFLICT(user_id) DO UPDATE SET welcome_bonus_given = 1", (user_id,))
        await db.commit()

async def get_support_sent_status(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT support_message_sent FROM user_bonuses WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def set_support_sent_status(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO user_bonuses (user_id, balance, welcome_bonus_given, support_message_sent) VALUES (?, 0, 0, 1) ON CONFLICT(user_id) DO UPDATE SET support_message_sent = 1", (user_id,))
        await db.commit()

async def get_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM user_bonuses") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

# ---------- Товары (кэш) ----------
async def sync_products(products_list: List[Dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        for p in products_list:
            await db.execute("""
                INSERT OR REPLACE INTO products (id, name, price, sale_price, description, image, images)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (p['id'], p['name'], p['price'], p.get('sale_price'), p['description'], p.get('image', ''), p.get('images', '')))
        await db.commit()

async def get_all_products() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM products")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

# ---------- Корзина ----------
async def add_to_cart(user_id: int, product_id: int, quantity: int = 1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO cart (user_id, product_id, quantity) VALUES (?, ?, ?)
            ON CONFLICT(user_id, product_id) DO UPDATE SET quantity = quantity + ?
        """, (user_id, product_id, quantity, quantity))
        await db.commit()

async def get_cart(user_id: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT c.product_id, c.quantity, p.name, p.price, p.image
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
        """, (user_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def clear_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        await db.commit()

async def remove_from_cart(user_id: int, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart WHERE user_id = ? AND product_id = ?", (user_id, product_id))
        await db.commit()

# ---------- Адреса ----------
async def save_user_address(user_id: int, address: str, is_default: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        if is_default:
            await db.execute("UPDATE user_addresses SET is_default = 0 WHERE user_id = ?", (user_id,))
        await db.execute("INSERT INTO user_addresses (user_id, address, is_default) VALUES (?, ?, ?)", (user_id, address, is_default))
        await db.commit()

async def get_user_addresses(user_id: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM user_addresses WHERE user_id = ? ORDER BY is_default DESC, created_at DESC", (user_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
