# -*- coding: utf-8 -*-
import logging
import os
import re
import json
import asyncio
import time
import aiohttp
import uuid
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse, urlencode
import iop
from concurrent.futures import ThreadPoolExecutor
from aliexpress_utils import get_product_details_by_id 

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue 
from telegram.constants import ParseMode, ChatAction

# --- Initialize Logger ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALIEXPRESS_APP_KEY = os.getenv('ALIEXPRESS_APP_KEY')
ALIEXPRESS_APP_SECRET = os.getenv('ALIEXPRESS_APP_SECRET')
ALIEXPRESS_TRACKING_ID = os.getenv('ALIEXPRESS_TRACKING_ID', 'default_tracking_id')  # Updated default
TARGET_CURRENCY = os.getenv('TARGET_CURRENCY', 'USD')
TARGET_LANGUAGE = os.getenv('TARGET_LANGUAGE', 'ar')
QUERY_COUNTRY = os.getenv('QUERY_COUNTRY', 'US')

# --- AliExpress API Configuration ---
ALIEXPRESS_API_URL = 'https://api-sg.aliexpress.com/sync'
QUERY_FIELDS = 'product_main_image_url,target_sale_price,product_title,target_sale_price_currency'

# --- Thread Pool ---
executor = ThreadPoolExecutor(max_workers=10)

# --- Cache Configuration ---
CACHE_EXPIRY_DAYS = 1
CACHE_EXPIRY_SECONDS = CACHE_EXPIRY_DAYS * 24 * 60 * 60

# --- Regex Patterns (Precompiled) ---
URL_REGEX = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+|\b(?:s\.click\.|a\.)?aliexpress\.(?:com|ru|es|fr|pt|it|pl|nl|co\.kr|co\.jp|com\.br|com\.tr|com\.vn|us|id|th|ar)(?:\.[\w-]+)?/[^\s<>"]*', re.IGNORECASE)
PRODUCT_ID_REGEX = re.compile(r'/item/(\d+)\.html')
STANDARD_ALIEXPRESS_DOMAIN_REGEX = re.compile(r'https?://(?!a\.|s\.click\.)([\w-]+\.)?aliexpress\.(com|ru|es|fr|pt|it|pl|nl|co\.kr|co\.jp|com\.br|com\.tr|com\.vn|us|id\.aliexpress\.com|th\.aliexpress\.com|ar\.aliexpress\.com)(\.([\w-]+))?(/.*)?', re.IGNORECASE)
SHORT_LINK_DOMAIN_REGEX = re.compile(r'https?://(?:s\.click\.aliexpress\.com/e/|a\.aliexpress\.com/_)[a-zA-Z0-9_-]+/?', re.IGNORECASE)

# --- Offer Parameters (Updated with Coin Page) ---
OFFER_PARAMS = {
    "coin": {"name": "🪙 تخفيض العملات", "params": {"sourceType": "620%26channel=coin", "afSmartRedirect": "y"}},
    "coin_page": {"name": "💰 التخفيض بالعملات فالصفحة", "params": {"sourceType": "coin_page", "afSmartRedirect": "y"}},
    "super": {"name": "🔥 السوبر ديلز", "params": {"sourceType": "562", "channel": "sd", "afSmartRedirect": "y"}},
    "limited": {"name": "⏳ العرض المحدود", "params": {"sourceType": "561", "channel": "limitedoffers", "afSmartRedirect": "y"}},
    "bigsave": {"name": "💰 التخفيض الكبير", "params": {"sourceType": "680", "channel": "bigSave", "afSmartRedirect": "y"}},
}
OFFER_ORDER = ["coin", "coin_page", "super", "limited", "bigsave"]  # Updated order

# --- Cache Implementation ---
class CacheWithExpiry:
    def __init__(self, expiry_seconds):
        self.cache = {}
        self.expiry_seconds = expiry_seconds
        self._lock = asyncio.Lock()

    async def get(self, key):
        async with self._lock:
            if key in self.cache:
                item, timestamp = self.cache[key]
                if time.time() - timestamp < self.expiry_seconds:
                    return item
                else:
                    del self.cache[key]
            return None

    async def set(self, key, value):
        async with self._lock:
            self.cache[key] = (value, time.time())

    async def clear_expired(self):
        async with self._lock:
            current_time = time.time()
            expired_keys = [k for k, (_, t) in self.cache.items() if current_time - t >= self.expiry_seconds]
            for key in expired_keys:
                del self.cache[key]
            return len(expired_keys)

# Initialize caches
product_cache = CacheWithExpiry(CACHE_EXPIRY_SECONDS)
link_cache = CacheWithExpiry(CACHE_EXPIRY_SECONDS)
resolved_url_cache = CacheWithExpiry(CACHE_EXPIRY_SECONDS)

# --- Helper Functions ---
def generate_random_string(length=5):
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=length))

