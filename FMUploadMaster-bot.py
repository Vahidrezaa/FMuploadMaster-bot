import time
import logging
import json
import uuid
import sqlite3
import asyncio
import os
from typing import Dict, List, Optional, Tuple
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    Document,
    PhotoSize,
    Video,
    Audio,
    Voice
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
from dotenv import load_dotenv
from datetime import datetime

import psycopg2 
from psycopg2 import sql 
from flask import Flask
# بارگذاری متغیرهای محیطی
load_dotenv()

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# حالت‌های گفتگو
UPLOADING, WAITING_FOR_CHANNEL_INFO, WAITING_FOR_INVITE_LINK = range(3)

def load_config() -> Tuple[str, str, List[int]]:
    """بارگذاری تنظیمات از متغیرهای محیطی"""
    bot_token = os.getenv('BOT_TOKEN')
    bot_username = os.getenv('BOT_USERNAME')
    admin_ids_str = os.getenv('ADMIN_IDS', '')
    
    # پردازش آیدی ادمین‌ها
    admin_ids = []
    if admin_ids_str:
        try:
            admin_ids = [int(id.strip()) for id in admin_ids_str.split(',') if id.strip()]
        except ValueError as e:
            logger.error(f"Error processing admin IDs: {e}")
    
    # بارگذاری از فایل config.json اگر وجود داشت
    if not admin_ids:
        config_path = 'config.json'
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    admin_ids = config.get('admin_ids', [])
            except Exception as e:
                logger.error(f"Error loading config file: {e}")
    
    # بررسی تنظیمات ضروری
    if not bot_token:
        raise ValueError("❌ BOT_TOKEN not found in environment variables!")
    if not bot_username:
        raise ValueError("❌ BOT_USERNAME not found in environment variables!")
    if not admin_ids:
        raise ValueError("❌ Admin IDs not found in environment variables or config file!")
    
    return bot_token, bot_username, admin_ids

# بارگذاری تنظیمات
try:
    BOT_TOKEN, BOT_USERNAME, ADMIN_IDS = load_config()
except ValueError as e:
    logger.error(f"Configuration error: {e}")
    exit(1)

