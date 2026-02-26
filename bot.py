import asyncio
import re
import urllib.request
import json
import os
import logging
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. PostgreSQL is required.")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PreCheckoutQueryHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Exchange rate cache
_usd_rate = {"rate": 90.0, "date": ""}

def get_usd_rate() -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    if _usd_rate["date"] == today:
        return _usd_rate["rate"]

    sources = [
        ("CBR", "https://www.cbr-xml-daily.ru/daily_json.js",
         lambda d: d["Valute"]["USD"]["Value"]),
        ("ER-API", "https://open.er-api.com/v6/latest/RUB",
         lambda d: 1 / d["rates"]["USD"]),
        ("Frankfurter", "https://api.frankfurter.app/latest?from=USD&to=RUB",
         lambda d: d["rates"]["RUB"]),
    ]

    for name, url, extractor in sources:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as r:
                data = json.loads(r.read())
            rate = float(extractor(data))
            _usd_rate["rate"] = rate
            _usd_rate["date"] = today
            logger.info(f"USD rate from {name}: {rate}")
            return rate
        except Exception as e:
            logger.warning(f"{name} failed: {e}")

    logger.warning("All rate sources failed, using fallback 90")
    return 90.0

BOT_TOKEN = os.getenv("BOT_TOKEN", "8649933614:AAFtSLs2sAyPzKiErNhmpIZeaP93XeKpX5I")
ADMIN_ID = 587349420
STARS_PRICE = 99  # Telegram Stars for 30 days

CPM_BY_NICHE = {
    "crypto": 500,
    "finance": 450,
    "business": 400,
    "marketing": 350,
    "default": 300
}

CRYPTO_KEYWORDS = ["крипт", "bitcoin", "btc", "eth", "invest", "трейд", "binance", "биржа"]
FINANCE_KEYWORDS = ["финанс", "деньги", "заработ", "доход", "акци"]
MARKETING_KEYWORDS = ["маркетинг", "smm", "реклам", "таргет", "арбитраж"]

FREE_CHECKS_PER_DAY = 3