def extract_product_id(url):
    """Extracts product ID from URL (handles multiple patterns)"""
    patterns = [
        r'/item/(\d+)\.html',  # Standard pattern
        r'/p/[^/]+/(\d+)\.html',  # Alternative pattern
        r'product/(\d+)'  # Another variant
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def clean_aliexpress_url(url: str, product_id: str) -> str | None:
    """Reconstructs clean base URL"""
    try:
        parsed_url = urlparse(url)
        path_segment = f'/item/{product_id}.html'
        return urlunparse((
            parsed_url.scheme or 'https',
            parsed_url.netloc,
            path_segment,
            '', '', ''
        ))
    except ValueError:
        return None

def build_url_with_offer_params(base_url, params_to_add):
    """Builds URLs with offer parameters (updated for coin_page)"""
    if not params_to_add:
        return base_url

    try:
        # Special handling for coin page offer
        if params_to_add.get('sourceType') == 'coin_page':
            product_id = extract_product_id(base_url)
            if not product_id:
                return None
                
            timestamp = int(time.time() * 1000)
            random_str = generate_random_string()
            
            return (
                "https://m.aliexpress.com/p/coin-index/index.html"
                "?_immersiveMode=true"
                "&from=syicon"
                f"&productIds={product_id}"
                f"&aff_fcid={uuid.uuid4().hex}"
                f"&aff_fsk={ALIEXPRESS_TRACKING_ID}"
                "&aff_platform=api-new-link-generate"
                f"&sk={ALIEXPRESS_TRACKING_ID}"
                f"&aff_trace_key={uuid.uuid4().hex}-{timestamp}-{random_str}"
                f"&terminal_id={uuid.uuid4().hex}"
            )
            
        # Original handling for other offers
        parsed_url = urlparse(base_url)
        netloc = parsed_url.netloc
        if '.' in netloc and netloc.count('.') > 1:
            parts = netloc.split('.')
            if len(parts) >= 2 and 'aliexpress' in parts[-2]:
                netloc = f"aliexpress.{parts[-1]}"
        
        if 'sourceType' in params_to_add and '%26' in params_to_add['sourceType']:
            new_query_string = '&'.join([f"{k}={v}" for k, v in params_to_add.items() 
                                      if k != 'channel' and '%26channel=' in params_to_add['sourceType']])
        else:
            new_query_string = urlencode(params_to_add)
            
        reconstructed_url = urlunparse((
            parsed_url.scheme,
            netloc,
            parsed_url.path,
            '',
            new_query_string,
            ''
        ))
        return f"https://star.aliexpress.com/share/share.htm?&redirectUrl={reconstructed_url}"
    except ValueError:
        return base_url

async def resolve_short_link(short_url: str, session: aiohttp.ClientSession) -> str | None:
    """Resolves short links to final URL"""
    cached = await resolved_url_cache.get(short_url)
    if cached:
        return cached

    try:
        async with session.get(short_url, allow_redirects=True, timeout=10) as response:
            final_url = str(response.url)
            if '.aliexpress.us' in final_url:
                final_url = final_url.replace('.aliexpress.us', '.aliexpress.com')
            await resolved_url_cache.set(short_url, final_url)
            return final_url
    except Exception as e:
        logger.error(f"Error resolving short link: {e}")
        return None

# --- API Functions ---
async def fetch_product_details_v2(product_id):
    """Fetches product details with cache"""
    cached = await product_cache.get(product_id)
    if cached:
        return cached

    def _execute_api_call():
        try:
            request = iop.IopRequest('aliexpress.affiliate.productdetail.get')
            request.add_api_param('fields', QUERY_FIELDS)
            request.add_api_param('product_ids', product_id)
            request.add_api_param('target_currency', TARGET_CURRENCY)
            request.add_api_param('target_language', TARGET_LANGUAGE)
            request.add_api_param('tracking_id', ALIEXPRESS_TRACKING_ID)
            request.add_api_param('country', QUERY_COUNTRY)
            return aliexpress_client.execute(request)
        except Exception as e:
            logger.error(f"API call error: {e}")
            return None

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(executor, _execute_api_call)

    if not response or not response.body:
        return None

    try:
        response_data = json.loads(response.body) if isinstance(response.body, str) else response.body
        product_data = response_data.get('aliexpress_affiliate_productdetail_get_response', {}).get('resp_result', {}).get('result', {}).get('products', {}).get('product', [{}])[0]
        
        product_info = {
            'image_url': product_data.get('product_main_image_url'),
            'price': product_data.get('target_sale_price'),
            'currency': product_data.get('target_sale_price_currency', TARGET_CURRENCY),
            'title': product_data.get('product_title', f'Product {product_id}')
        }
        await product_cache.set(product_id, product_info)
        return product_info
    except Exception as e:
        logger.error(f"Error parsing product details: {e}")
        return None

async def generate_affiliate_links_batch(target_urls: list[str]) -> dict[str, str | None]:
    """Generates affiliate links in batch"""
    results = {}
    uncached_urls = []

    for url in target_urls:
        cached = await link_cache.get(url)
        if cached:
            results[url] = cached
        else:
            results[url] = None
            uncached_urls.append(url)

    if not uncached_urls:
        return results

    def _execute_batch_call():
        try:
            request = iop.IopRequest('aliexpress.affiliate.link.generate')
            request.add_api_param('promotion_link_type', '0')
            request.add_api_param('source_values', ','.join(uncached_urls))
            request.add_api_param('tracking_id', ALIEXPRESS_TRACKING_ID)
            return aliexpress_client.execute(request)
        except Exception as e:
            logger.error(f"Batch API error: {e}")
            return None

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(executor, _execute_batch_call)

    if not response or not response.body:
        return results

    try:
        response_data = json.loads(response.body) if isinstance(response.body, str) else response.body
        links = response_data.get('aliexpress_affiliate_link_generate_response', {}).get('resp_result', {}).get('result', {}).get('promotion_links', {}).get('promotion_link', [])
        
        for link in links:
            if isinstance(link, dict):
                source_url = link.get('source_value')
                promo_link = link.get('promotion_link')
                if source_url and promo_link:
                    results[source_url] = promo_link
                    await link_cache.set(source_url, promo_link)
    except Exception as e:
        logger.error(f"Error parsing batch links: {e}")

    return results

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command handler"""
    await update.message.reply_html(
        "👋 مرحبا بك في بوت تخفيض العملات! 🛍️\n\n"
        "🔍 <b>كيفية استعمال البوت ؟</b>\n"
        "1️⃣ قم بعمل نسخ لرابط منتج ما من ألي اكسبرس 📋\n"
        "2️⃣ أرسل رابط المنتج الى البوت 📤\n"
        "3️⃣ البوت سيقوم اوتوماتيكا بعمل رابط جديد يحتوي على تخفيض اكبر  ✨\n"
        "🔗 <b>أنواع الروابط المدعومة</b>\n"
        "• روابط الي اكسبرس العادية 🌐\n"
    )

async def process_product_telegram(product_id: str, base_url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes product and sends Telegram message"""
    chat_id = update.effective_chat.id
    loading_msg = await context.bot.send_message(chat_id, "⏳ جاري معالجة المنتج...")

    try:
        # Fetch product details
        product_details = await fetch_product_details_v2(product_id)
        if not product_details:
            product_details = {
                'title': f"Product {product_id}",
                'price': None,
                'currency': '',
                'image_url': None
            }

        # Generate all offer URLs
        target_urls = {}
        for offer_key in OFFER_ORDER:
            params = OFFER_PARAMS[offer_key]["params"]
            target_url = build_url_with_offer_params(base_url, params)
            if target_url:
                target_urls[offer_key] = target_url

        # Generate affiliate links
        links = await generate_affiliate_links_batch(list(target_urls.values()))

        # Build message
        message_lines = [
            f"<b>{product_details['title'][:250]}</b>",
            f"\n💵 <b>السعر:</b> {product_details['price']} {product_details['currency']}" if product_details['price'] else "",
            "\n🎁 <b>العروض المتاحة:</b>"
        ]

        for offer_key in OFFER_ORDER:
            if offer_key in target_urls:
                link = links.get(target_urls[offer_key])
                if link:
                    message_lines.append(f"{OFFER_PARAMS[offer_key]['name']}: {link}")

        # Add footer
        message_lines.append("\n🛒 <i>اضغط على أي رابط لفتح العرض</i>")

        # Send message
        if product_details['image_url']:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=product_details['image_url'],
                caption="\n".join(message_lines),
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(message_lines),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

    except Exception as e:
        logger.error(f"Error processing product: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ حدث خطأ أثناء معالجة المنتج. يرجى المحاولة لاحقًا."
        )
    finally:
        await context.bot.delete_message(chat_id, loading_msg.message_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming messages"""
    if not update.message or not update.message.text:
        return

    message_text = update.message.text
    chat_id = update.effective_chat.id
    user = update.effective_user

    potential_urls = extract_potential_aliexpress_urls(message_text)
    if not potential_urls:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ لم يتم العثور على روابط AliExpress صالحة في رسالتك."
        )
        return

    processed_ids = set()
    async with aiohttp.ClientSession() as session:
        for url in potential_urls:
            if not url.startswith(('http://', 'https://')):
                url = f"https://{url}"

            # Resolve short links
            if SHORT_LINK_DOMAIN_REGEX.match(url):
                url = await resolve_short_link(url, session) or url

            product_id = extract_product_id(url)
            if not product_id or product_id in processed_ids:
                continue

            base_url = clean_aliexpress_url(url, product_id)
            if not base_url:
                continue

            processed_ids.add(product_id)
            await process_product_telegram(product_id, base_url, update, context)

# --- Main Application ---
def main() -> None:
    """Start the bot"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (
            filters.Regex(r'aliexpress\.com|s\.click\.aliexpress\.com|a\.aliexpress\.com')
        ),
        handle_message
    ))

    # Start the bot
    logger.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    # Initialize AliExpress client
    try:
        aliexpress_client = iop.IopClient(ALIEXPRESS_API_URL, ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET)
        main()
    except Exception as e:
        logger.error(f"Failed to initialize AliExpress client: {e}")
