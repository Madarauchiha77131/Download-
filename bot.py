# bot.py
import asyncio
import logging
import sqlite3
import os
from datetime import datetime
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, FSInputFile
from aiogram.exceptions import TelegramBadRequest

# --- CONFIGURATION ---
BOT_TOKEN = "7829302766:AAEtOFAZ6BF9P_Q6I1PxtpG-7i_Y0SJHe14"  # Replace with your bot token
ADMIN_ID = 6798566345  # Replace with your Telegram user ID
PRIVATE_CHANNEL_ID = -1002740009398  # Replace with your private channel ID (negative number)
CHANNEL_INVITE_LINK = "https://t.me/+1mW3DLmVYUQ3MWRl"  # Replace with your channel invite link
SUPPORT_USERNAME = "obito_uchiha77"  # Replace with your Telegram username

# --- DATABASE SETUP ---
class Database:
    def __init__(self, db_path="shop_bot.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    orders_count INTEGER DEFAULT 0,
                    joined_date TEXT,
                    last_active TEXT
                )
            """)
            
            # Products table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    stock INTEGER NOT NULL,
                    description TEXT
                )
            """)
            
            # Orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_id INTEGER,
                    product_name TEXT,
                    price REAL,
                    utr TEXT,
                    screenshot_file_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    approved_at TEXT
                )
            """)
            
            # Stats table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    total_income REAL DEFAULT 0,
                    total_orders INTEGER DEFAULT 0
                )
            """)
            
            # Initialize stats if empty
            cursor.execute("SELECT COUNT(*) FROM stats")
            if cursor.fetchone()[0] == 0:
                cursor.execute("INSERT INTO stats (total_income, total_orders) VALUES (0, 0)")
            
            conn.commit()

    # User methods
    def add_user(self, user_id: int, username: str, full_name: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, full_name, joined_date, last_active)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, full_name, datetime.now().isoformat(), datetime.now().isoformat()))
            conn.commit()

    def get_user(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()

    def increment_user_orders(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET orders_count = orders_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_all_users(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in cursor.fetchall()]

    # Product methods
    def add_product(self, name: str, price: float, stock: int, description: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO products (name, price, stock, description)
                VALUES (?, ?, ?, ?)
            """, (name, price, stock, description))
            conn.commit()
            return cursor.lastrowid

    def get_products(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT product_id, name, price, stock, description FROM products WHERE stock > 0")
            return cursor.fetchall()

    def get_all_products(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT product_id, name, price, stock, description FROM products")
            return cursor.fetchall()

    def get_product(self, product_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT product_id, name, price, stock, description FROM products WHERE product_id = ?", (product_id,))
            return cursor.fetchone()

    def update_stock(self, product_id: int, quantity: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE products SET stock = stock + ? WHERE product_id = ?", (quantity, product_id))
            conn.commit()

    def reduce_stock(self, product_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE products SET stock = stock - 1 WHERE product_id = ? AND stock > 0", (product_id,))
            conn.commit()

    # Order methods
    def add_order(self, user_id: int, product_id: int, product_name: str, price: float, utr: str, screenshot_file_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO orders (user_id, product_id, product_name, price, utr, screenshot_file_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, product_id, product_name, price, utr, screenshot_file_id, datetime.now().isoformat()))
            conn.commit()
            return cursor.lastrowid

    def get_pending_orders(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT order_id, user_id, product_name, price, utr, screenshot_file_id, created_at
                FROM orders WHERE status = 'pending'
            """)
            return cursor.fetchall()

    def approve_order(self, order_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE orders SET status = 'approved', approved_at = ? WHERE order_id = ?", 
                         (datetime.now().isoformat(), order_id))
            conn.commit()

    def decline_order(self, order_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE orders SET status = 'declined' WHERE order_id = ?", (order_id,))
            conn.commit()

    def get_order(self, order_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            return cursor.fetchone()

    # Stats methods
    def update_stats(self, income: float):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE stats 
                SET total_income = total_income + ?, total_orders = total_orders + 1
                WHERE stat_id = 1
            """, (income,))
            conn.commit()

    def get_stats(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT total_income, total_orders FROM stats WHERE stat_id = 1")
            return cursor.fetchone()

db = Database()

# --- STATE MACHINES ---
class AddProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_stock = State()
    waiting_for_description = State()

class AddStockStates(StatesGroup):
    waiting_for_product = State()
    waiting_for_quantity = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()

class PaymentStates(StatesGroup):
    waiting_for_utr = State()
    waiting_for_screenshot = State()

# --- KEYBOARDS ---
def get_force_join_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔗 JOIN CHANNEL", url=CHANNEL_INVITE_LINK)],
        [InlineKeyboardButton(text="✅ VERIFY", callback_data="verify_membership")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🏪 SHOP", callback_data="shop")],
        [InlineKeyboardButton(text="👤 MY PROFILE", callback_data="profile")],
        [InlineKeyboardButton(text="🆘 SUPPORT", callback_data="support")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard():
    buttons = [[InlineKeyboardButton(text="🔙 BACK", callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_products_keyboard(products):
    buttons = []
    for product in products:
        product_id, name, price, stock, _ = product
        button_text = f"📦 {name} 🎨 ({stock} left)" if stock > 0 else f"📦 {name} ❌ Out of Stock"
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"product_{product_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 BACK", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_product_detail_keyboard(product_id):
    buttons = [
        [InlineKeyboardButton(text="🛍 BUY", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton(text="🔙 BACK", callback_data="shop")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ PAID", callback_data="payment_paid")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_panel_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➕ ADD PRODUCT", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="📦 ADD STOCK", callback_data="admin_add_stock")],
        [InlineKeyboardButton(text="📊 STATS", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 BROADCAST", callback_data="admin_broadcast")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_approve_keyboard(order_id):
    buttons = [
        [InlineKeyboardButton(text="✅ APPROVE", callback_data=f"approve_{order_id}")],
        [InlineKeyboardButton(text="❌ DECLINE", callback_data=f"decline_{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- BOT INITIALIZATION ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- MIDDLEWARE FOR FORCE JOIN ---
async def is_user_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=PRIVATE_CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# --- HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    if await is_user_member(user_id):
        db.add_user(user_id, message.from_user.username, message.from_user.full_name)
        await message.answer(
            f"🛍 Welcome to Rexovaan Shop!\n\n"
            f"Hey {message.from_user.first_name}! 👋\n\n"
            f"We offer premium digital products at the best prices.\n"
            f"Fast, secure, and manual delivery.\n\n"
            f"🏪 Shop — Browse & buy products\n"
            f"👤 My Profile — Your info\n"
            f"🆘 Support — Get help\n\n"
            f"👇 Choose an option",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await message.answer(
            "🚫 Please join our channel first\n\n"
            "📢 You must join our private channel to use this bot\n\n"
            "👇 Join and then click VERIFY",
            reply_markup=get_force_join_keyboard()
        )

@dp.callback_query(F.data == "verify_membership")
async def verify_membership(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if await is_user_member(user_id):
        db.add_user(user_id, callback.from_user.username, callback.from_user.full_name)
        await callback.message.edit_text(
            f"🛍 Welcome to Rexovaan Shop!\n\n"
            f"Hey {callback.from_user.first_name}! 👋\n\n"
            f"We offer premium digital products at the best prices.\n"
            f"Fast, secure, and manual delivery.\n\n"
            f"🏪 Shop — Browse & buy products\n"
            f"👤 My Profile — Your info\n"
            f"🆘 Support — Get help\n\n"
            f"👇 Choose an option",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await callback.answer("❌ You haven't joined the channel yet!", show_alert=True)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        f"🛍 Welcome back!\n\n"
        f"🏪 Shop — Browse & buy products\n"
        f"👤 My Profile — Your info\n"
        f"🆘 Support — Get help\n\n"
        f"👇 Choose an option",
        reply_markup=get_main_menu_keyboard()
    )

@dp.callback_query(F.data == "shop")
async def show_shop(callback: CallbackQuery):
    products = db.get_products()
    
    if not products:
        await callback.message.edit_text(
            "🛒 No products available right now",
            reply_markup=get_back_keyboard()
        )
    else:
        await callback.message.edit_text(
            "🛍 Our Products:\n\n👇 Click on any product to view details",
            reply_markup=get_products_keyboard(products)
        )

@dp.callback_query(F.data.startswith("product_"))
async def show_product_detail(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    product = db.get_product(product_id)
    
    if product:
        product_id, name, price, stock, description = product
        await callback.message.edit_text(
            f"📦 {name}\n\n"
            f"💵 Price: ₹{price}\n"
            f"📦 Stock: {stock}\n\n"
            f"📝 {description}\n\n"
            f"👇 Choose an option",
            reply_markup=get_product_detail_keyboard(product_id)
        )
    else:
        await callback.answer("Product not found!", show_alert=True)

@dp.callback_query(F.data.startswith("buy_"))
async def start_payment(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[1])
    product = db.get_product(product_id)
    
    if product and product[3] > 0:  # Check stock
        await state.update_data(product_id=product_id, product_name=product[1], product_price=product[2])
        await callback.message.edit_text(
            f"💳 Payment Details\n\n"
            f"📦 Product: {product[1]}\n"
            f"💵 Amount: ₹{product[2]}\n\n"
            f"🏦 UPI ID: rexovaan@okaxis\n\n"
            f"📌 Send exact amount\n"
            f"📌 After payment click PAID",
            reply_markup=get_payment_keyboard()
        )
    else:
        await callback.answer("❌ Product out of stock!", show_alert=True)

@dp.callback_query(F.data == "payment_paid")
async def payment_paid(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📤 Payment Verification\n\n"
        "Please send:\n"
        "1. UTR Number (as text message)\n"
        "2. Payment Screenshot (as photo)\n\n"
        "⚠️ Wrong info will lead to rejection\n\n"
        "Please send your UTR number first:"
    )
    await state.set_state(PaymentStates.waiting_for_utr)

@dp.message(PaymentStates.waiting_for_utr)
async def receive_utr(message: Message, state: FSMContext):
    utr = message.text
    await state.update_data(utr=utr)
    await message.answer(
        "✅ UTR received!\n\n"
        "Now please send the payment screenshot as a photo:"
    )
    await state.set_state(PaymentStates.waiting_for_screenshot)

@dp.message(PaymentStates.waiting_for_screenshot, F.photo)
async def receive_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    utr = data.get('utr')
    product_id = data.get('product_id')
    product_name = data.get('product_name')
    product_price = data.get('product_price')
    
    screenshot_file_id = message.photo[-1].file_id
    
    # Save order
    order_id = db.add_order(
        user_id=message.from_user.id,
        product_id=product_id,
        product_name=product_name,
        price=product_price,
        utr=utr,
        screenshot_file_id=screenshot_file_id
    )
    
    # Notify user
    await message.answer(
        "✅ Payment information submitted!\n\n"
        "Your order is being verified by admin.\n"
        "You will receive a confirmation shortly.",
        reply_markup=get_back_keyboard()
    )
    
    # Get user info
    user = message.from_user
    user_profile_link = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    
    # Notify admin
    admin_text = (
        f"🆕 New Payment Request\n\n"
        f"👤 Name: {user.full_name}\n"
        f"🔗 Profile: {user_profile_link}\n"
        f"🆔 User ID: {user.id}\n\n"
        f"📦 Product: {product_name}\n"
        f"💵 Amount: ₹{product_price}\n\n"
        f"🔢 UTR: {utr}"
    )
    
    # Send to admin with photo
    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=screenshot_file_id,
            caption=admin_text,
            reply_markup=get_approve_keyboard(order_id)
        )
    except Exception as e:
        logging.error(f"Failed to send to admin: {e}")
    
    await state.clear()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    order = db.get_order(order_id)
    
    if order:
        user_id = order[1]
        product_id = order[2]
        product_name = order[3]
        price = order[4]
        
        # Update database
        db.approve_order(order_id)
        db.reduce_stock(product_id)
        db.update_stats(price)
        db.increment_user_orders(user_id)
        
        await callback.answer("✅ Order approved!")
        await callback.message.edit_text(
            callback.message.caption + "\n\n✅ APPROVED by admin",
            reply_markup=None
        )
        
        # Notify user
        await bot.send_message(
            user_id,
            f"✅ Payment Approved!\n\n"
            f"📦 Your product is ready\n"
            f"📬 Sending shortly..."
        )
        
        # Get user info for delivery
        user = await bot.get_chat(user_id)
        
        # Forward the admin's reply system is handled separately
        await callback.message.answer(
            f"💡 You can now reply to this message to deliver the product to the user.\n"
            f"User: {user.first_name} (ID: {user_id})"
        )
    else:
        await callback.answer("Order not found!", show_alert=True)

@dp.callback_query(F.data.startswith("decline_"))
async def decline_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    order = db.get_order(order_id)
    
    if order:
        user_id = order[1]
        db.decline_order(order_id)
        
        await callback.answer("❌ Order declined!")
        await callback.message.edit_text(
            callback.message.caption + "\n\n❌ DECLINED by admin",
            reply_markup=None
        )
        
        # Notify user
        await bot.send_message(
            user_id,
            f"❌ Payment Declined\n\n"
            f"Your payment could not be verified.\n"
            f"Contact support if needed."
        )
    else:
        await callback.answer("Order not found!", show_alert=True)

@dp.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    user_data = db.get_user(callback.from_user.id)
    
    if user_data:
        user_id, username, full_name, orders_count, joined_date, last_active = user_data
        await callback.message.edit_text(
            f"👤 My Profile\n\n"
            f"🆔 User ID: {user_id}\n"
            f"📛 Name: {full_name}\n"
            f"📦 Total Orders: {orders_count}\n"
            f"🗓 Joined: {joined_date[:10] if joined_date else 'Unknown'}",
            reply_markup=get_back_keyboard()
        )
    else:
        await callback.answer("User not found!", show_alert=True)

@dp.callback_query(F.data == "support")
async def show_support(callback: CallbackQuery):
    buttons = [[InlineKeyboardButton(text="💬 CONTACT ADMIN", url=f"https://t.me/{SUPPORT_USERNAME}")],
               [InlineKeyboardButton(text="🔙 BACK", callback_data="back_to_menu")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        "🆘 Support\n\nNeed help? Contact admin directly",
        reply_markup=keyboard
    )

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Unauthorized access!")
        return
    
    await message.answer(
        "🔧 Admin Panel\n\n"
        "Choose an action:",
        reply_markup=get_admin_panel_keyboard()
    )

@dp.callback_query(F.data.startswith("admin_"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Unauthorized!", show_alert=True)
        return
    
    action = callback.data.split("_")[1]
    
    if action == "addproduct":
        await callback.message.edit_text("📝 Send the product name (you can use emojis):")
        await state.set_state(AddProductStates.waiting_for_name)
    
    elif action == "addstock":
        products = db.get_all_products()
        if not products:
            await callback.message.edit_text("No products available!", reply_markup=get_back_keyboard())
            return
        
        buttons = []
        for product in products:
            product_id, name, price, stock, _ = product
            buttons.append([InlineKeyboardButton(text=f"{name} (Stock: {stock})", callback_data=f"stock_{product_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 BACK", callback_data="back_to_menu")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        
        await callback.message.edit_text("Select product to add stock:", reply_markup=keyboard)
        await state.set_state(AddStockStates.waiting_for_product)
    
    elif action == "stats":
        total_income, total_orders = db.get_stats()
        all_users = db.get_all_users()
        total_users = len(all_users)
        
        await callback.message.edit_text(
            f"📊 Bot Stats\n\n"
            f"💰 Total Income: ₹{total_income:.2f}\n"
            f"📦 Total Orders: {total_orders}\n"
            f"👥 Total Users: {total_users}",
            reply_markup=get_back_keyboard()
        )
    
    elif action == "broadcast":
        await callback.message.edit_text("📢 Send the message you want to broadcast to all users:")
        await state.set_state(BroadcastStates.waiting_for_message)

@dp.callback_query(AddStockStates.waiting_for_product, F.data.startswith("stock_"))
async def select_product_for_stock(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[1])
    await state.update_data(product_id=product_id)
    await callback.message.edit_text("Enter the quantity to add to stock:")
    await state.set_state(AddStockStates.waiting_for_quantity)

@dp.message(AddStockStates.waiting_for_quantity)
async def add_stock_quantity(message: Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity <= 0:
            raise ValueError
        
        data = await state.get_data()
        product_id = data.get('product_id')
        db.update_stock(product_id, quantity)
        
        await message.answer(f"✅ Added {quantity} items to stock!", reply_markup=get_back_keyboard())
        await state.clear()
    except:
        await message.answer("❌ Please enter a valid positive number!")

@dp.message(AddProductStates.waiting_for_name)
async def add_product_name(message: Message, state: FSMContext):
    await state.update_data(product_name=message.text)
    await message.answer("💰 Enter the product price (in ₹, numbers only):")
    await state.set_state(AddProductStates.waiting_for_price)

@dp.message(AddProductStates.waiting_for_price)
async def add_product_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(product_price=price)
        await message.answer("📦 Enter the initial stock quantity:")
        await state.set_state(AddProductStates.waiting_for_stock)
    except:
        await message.answer("❌ Please enter a valid number for price!")

@dp.message(AddProductStates.waiting_for_stock)
async def add_product_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
        await state.update_data(product_stock=stock)
        await message.answer("📝 Enter the product description:")
        await state.set_state(AddProductStates.waiting_for_description)
    except:
        await message.answer("❌ Please enter a valid number for stock!")

@dp.message(AddProductStates.waiting_for_description)
async def add_product_description(message: Message, state: FSMContext):
    data = await state.get_data()
    product_name = data.get('product_name')
    product_price = data.get('product_price')
    product_stock = data.get('product_stock')
    product_description = message.text
    
    # Save product
    product_id = db.add_product(product_name, product_price, product_stock, product_description)
    
    await message.answer(f"✅ Product added successfully!\n\n"
                        f"📦 {product_name}\n"
                        f"💰 ₹{product_price}\n"
                        f"📦 Stock: {product_stock}",
                        reply_markup=get_back_keyboard())
    
    # Notify all users about new product
    users = db.get_all_users()
    notification_text = (
        f"🆕 New Product Added!\n\n"
        f"📦 {product_name}\n"
        f"💵 ₹{product_price}\n"
        f"📦 Stock: {product_stock}\n\n"
        f"🛒 Check it in SHOP!"
    )
    
    for user_id in users:
        try:
            await bot.send_message(user_id, notification_text)
        except:
            pass
    
    await state.clear()

@dp.message(BroadcastStates.waiting_for_message)
async def send_broadcast(message: Message, state: FSMContext):
    users = db.get_all_users()
    success_count = 0
    
    await message.answer(f"📢 Broadcasting to {len(users)} users...")
    
    for user_id in users:
        try:
            await message.copy_to(user_id)
            success_count += 1
            await asyncio.sleep(0.05)  # Prevent flooding
        except:
            pass
    
    await message.answer(f"✅ Broadcast complete!\nSent to {success_count}/{len(users)} users",
                        reply_markup=get_back_keyboard())
    await state.clear()

# Handle admin replies for product delivery
@dp.message(F.reply_to_message)
async def handle_admin_delivery(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    # Check if replying to an order approval message
    if message.reply_to_message and "User: " in message.reply_to_message.text:
        try:
            # Extract user ID from the admin message
            text = message.reply_to_message.text
            user_id_part = text.split("User: ")[1]
            user_id = int(user_id_part.split(" (ID: ")[1].rstrip(")"))
            
            # Forward the reply to the user
            await message.copy_to(user_id)
            await message.reply(f"✅ Product delivered to user {user_id}!")
        except Exception as e:
            await message.reply(f"❌ Failed to deliver: {str(e)}")

@dp.callback_query()
async def handle_unknown_callback(callback: CallbackQuery):
    await callback.answer("Invalid option!", show_alert=True)

async def main():
    print("🤖 Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())