# --- Database ---

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id BIGINT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_checks (
            user_id BIGINT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_cache (
            username TEXT PRIMARY KEY,
            members BIGINT,
            avg_views FLOAT,
            er FLOAT,
            niche TEXT,
            fair_price INT,
            posts_per_day FLOAT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gift_codes (
            code TEXT PRIMARY KEY,
            days INT NOT NULL DEFAULT 30,
            used BOOLEAN NOT NULL DEFAULT FALSE,
            used_by BIGINT,
            created_at TIMESTAMP DEFAULT NOW(),
            used_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("DB initialized: PostgreSQL")
    # Auto-grant subscription to admin if expired/missing
    if not is_premium(ADMIN_ID):
        add_subscription(ADMIN_ID, days=3650)
        logger.info(f"Admin subscription auto-granted to {ADMIN_ID}")

def is_premium(user_id: int) -> bool:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id = %s AND expires_at > NOW()", (user_id,))
        count = cur.fetchone()[0]
        conn.close()
        return count > 0
    except Exception as e:
        logger.error(f"is_premium error for {user_id}: {e}")
        return False

def add_subscription(user_id: int, days: int = 30):
    conn = get_conn()
    cur = conn.cursor()
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    cur.execute(
        "INSERT INTO subscriptions (user_id, expires_at) VALUES (%s, %s) ON CONFLICT(user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
        (user_id, expires)
    )
    conn.commit()
    conn.close()

def get_expiry(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT expires_at FROM subscriptions WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return str(row[0])[:10]  # YYYY-MM-DD

CACHE_NOTIFY_MILESTONES = [100, 250, 500, 1000]

def save_channel_cache(username: str, members: int, avg_views: float, er: float,
                        niche: str, fair_price: int, posts_per_day: float):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO channel_cache (username, members, avg_views, er, niche, fair_price, posts_per_day, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT(username) DO UPDATE SET
                members = EXCLUDED.members,
                avg_views = EXCLUDED.avg_views,
                er = EXCLUDED.er,
                niche = EXCLUDED.niche,
                fair_price = EXCLUDED.fair_price,
                posts_per_day = EXCLUDED.posts_per_day,
                updated_at = NOW()
        """, (username, members, avg_views, er, niche, fair_price, posts_per_day))
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM channel_cache")
        total = cur.fetchone()[0]
        conn.close()
        if total in CACHE_NOTIFY_MILESTONES:
            asyncio.create_task(_notify_admin_cache(total))
    except Exception as e:
        logger.error(f"save_channel_cache error: {e}")

async def _notify_admin_cache(total: int):
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        if total == 100:
            text = f"🎯 База каналов достигла {total}! Можно добавлять кнопки с топом по категориям."
        else:
            text = f"📊 База каналов: уже {total} каналов накоплено."
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        logger.error(f"_notify_admin_cache error: {e}")

# --- Gift codes ---

def generate_gift_code(days: int = 30) -> str:
    import secrets, string
    alphabet = string.ascii_uppercase + string.digits
    code = "GIFT-" + "".join(secrets.choice(alphabet) for _ in range(8))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO gift_codes (code, days) VALUES (%s, %s)", (code, days))
    conn.commit()
    conn.close()
    return code

def redeem_gift_code(code: str, user_id: int) -> tuple[bool, str]:
    """Returns (success, message)"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT days, used, used_by FROM gift_codes WHERE code = %s", (code,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "❌ Код не найден."
    days, used, used_by = row
    if used:
        conn.close()
        return False, "❌ Этот код уже был использован."
    cur.execute(
        "UPDATE gift_codes SET used = TRUE, used_by = %s, used_at = NOW() WHERE code = %s",
        (user_id, code)
    )
    conn.commit()
    conn.close()
    add_subscription(user_id, days=days)
    expiry = get_expiry(user_id)
    return True, f"🎁 Подарочная подписка активирована!\n⚡ Безлимитный доступ до *{expiry}*"

# --- Helpers ---

def extract_username(text: str):
    text = text.strip()
    if 't.me/' in text:
        part = text.split('t.me/')[-1].split('/')[0].split('?')[0].strip()
        return part if part else None
    if text.startswith('@'):
        # Берём только первое слово (до пробела/переноса строки)
        first_word = re.split(r'\s', text[1:])[0]
        return first_word.split('/')[0].split('?')[0].strip() or None
    if re.match(r'^[a-zA-Z0-9_]+$', text) and len(text) >= 4:
        return text
    return None

def detect_niche(description: str) -> str:
    desc = (description or "").lower()
    for kw in CRYPTO_KEYWORDS:
        if kw in desc: return "crypto"
    for kw in FINANCE_KEYWORDS:
        if kw in desc: return "finance"
    for kw in MARKETING_KEYWORDS:
        if kw in desc: return "marketing"
    return "default"

def get_channel_info(username: str, token: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/getChat?chat_id=@{username}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        raise ValueError("Канал не найден или закрытый")
    chat = data["result"]
    if chat.get("type") not in ("channel", "supergroup"):
        raise ValueError("Это не канал — только публичные каналы поддерживаются")
    url2 = f"https://api.telegram.org/bot{token}/getChatMemberCount?chat_id=@{username}"
    with urllib.request.urlopen(urllib.request.Request(url2), timeout=10) as r2:
        count_data = json.loads(r2.read())
    return {
        "title": chat.get("title", username),
        "description": chat.get("description", ""),
        "username": username,
        "members": count_data.get("result", 0)
    }

def get_post_views(username: str) -> tuple:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }
    url = f"https://t.me/s/{username}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
        content = r.read().decode()
    views_raw = re.findall(r'tgme_widget_message_views[^>]*>([^<]+)<', content)
    views = []
    for v in views_raw:
        v = v.strip().replace('\xa0', '').replace(' ', '')
        try:
            if 'K' in v: views.append(float(v.replace('K', '')) * 1000)
            elif 'M' in v: views.append(float(v.replace('M', '')) * 1_000_000)
            else: views.append(float(v))
        except: pass
    dates = re.findall(r'datetime="([^"]+)"', content)
    return views, dates

def calculate_fair_price(avg_views: float, niche: str) -> tuple:
    cpm = CPM_BY_NICHE.get(niche, CPM_BY_NICHE["default"])
    return int(avg_views * cpm / 1000), cpm

def check_daily_limit(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT count FROM daily_checks WHERE user_id = %s AND date = %s", (user_id, today))
    row = cur.fetchone()
    count = row[0] if row else 0
    if count >= FREE_CHECKS_PER_DAY:
        conn.close()
        return False
    cur.execute(
        "INSERT INTO daily_checks (user_id, date, count) VALUES (%s,%s,1) "
        "ON CONFLICT(user_id, date) DO UPDATE SET count = daily_checks.count + 1",
        (user_id, today)
    )
    conn.commit()
    conn.close()
    return True

def get_er_status(er: float) -> str:
    if er >= 20: return "🟢 Отличный"
    if er >= 10: return "🟡 Хороший"
    if er >= 5:  return "🟠 Средний"
    return "🔴 Низкий (возможна накрутка)"

def fmt_num(n: float) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1000: return f"{n/1000:.0f}K"
    return str(int(n))

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    premium = is_premium(user_id)
    expiry = get_expiry(user_id) if premium else None
    premium_line = f"✅ Подписка активна до {expiry}" if premium else f"🆓 Бесплатно: {FREE_CHECKS_PER_DAY} проверок/день\n⚡ Безлимит: {STARS_PRICE} ⭐ Stars / 30 дней"

    text = (
        "👋 Привет! Я анализирую Telegram-каналы и показываю *справедливую цену рекламы*.\n\n"
        "📊 Отправь @username канала — и я скажу:\n"
        "• Реальный охват постов\n"
        "• ER (вовлечённость аудитории)\n"
        "• Справедливую цену рекламного поста\n"
        "• Есть ли признаки накрутки\n\n"
        f"{premium_line}\n\n"
        "Попробуй: отправь @durov или любой другой канал"
    )
    keyboard = [
        [InlineKeyboardButton("🔍 Начать анализ", switch_inline_query_current_chat="@")],
        [InlineKeyboardButton("📊 Мой статус", callback_data="status")],
    ]
    if not premium:
        keyboard.append([InlineKeyboardButton(f"⚡ Купить безлимит — {STARS_PRICE} ⭐", callback_data="buy")])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    premium = is_premium(user_id)
    expiry = get_expiry(user_id) if premium else None
    if premium:
        text = f"✅ *Подписка активна*\nДействует до: *{expiry}*\n⚡ Безлимитные проверки включены"
        keyboard = []
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT count FROM daily_checks WHERE user_id = %s AND date = %s", (user_id, today))
        row = cur.fetchone()
        conn.close()
        used = row[0] if row else 0
        remaining = max(0, FREE_CHECKS_PER_DAY - used)
        text = (
            f"📊 *Ваш статус*\n\n"
            f"🆓 Бесплатный план\n"
            f"• Проверок сегодня осталось: {remaining}/{FREE_CHECKS_PER_DAY}\n\n"
            f"⚡ Безлимит — всего {STARS_PRICE} ⭐ Stars / 30 дней"
        )
        keyboard = [[InlineKeyboardButton(f"⚡ Купить безлимит — {STARS_PRICE} ⭐", callback_data="buy")]]
    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != 587349420:
        return
    user_id = update.effective_user.id
    lines = []
    try:
        lines.append("DB: PostgreSQL")
        lines.append(f"DATABASE_URL set: True")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM subscriptions")
        lines.append(f"Total subs: {cur.fetchone()[0]}")
        cur.execute("SELECT user_id, expires_at FROM subscriptions WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        lines.append(f"My sub: {row}")
        conn.close()
        lines.append(f"is_premium: {is_premium(user_id)}")
    except Exception as e:
        lines.append(f"ERROR: {e}")
    await update.message.reply_text("\n".join(lines))

async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /grant <user_id> — grant premium subscription"""
    if update.effective_user.id != 587349420:
        return
    args = context.args
    target_id = int(args[0]) if args else update.effective_user.id
    add_subscription(target_id, days=30)
    expiry = get_expiry(target_id)
    await update.message.reply_text(f"✅ Подписка выдана пользователю {target_id} до {expiry}")

async def giftcode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: /giftcode [days] — generate a gift code"""
    if update.effective_user.id != ADMIN_ID:
        return
    days = int(context.args[0]) if context.args else 30
    code = generate_gift_code(days=days)
    await update.message.reply_text(
        f"🎁 Подарочный код на {days} дней:\n\n`{code}`\n\nОтправь его пользователю — он вводит прямо в бота.",
        parse_mode="Markdown"
    )

async def analyze_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""

    # Check if message is a gift code
    stripped = text.strip().upper()
    if stripped.startswith("GIFT-"):
        user_id = update.effective_user.id
        success, msg = redeem_gift_code(stripped, user_id)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    username = None
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type in ("url", "text_link"):
                url = entity.url or text[entity.offset:entity.offset+entity.length]
                username = extract_username(url)
                if username: break
    if not username:
        username = extract_username(text)
    if not username:
        return

    user_id = update.effective_user.id
    logger.info(f"User {user_id} checking @{username}")

    if not check_daily_limit(user_id):
        keyboard = [[InlineKeyboardButton(f"⚡ Купить безлимит — {STARS_PRICE} ⭐", callback_data="buy")]]
        await update.message.reply_text(
            f"⚠️ Бесплатный лимит исчерпан ({FREE_CHECKS_PER_DAY}/день).\n"
            "Купи безлимитный доступ за Telegram Stars!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    msg = await update.message.reply_text(f"🔍 Анализирую @{username}...")

    try:
        info = get_channel_info(username, BOT_TOKEN)
        views, dates = get_post_views(username)

        if not views:
            await msg.edit_text("❌ Не удалось получить данные. Канал может быть закрытым или без публичных постов.")
            return

        members = info["members"]
        avg_views = sum(views) / len(views)
        er = (avg_views / members * 100) if members > 0 else 0
        niche = detect_niche(info["description"])
        fair_price, cpm = calculate_fair_price(avg_views, niche)
        er_status = get_er_status(er)

        freq_text = ""
        posts_per_day = 0.0
        if len(dates) >= 2:
            try:
                d1 = datetime.fromisoformat(dates[0].replace('Z', '+00:00'))
                d2 = datetime.fromisoformat(dates[-1].replace('Z', '+00:00'))
                span_days = abs((d1 - d2).days) or 1
                posts_per_day = len(dates) / span_days
                freq_text = f"\n📅 Частота: ~{posts_per_day:.1f} постов/день"
            except: pass

        usd_rate = get_usd_rate()
        fair_price_usd = int(fair_price / usd_rate)

        result = (
            f"📊 *@{username}*\n"
            f"━━━━━━━━━━━━━━\n"
            f"👥 Подписчики: {fmt_num(members)}\n"
            f"👁 Средний охват: {fmt_num(avg_views)} ({len(views)} постов)\n"
            f"📈 ER: {er:.1f}% — {er_status}"
            f"{freq_text}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 *Справедливая цена поста:*\n"
            f"   ~{fair_price:,} ₽ (~${fair_price_usd:,})\n"
            f"━━━━━━━━━━━━━━\n"
        )
        if er < 5:
            result += "⚠️ *Внимание:* низкий ER — возможна накрутка\n"

        # Save to channel cache for future top-by-category feature
        save_channel_cache(username, members, avg_views, er, niche, fair_price, posts_per_day)

        keyboard = []
        if not is_premium(user_id):
            keyboard.append([InlineKeyboardButton(f"⚡ Безлимит — {STARS_PRICE} ⭐", callback_data="buy")])

        await msg.edit_text(result, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"Error analyzing @{username}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка при анализе: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "status":
        user_id = query.from_user.id
        premium = is_premium(user_id)
        expiry = get_expiry(user_id) if premium else None
        if premium:
            text = f"✅ *Подписка активна*\nДействует до: *{expiry}*\n⚡ Безлимитные проверки включены"
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT count FROM daily_checks WHERE user_id = %s AND date = %s", (user_id, today))
            row = cur.fetchone()
            conn.close()
            used = row[0] if row else 0
            remaining = max(0, FREE_CHECKS_PER_DAY - used)
            text = f"📊 *Ваш статус*\n\n🆓 Бесплатный план\nПроверок сегодня осталось: {remaining}/{FREE_CHECKS_PER_DAY}\n\n⚡ Безлимит — всего {STARS_PRICE} ⭐ / 30 дней"
        await query.message.reply_text(text, parse_mode="Markdown")
    elif query.data == "buy":
        user_id = query.from_user.id
        await context.bot.send_invoice(
            chat_id=user_id,
            title="⚡ Безлимитный доступ — 30 дней",
            description="Безлимитные проверки каналов + кнопка мониторинга на 30 дней",
            payload=f"premium_{user_id}",
            provider_token="",  # Empty string for Telegram Stars
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice("30 дней безлимита", STARS_PRICE)],
        )
    elif query.data.startswith("monitor_"):
        channel = query.data.split("_", 1)[1]
        user_id = query.from_user.id
        if is_premium(user_id):
            await query.message.reply_text(f"🔔 Мониторинг @{channel} — скоро будет!")
        else:
            keyboard = [[InlineKeyboardButton(f"⚡ Купить безлимит — {STARS_PRICE} ⭐", callback_data="buy")]]
            await query.message.reply_text(
                "🔒 Мониторинг доступен в платной версии.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_subscription(user_id, days=30)
    expiry = get_expiry(user_id)
    user = update.effective_user
    username = f"@{user.username}" if user.username else f"id{user_id}"
    await update.message.reply_text(
        f"✅ *Оплата прошла! Спасибо!*\n\n"
        f"⚡ Безлимитный доступ активирован до *{expiry}*\n"
        f"Теперь проверяй сколько угодно каналов!",
        parse_mode="Markdown"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💰 Новый платный подписчик!\n\n"
                 f"👤 {user.full_name} ({username})\n"
                 f"🆔 {user_id}\n"
                 f"📅 Подписка до: {expiry}\n"
                 f"⭐ Оплата: {STARS_PRICE} Stars"
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("giftcode", giftcode_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_channel))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