class DatabaseManager:
    """مدیریت دیتابیس PostgreSQL بهینه شده"""
    
    def __init__(self):
        # اتصال به دیتابیس PostgreSQL
        self.conn = psycopg2.connect(os.getenv('DATABASE_URL'))
        self.conn.autocommit = False  # کنترل دستی commit
        self.init_database()
    
    def init_database(self):
        """ایجاد جداول دیتابیس بهینه"""
        try:
            with self.conn.cursor() as cursor:
                # جدول دسته‌ها
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS categories (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_by BIGINT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT categories_name_unique UNIQUE(name)
                    )
                ''')
                
                # جدول فایل‌ها
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS files (
                        id SERIAL PRIMARY KEY,
                        category_id TEXT NOT NULL,
                        file_id TEXT NOT NULL UNIQUE,
                        file_name TEXT NOT NULL,
                        file_size BIGINT NOT NULL,
                        file_type TEXT NOT NULL,
                        caption TEXT,
                        upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT files_file_id_unique UNIQUE (file_id),
                        CONSTRAINT fk_category FOREIGN KEY (category_id) 
                            REFERENCES categories (id) ON DELETE CASCADE
                    )
                ''')
                
                # جدول کانال‌های اجباری
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS channels (
                        id SERIAL PRIMARY KEY,
                        channel_id TEXT NOT NULL UNIQUE,
                        channel_name TEXT NOT NULL,
                        invite_link TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # ایجاد ایندکس‌ها برای بهبود کارایی
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_files_category ON files(category_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_files_type ON files(file_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_categories_created_by ON categories(created_by)')
                
                self.conn.commit()
                logger.info("Database initialized successfully with optimizations")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error initializing database: {e}")
            raise
    
    def _execute_with_retry(self, query: str, params: tuple = None, fetch: str = None):
        """اجرای کوئری با مکانیزم retry"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(query, params or ())
                    
                    if fetch == 'one':
                        result = cursor.fetchone()
                    elif fetch == 'all':
                        result = cursor.fetchall()
                    else:
                        result = None
                    
                    self.conn.commit()
                    return result
                    
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Database connection lost, retrying... ({attempt + 1}/{max_retries})")
                    time.sleep(1)
                    try:
                        self.conn = psycopg2.connect(os.getenv('DATABASE_URL'))
                    except:
                        pass
                else:
                    raise e
            except Exception as e:
                self.conn.rollback()
                raise e
    
    # ---------- مدیریت دسته‌ها ----------
    def add_category(self, category_id: str, name: str, created_by: int) -> bool:
        """اضافه کردن دسته جدید"""
        try:
            self._execute_with_retry('''
                INSERT INTO categories (id, name, created_by, created_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ''', (category_id, name, created_by))
            return True
        except Exception as e:
            logger.error(f"Error adding category: {e}")
            return False
    
    def get_categories(self) -> Dict[str, Dict]:
        """دریافت تمام دسته‌ها با کش"""
        try:
            # دریافت دسته‌ها
            categories_data = self._execute_with_retry('''
                SELECT id, name, created_by, created_at FROM categories
                ORDER BY created_at DESC
            ''', fetch='all')
            
            if not categories_data:
                return {}
            
            categories = {}
            category_ids = [cat[0] for cat in categories_data]
            
            # دریافت همه فایل‌ها در یک کوئری
            if category_ids:
                placeholders = ','.join(['%s'] * len(category_ids))
                files_data = self._execute_with_retry(f'''
                    SELECT category_id, file_id, file_name, file_size, file_type, caption 
                    FROM files 
                    WHERE category_id IN ({placeholders})
                    ORDER BY upload_date ASC
                ''', tuple(category_ids), fetch='all')
                
                # گروه‌بندی فایل‌ها بر اساس category_id
                files_by_category = {}
                for file_data in files_data or []:
                    cat_id = file_data[0]
                    if cat_id not in files_by_category:
                        files_by_category[cat_id] = []
                    files_by_category[cat_id].append({
                        'file_id': file_data[1],
                        'file_name': file_data[2],
                        'file_size': file_data[3],
                        'file_type': file_data[4],
                        'caption': file_data[5] or ''
                    })
            else:
                files_by_category = {}
            
            # ساخت نتیجه نهایی
            for cat_data in categories_data:
                cat_id, name, created_by, created_at = cat_data
                categories[cat_id] = {
                    'name': name,
                    'files': files_by_category.get(cat_id, []),
                    'created_by': int(created_by),
                    'created_at': str(created_at)
                }
            
            return categories
            
        except Exception as e:
            logger.error(f"Error retrieving categories: {e}")
            return {}
    
    def get_category(self, category_id: str) -> Optional[Dict]:
        """دریافت یک دسته خاص"""
        try:
            # دریافت اطلاعات دسته
            cat_data = self._execute_with_retry('''
                SELECT name, created_by, created_at FROM categories WHERE id = %s
            ''', (category_id,), fetch='one')
            
            if not cat_data:
                return None
            
            name, created_by, created_at = cat_data
            
            # دریافت فایل‌های دسته
            files_data = self._execute_with_retry('''
                SELECT file_id, file_name, file_size, file_type, caption 
                FROM files 
                WHERE category_id = %s 
                ORDER BY upload_date ASC
            ''', (category_id,), fetch='all')
            
            files = [
                {
                    'file_id': row[0],
                    'file_name': row[1],
                    'file_size': row[2],
                    'file_type': row[3],
                    'caption': row[4] or ''
                } for row in (files_data or [])
            ]
            
            return {
                'name': name,
                'files': files,
                'created_by': int(created_by),
                'created_at': str(created_at)
            }
            
        except Exception as e:
            logger.error(f"Error retrieving category: {e}")
            return None
    
    def delete_category(self, category_id: str) -> bool:
        """حذف دسته (CASCADE خودکار فایل‌ها را حذف می‌کند)"""
        try:
            self._execute_with_retry('''
                DELETE FROM categories WHERE id = %s
            ''', (category_id,))
            return True
        except Exception as e:
            logger.error(f"Error deleting category: {e}")
            return False

    # ---------- مدیریت فایل‌ها ----------
    def add_file_to_category(self, category_id: str, file_info: Dict) -> bool:
        """اضافه کردن فایل به دسته"""
        try:
            self._execute_with_retry('''
                INSERT INTO files (category_id, file_id, file_name, file_size, file_type, caption)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (
                category_id,
                file_info['file_id'],
                file_info['file_name'],
                file_info['file_size'],
                file_info['file_type'],
                file_info.get('caption', '')
            ))
            return True
        except psycopg2.IntegrityError:
            logger.warning(f"File {file_info['file_id']} already exists")
            return False
        except Exception as e:
            logger.error(f"Error adding file: {e}")
            return False
    
    def add_files_to_category(self, category_id: str, files: List[Dict]) -> bool:
        """اضافه کردن چندین فایل به دسته (Batch Insert)"""
        try:
            with self.conn.cursor() as cursor:
                # استفاده از executemany برای بهبود کارایی
                cursor.executemany('''
                    INSERT INTO files (category_id, file_id, file_name, file_size, file_type, caption)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT files_file_id_unique DO NOTHING
                ''', [
                    (
                        category_id,
                        file_info['file_id'],
                        file_info['file_name'],
                        file_info['file_size'],
                        file_info['file_type'],
                        file_info.get('caption', '')
                    ) for file_info in files
                ])
                self.conn.commit()
                return True
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error adding files: {e}")
            return False
    
    def delete_file(self, category_id: str, file_index: int) -> bool:
        """حذف فایل از دسته"""
        try:
            with self.conn.cursor() as cursor:
                # دریافت file_id بر اساس ایندکس
                cursor.execute('''
                    SELECT id FROM files 
                    WHERE category_id = %s 
                    ORDER BY upload_date ASC
                    LIMIT 1 OFFSET %s
                ''', (category_id, file_index))
                
                result = cursor.fetchone()
                if result:
                    cursor.execute('DELETE FROM files WHERE id = %s', (result[0],))
                    self.conn.commit()
                    return True
                return False
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error deleting file: {e}")
            return False

    # ---------- مدیریت کانال‌های اجباری ----------
    def add_channel(self, channel_id: str, channel_name: str, invite_link: str) -> bool:
        """اضافه کردن کانال اجباری"""
        try:
            self._execute_with_retry('''
                INSERT INTO channels (channel_id, channel_name, invite_link)
                VALUES (%s, %s, %s)
            ''', (channel_id, channel_name, invite_link))
            return True
        except psycopg2.IntegrityError:
            logger.warning(f"Channel {channel_id} already exists")
            return False
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            return False
    
    def get_channels(self) -> List[Dict]:
        """دریافت لیست کانال‌های اجباری"""
        try:
            channels_data = self._execute_with_retry('''
                SELECT channel_id, channel_name, invite_link 
                FROM channels 
                ORDER BY created_at ASC
            ''', fetch='all')
            
            return [
                {
                    'channel_id': row[0],
                    'channel_name': row[1],
                    'invite_link': row[2]
                } for row in (channels_data or [])
            ]
        except Exception as e:
            logger.error(f"Error retrieving channels: {e}")
            return []
    
    def delete_channel(self, channel_id: str) -> bool:
        """حذف کانال اجباری"""
        try:
            self._execute_with_retry('''
                DELETE FROM channels WHERE channel_id = %s
            ''', (channel_id,))
            return True
        except Exception as e:
            logger.error(f"Error deleting channel: {e}")
            return False

class FileManagerBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.pending_uploads: Dict[int, Dict] = {}
        self.pending_channel_data: Dict[int, Dict] = {}
    
    def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return user_id in ADMIN_IDS
    
    def generate_category_link(self, category_id: str) -> str:
        """تولید لینک برای دسته"""
        return f"https://t.me/{BOT_USERNAME}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> Optional[Dict]:
        """استخراج اطلاعات فایل از پیام"""
        message = update.message
        file_info = None
        
        # تشخیص نوع فایل و استخراج اطلاعات
        if message.document:
            doc = message.document
            file_info = {
                'file_id': doc.file_id,
                'file_name': doc.file_name or "document",
                'file_size': doc.file_size,
                'file_type': 'document',
                'caption': message.caption or ''
            }
        elif message.photo:
            photo = message.photo[-1]  # بزرگترین سایز
            file_info = {
                'file_id': photo.file_id,
                'file_name': "photo.jpg",
                'file_size': photo.file_size,
                'file_type': 'photo',
                'caption': message.caption or ''
            }
        elif message.video:
            video = message.video
            file_info = {
                'file_id': video.file_id,
                'file_name': video.file_name or "video.mp4",
                'file_size': video.file_size,
                'file_type': 'video',
                'caption': message.caption or ''
            }
        elif message.audio:
            audio = message.audio
            file_info = {
                'file_id': audio.file_id,
                'file_name': audio.file_name or "audio",
                'file_size': audio.file_size,
                'file_type': 'audio',
                'caption': message.caption or ''
            }
        elif message.voice:
            voice = message.voice
            file_info = {
                'file_id': voice.file_id,
                'file_name': "voice.ogg",
                'file_size': voice.file_size,
                'file_type': 'voice',
                'caption': message.caption or ''
            }
        
        return file_info

# ایجاد نمونه از کلاس
bot_manager = FileManagerBot()

# =========================================
# ========== HANDLERS PRINCIPALES =========
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کمند شروع"""
    user_id = update.effective_user.id
    
    # دسترسی از طریق لینک دسته
    if context.args and context.args[0].startswith('cat_'):
        category_id = context.args[0][4:]
        await handle_category_access(update, context, category_id)
        return
    
    # کاربر ادمین
    if bot_manager.is_admin(user_id):
        await update.message.reply_text(
            "👋 سلام ادمین عزیز!\n\n"
            "📚 دستورات در دسترس:\n"
            "/new_category - ایجاد دسته جدید\n"
            "/upload - شروع آپلود فایل\n"
            "/finish_upload - پایان آپلود\n"
            "/categories - لیست دسته‌ها\n"
            "/add_channel - افزودن کانال اجباری\n"
            "/remove_channel - حذف کانال اجباری\n"
            "/channels - لیست کانال‌های اجباری"
        )
    else:
        await update.message.reply_text(
            "👋 سلام!\n\n"
            "برای دریافت فایل‌ها از لینک‌های ارائه شده استفاده کنید."
        )

async def handle_category_access(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته با بررسی عضویت در کانال‌ها"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # ادمین‌ها نیاز به بررسی عضویت ندارند
    if bot_manager.is_admin(user_id):
        await handle_admin_category_access(update, context, category_id)
        return
    
    # دریافت کانال‌های اجباری
    channels = bot_manager.db.get_channels()
    if not channels:
        await send_category_files(update, context, category_id)
        return
    
    # بررسی عضویت کاربر در کانال‌ها
    non_joined_channels = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel['channel_id'],
                user_id=user_id
            )
            if member.status not in ['member', 'administrator', 'creator']:
                non_joined_channels.append(channel)
        except Exception as e:
            logger.error(f"Error checking membership: {e}")
            non_joined_channels.append(channel)
    
    # اگر کاربر در همه کانال‌ها عضو است
    if not non_joined_channels:
        await send_category_files(update, context, category_id)
        return
    
    # ایجاد صفحه عضویت در کانال‌ها
    keyboard = []
    for channel in non_joined_channels:
        keyboard.append([
            InlineKeyboardButton(
                text=f"📢 {channel['channel_name']}",
                url=channel['invite_link']
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(
            text="✅ عضو شدم",
            callback_data=f"check_membership_{category_id}"
        )
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ برای دسترسی به فایل‌ها، لطفاً در کانال‌های زیر عضو شوید:",
        reply_markup=reply_markup
    )

async def handle_admin_category_access(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی ادمین به دسته"""
    category = bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته مورد نظر یافت نشد!")
        return
    
    keyboard = [
        [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
        [InlineKeyboardButton("➕ اضافه کردن فایل", callback_data=f"add_{category_id}")],
        [InlineKeyboardButton("🗑 حذف فایل", callback_data=f"delete_file_{category_id}")],
        [InlineKeyboardButton("❌ حذف کل دسته", callback_data=f"delete_cat_{category_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"👨‍💼 شما ادمین هستید!\n\n"
        f"📂 دسته: {category['name']}\n"
        f"📦 تعداد فایل‌ها: {len(category['files'])}\n\n"
        f"لطفاً عملیات مورد نظر را انتخاب کنید:",
        reply_markup=reply_markup
    )

async def send_category_files(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته"""
    category = bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته مورد نظر یافت نشد!")
        return
    
    if not category['files']:
        await update.message.reply_text("📂 این دسته فایلی ندارد!")
        return
    
    await update.message.reply_text(f"📤 در حال ارسال فایل‌های دسته '{category['name']}'...")
    
    for file_info in category['files']:
        try:
            if file_info['file_type'] == 'document':
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            elif file_info['file_type'] == 'photo':
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            elif file_info['file_type'] == 'video':
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            elif file_info['file_type'] == 'audio':
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=file_info['file_id'],
                    caption=file_info.get('caption', '')
                 )
            elif file_info['file_type'] == 'voice':
                await context.bot.send_voice(
                    chat_id=update.effective_chat.id,
                    voice=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending file: {e}")

# =========================================
# ========== COMMANDES ADMIN ==============
# =========================================

async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد دسته جدید"""
    user_id = update.effective_user.id
    
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفاً نام دسته را مشخص کنید.\nمثال: /new_category نام_دسته")
        return
    
    category_name = ' '.join(context.args)
    category_id = str(uuid.uuid4())[:8]
    
    if bot_manager.db.add_category(category_id, category_name, user_id):
        link = bot_manager.generate_category_link(category_id)
        await update.message.reply_text(
            f"✅ دسته '{category_name}' با موفقیت ایجاد شد!\n\n"
            f"🔗 لینک دسته:\n{link}\n\n"
            f"برای شروع آپلود فایل‌ها از دستور زیر استفاده کنید:\n"
            f"/upload {category_id}"
        )
    else:
        await update.message.reply_text("❌ خطا در ایجاد دسته!")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع آپلود فایل‌ها برای دسته"""
    user_id = update.effective_user.id
    
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفاً آیدی دسته را مشخص کنید.\nمثال: /upload category_id")
        return
    
    category_id = context.args[0]
    category = bot_manager.db.get_category(category_id)
    
    if not category:
        await update.message.reply_text("❌ دسته مورد نظر یافت نشد!")
        return
    
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فایل برای دسته '{category['name']}' فعال شد!\n\n"
        f"فایل‌های خود را ارسال کنید.\n"
        f"برای پایان آپلود، از دستور /finish_upload استفاده کنید."
    )
    return UPLOADING

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دریافت انواع فایل‌ها"""
    user_id = update.effective_user.id
    
    if user_id not in bot_manager.pending_uploads:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload_info = bot_manager.pending_uploads[user_id]
    upload_info['files'].append(file_info)
    
    await update.message.reply_text(
        f"✅ فایل '{file_info['file_name']} دریافت شد!\n"
        f"تعداد فایل‌های دریافت شده: {len(upload_info['files'])}"
    )

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    
    if user_id not in bot_manager.pending_uploads:
        await update.message.reply_text("❌ هیچ آپلودی در حال انجام نیست!")
        return ConversationHandler.END
    
    upload_info = bot_manager.pending_uploads.pop(user_id)
    
    if not upload_info['files']:
        await update.message.reply_text("❌ هیچ فایلی آپلود نشده است!")
        return ConversationHandler.END
    
    success = bot_manager.db.add_files_to_category(
        upload_info['category_id'], 
        upload_info['files']
    )
    
    if success:
        link = bot_manager.generate_category_link(upload_info['category_id'])
        await update.message.reply_text(
            f"✅ {len(upload_info['files'])} فایل با موفقیت اضافه شد!\n\n"
            f"🔗 لینک دسته:\n{link}"
        )
    else:
        await update.message.reply_text("❌ خطا در ذخیره فایل‌ها!")
    
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    categories = bot_manager.db.get_categories()
    
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cat_id, cat_info in categories.items():
        link = bot_manager.generate_category_link(cat_id)
        message += f"• {cat_info['name']}\n"
        message += f"  فایل‌ها: {len(cat_info['files'])}\n"
        message += f"  لینک: {link}\n\n"
    
    await update.message.reply_text(message)

# =========================================
# ===== GESTION DES CANAUX OBLIGATOIRES ===
# =========================================

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند افزودن کانال اجباری"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    await update.message.reply_text(
        "لطفاً اطلاعات کانال را به ترتیب زیر ارسال کنید:\n\n"
        "1. آیدی عددی کانال\n"
        "2. نام کانال\n"
        "3. لینک دعوت به کانال\n\n"
        "مثال:\n"
        "-1001234567890\n"
        "کانال نمونه\n"
        "https://t.me/example"
    )
    return WAITING_FOR_CHANNEL_INFO

async def handle_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش اطلاعات کانال"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # ذخیره اطلاعات موقت
    if user_id not in bot_manager.pending_channel_data:
        bot_manager.pending_channel_data[user_id] = {
            'channel_id': '',
            'channel_name': '',
            'invite_link': ''
        }
    
    channel_data = bot_manager.pending_channel_data[user_id]
    
    # دریافت اطلاعات کانال
    if not channel_data['channel_id']:
        channel_data['channel_id'] = text
        await update.message.reply_text("✅ آیدی کانال دریافت شد!\nلطفاً نام کانال را ارسال کنید:")
        return WAITING_FOR_CHANNEL_INFO
    
    if not channel_data['channel_name']:
        channel_data['channel_name'] = text
        await update.message.reply_text("✅ نام کانال دریافت شد!\nلطفاً لینک دعوت را ارسال کنید:")
        return WAITING_FOR_CHANNEL_INFO
    
    if not channel_data['invite_link']:
        channel_data['invite_link'] = text
        
        # ذخیره کانال در دیتابیس
        success = bot_manager.db.add_channel(
            channel_data['channel_id'],
            channel_data['channel_name'],
            channel_data['invite_link']
        )
        
        del bot_manager.pending_channel_data[user_id]
        
        if success:
            await update.message.reply_text("✅ کانال با موفقیت افزوده شد!")
        else:
            await update.message.reply_text("❌ خطا در افزودن کانال!")
        
        return ConversationHandler.END
    
    return ConversationHandler.END

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف کانال اجباری"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفاً آیدی کانال را مشخص کنید.\nمثال: /remove_channel -1001234567890")
        return
    
    channel_id = context.args[0]
    if bot_manager.db.delete_channel(channel_id):
        await update.message.reply_text("✅ کانال با موفقیت حذف شد!")
    else:
        await update.message.reply_text("❌ خطا در حذف کانال یا کانال یافت نشد!")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لیست کانال‌های اجباری"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما مجاز به انجام این عمل نیستید!")
        return
    
    channels = bot_manager.db.get_channels()
    if not channels:
        await update.message.reply_text("📢 هیچ کانال اجباری ثبت نشده است!")
        return
    
    message = "📢 لیست کانال‌های اجباری:\n\n"
    for i, channel in enumerate(channels, 1):
        message += (
            f"{i}. {channel['channel_name']}\n"
            f"   آیدی: {channel['channel_id']}\n"
            f"   لینک: {channel['invite_link']}\n\n"
        )
    
    await update.message.reply_text(message)

# =========================================
# ========== GESTION DES BOUTONS ==========
# =========================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های اینلاین"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith('check_membership_'):
        category_id = data[17:]
        await handle_category_access(query, context, category_id)
    
    elif bot_manager.is_admin(user_id):
        if data.startswith('view_'):
            category_id = data[5:]
            await view_category_files(query, context, category_id)
        
        elif data.startswith('add_'):
            category_id = data[4:]
            await start_adding_files(query, category_id, user_id)
        
        elif data.startswith('delete_file_'):
            category_id = data[12:]
            await show_files_for_deletion(query, category_id)
        
        elif data.startswith('delete_cat_'):
            category_id = data[11:]
            await confirm_category_deletion(query, category_id)
        
        elif data.startswith('confirm_del_cat_'):
            category_id = data[16:]
            await delete_category(query, category_id)
        
        elif data.startswith('del_file_'):
            parts = data[9:].split('_', 1)
            category_id, file_index = parts[0], int(parts[1])
            await delete_file_from_category(query, category_id, file_index)
    
    else:
        await query.edit_message_text("❌ شما مجاز به انجام این عمل نیستید!")

async def view_category_files(query, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """نمایش فایل‌های دسته برای ادمین"""
    category = bot_manager.db.get_category(category_id)
    if not category:
        await query.edit_message_text("❌ دسته یافت نشد!")
        return
    
    if not category['files']:
        await query.edit_message_text("📂 این دسته فایلی ندارد!")
        return
    
    await query.edit_message_text("📤 در حال ارسال فایل‌ها...")
    
    for file_info in category['files']:
        try:
            if file_info['file_type'] == 'document':
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            elif file_info['file_type'] == 'photo':
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=file_info['file_id'],
                    caption=file_info.get('caption', '')
                )
            # ... سایر انواع فایل‌ها به همین ترتیب
            
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending file: {e}")

async def start_adding_files(query, category_id: str, user_id: int):
    """شروع اضافه کردن فایل‌ها"""
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    await query.edit_message_text(
        "📤 حالت اضافه کردن فایل فعال شد!\n\n"
        "فایل‌های جدید خود را ارسال کنید.\n"
        "برای پایان، از /finish_upload استفاده کنید."
    )

async def show_files_for_deletion(query, category_id: str):
    """نمایش فایل‌ها برای حذف"""
    category = bot_manager.db.get_category(category_id)
    if not category or not category['files']:
        await query.edit_message_text("📂 این دسته فایلی ندارد!")
        return
    
    keyboard = []
    for i, file_info in enumerate(category['files']):
        keyboard.append([InlineKeyboardButton(
            f"🗑 {file_info['file_name']}", 
            callback_data=f"del_file_{category_id}_{i}"
        )])
    
    await query.edit_message_text(
        "کدام فایل را می‌خواهید حذف کنید؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_file_from_category(query, category_id: str, file_index: int):
    """حذف فایل از دسته"""
    success = bot_manager.db.delete_file(category_id, file_index)
    if success:
        await query.edit_message_text("✅ فایل با موفقیت حذف شد!")
    else:
        await query.edit_message_text("❌ خطا در حذف فایل!")

async def confirm_category_deletion(query, category_id: str):
    """تأیید حذف دسته"""
    keyboard = [
        [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"confirm_del_cat_{category_id}")],
        [InlineKeyboardButton("❌ انصراف", callback_data="cancel")]
    ]
    await query.edit_message_text(
        "⚠️ آیا مطمئن هستید که می‌خواهید این دسته را حذف کنید؟\n"
        "این عمل قابل بازگشت نیست!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_category(query, category_id: str):
    """حذف دسته"""
    category = bot_manager.db.get_category(category_id)
    if not category:
        await query.edit_message_text("❌ دسته یافت نشد!")
        return
    
    if bot_manager.db.delete_category(category_id):
        await query.edit_message_text(f"✅ دسته '{category['name']}' با موفقیت حذف شد!")
    else:
        await query.edit_message_text("❌ خطا در حذف دسته!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری"""
    user_id = update.effective_user.id
    if user_id in bot_manager.pending_uploads:
        del bot_manager.pending_uploads[user_id]
    if user_id in bot_manager.pending_channel_data:
        del bot_manager.pending_channel_data[user_id]
    
    await update.message.reply_text("❌ عملیات لغو شد.")
    return ConversationHandler.END

# =========================================
# ============ LANCEMENT DU BOT ===========
# =========================================

web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Bot is running!", 200

def run_web_server():
    web_app.run(host='0.0.0.0', port=10000)

def main():
    """اجرای ربات"""
    import threading
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # دستورات اصلی
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_category", new_category))
    application.add_handler(CommandHandler("categories", categories_list))
    application.add_handler(CommandHandler("remove_channel", remove_channel))
    application.add_handler(CommandHandler("channels", list_channels))
    
    # گفتگو برای آپلود فایل‌ها
    upload_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOADING: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO | 
                    filters.AUDIO | filters.VOICE,
                    handle_media
                )
            ]
        },
        fallbacks=[
            CommandHandler("finish_upload", finish_upload),
            CommandHandler("cancel", cancel)
        ]
    )
    application.add_handler(upload_conv_handler)
    
    # گفتگو برای افزودن کانال
    channel_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add_channel", add_channel)],
        states={
            WAITING_FOR_CHANNEL_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_channel_info)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(channel_conv_handler)
    
    # سایر هندلرها
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("finish_upload", finish_upload))
    
    # اجرای ربات
    logger.info("ربات در حال اجرا...")
    application.run_polling()

if __name__ == '__main__':
    main()
