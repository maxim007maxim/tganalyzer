import asyncio
import re
import urllib.request
import json
import os
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8649933614:AAFtSLs2sAyPzKiErNhmpIZeaP93XeKpX5I")

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

FREE_CHECKS_PER_DAY = 10  # увеличил для тестирования
user_checks = {}

def extract_username(text: str):
    text = text.strip()
    # Handle t.me links
    if 't.me/' in text:
        part = text.split('t.me/')[-1].split('/')[0].split('?')[0].strip()
        return part if part else None
    # Handle @username
    if text.startswith('@'):
        return text[1:].split('/')[0].split('?')[0].strip() or None
    # Plain username (at least 4 chars, no spaces)
    if ' ' not in text and len(text) >= 4 and re.match(r'^[a-zA-Z0-9_]+$', text):
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
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
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
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
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
    today = datetime.now().strftime("%Y-%m-%d")
    if user_id not in user_checks or user_checks[user_id]["date"] != today:
        user_checks[user_id] = {"date": today, "count": 0}
    if user_checks[user_id]["count"] >= FREE_CHECKS_PER_DAY:
        return False
    user_checks[user_id]["count"] += 1
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я анализирую Telegram-каналы и показываю *справедливую цену рекламы*.\n\n"
        "📊 Отправь @username канала — и я скажу:\n"
        "• Реальный охват постов\n"
        "• ER (вовлечённость аудитории)\n"
        "• Справедливую цену рекламного поста\n"
        "• Есть ли признаки накрутки\n\n"
        f"🆓 Бесплатно: {FREE_CHECKS_PER_DAY} проверок в день\n"
        "⚡ Безлимит + мониторинг: 299₽/мес\n\n"
        "Попробуй: отправь @durov или любой другой канал 🦊"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def analyze_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    
    # Try to extract from entities (links)
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
        return  # Not a channel mention, ignore silently

    user_id = update.effective_user.id
    logger.info(f"User {user_id} checking @{username}")

    if not check_daily_limit(user_id):
        keyboard = [[InlineKeyboardButton("⚡ Купить безлимит — 299₽/мес", callback_data="buy")]]
        await update.message.reply_text(
            f"⚠️ Бесплатный лимит исчерпан ({FREE_CHECKS_PER_DAY}/день).\n"
            "Купи безлимитный доступ!",
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
        if len(dates) >= 2:
            try:
                d1 = datetime.fromisoformat(dates[0].replace('Z', '+00:00'))
                d2 = datetime.fromisoformat(dates[-1].replace('Z', '+00:00'))
                days = abs((d1 - d2).days) or 1
                freq_text = f"\n📅 Частота: ~{len(dates)/days:.1f} постов/день"
            except: pass

        result = (
            f"📊 *@{username}*\n"
            f"━━━━━━━━━━━━━━\n"
            f"👥 Подписчики: {fmt_num(members)}\n"
            f"👁 Средний охват: {fmt_num(avg_views)} ({len(views)} постов)\n"
            f"📈 ER: {er:.1f}% — {er_status}"
            f"{freq_text}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 *Справедливая цена поста:*\n"
            f"   ~{fair_price:,} ₽\n"
            f"   (CPM {cpm}₽ × {fmt_num(avg_views)} охват)\n"
            f"━━━━━━━━━━━━━━\n"
        )
        if er < 5:
            result += "⚠️ *Внимание:* низкий ER — возможна накрутка\n"

        keyboard = [[InlineKeyboardButton("🔔 Мониторить канал", callback_data=f"monitor_{username}")]]
        await msg.edit_text(result, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"Error analyzing @{username}: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка при анализе: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "buy":
        await query.message.reply_text("💳 Оплата скоро будет доступна!")
    elif query.data.startswith("monitor_"):
        channel = query.data.split("_", 1)[1]
        await query.message.reply_text(f"🔔 Мониторинг @{channel} — в платной версии!")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_channel))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
