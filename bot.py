# -*- coding: utf-8 -*-
import os, time, asyncio, logging
from typing import Dict, List, Any, Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from analysis import analyze_symbol
from scanner import scan_for_auto_signals, get_top_signals, scan_market_overview
try:
    from signal_tracker import add_signal_to_tracking, check_active_signals, format_active_signals, format_signal_stats, reset_signal_stats, parse_days_from_text, get_symbol_stats_report
except Exception:
    add_signal_to_tracking=None; check_active_signals=None; format_active_signals=None; format_signal_stats=None; reset_signal_stats=None; parse_days_from_text=lambda t:7; get_symbol_stats_report=None
try:
    from paper_trader import open_paper_position, format_paper_stats, format_open_positions, reset_paper_trades
except Exception:
    open_paper_position=None; format_paper_stats=None; format_open_positions=None; reset_paper_trades=None
try:
    from ai_memory import format_ai_status
    from coin_learning import format_learning_summary, format_coin_behavior, format_smart_stats
    from coin_rotation import format_rotation_report
    from ghost_signals import format_ghost_report
    from slot_manager import format_slot_report
except Exception:
    format_ai_status=None; format_learning_summary=None; format_coin_behavior=None; format_smart_stats=None; format_rotation_report=None; format_ghost_report=None; format_slot_report=None
try:
    from config import BOT_TOKEN, OWNER_ID, ALLOWED_USER_IDS, AUTO_SIGNAL_ENABLED, AUTO_SCAN_INTERVAL_MINUTES, AUTO_DIRECT_SCORE_MIN, AUTO_SIGNAL_COOLDOWN_MINUTES
except Exception:
    BOT_TOKEN=os.getenv('BOT_TOKEN',''); OWNER_ID=int(os.getenv('OWNER_ID','0') or 0); ALLOWED_USER_IDS=[]; AUTO_SIGNAL_ENABLED=True; AUTO_SCAN_INTERVAL_MINUTES=5; AUTO_DIRECT_SCORE_MIN=82; AUTO_SIGNAL_COOLDOWN_MINUTES=30
if OWNER_ID and OWNER_ID not in ALLOWED_USER_IDS: ALLOWED_USER_IDS.append(OWNER_ID)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger=logging.getLogger('crypto-ai-bot')
LAST_AUTO_SIGNAL_TIME={}; AUTO_SIGNAL_COOLDOWN_SECONDS=int(AUTO_SIGNAL_COOLDOWN_MINUTES)*60

def get_user_id(update):
    try: return int(update.effective_user.id)
    except Exception: return 0

def is_allowed(update):
    uid=get_user_id(update)
    if not OWNER_ID: return True
    return uid==OWNER_ID or uid in ALLOWED_USER_IDS
async def reject_unauthorized(update):
    if update.message: await update.message.reply_text('⛔️ شما اجازه استفاده از این ربات را ندارید.')

PERSIAN_SYMBOLS={'بیتکوین':'BTCUSDT','بیت کوین':'BTCUSDT','btc':'BTCUSDT','اتریوم':'ETHUSDT','اتر':'ETHUSDT','eth':'ETHUSDT','سولانا':'SOLUSDT','سول':'SOLUSDT','sol':'SOLUSDT','دوج':'DOGEUSDT','دوج کوین':'DOGEUSDT','doge':'DOGEUSDT','ریپل':'XRPUSDT','xrp':'XRPUSDT','کاردانو':'ADAUSDT','ada':'ADAUSDT','آواکس':'AVAXUSDT','avax':'AVAXUSDT','بایننس':'BNBUSDT','bnb':'BNBUSDT','تون':'TONUSDT','ton':'TONUSDT','لینک':'LINKUSDT','link':'LINKUSDT','اپتوس':'APTUSDT','apt':'APTUSDT','آربیتروم':'ARBUSDT','arb':'ARBUSDT','پالیگان':'POLUSDT','متیک':'POLUSDT','matic':'POLUSDT','شیبا':'SHIBUSDT','pepe':'PEPEUSDT','پپه':'PEPEUSDT'}
def normalize_symbol_text(text):
    t=str(text or '').strip().lower(); cleaned=t.replace('تحلیل','').replace('سیگنال','').replace('بررسی','').replace('خرید','').replace('فروش','').replace('/','').replace('-','').strip()
    for k,s in PERSIAN_SYMBOLS.items():
        if k in cleaned: return s
    raw=cleaned.upper().replace(' ','')
    if raw.endswith('USDT') and len(raw)>=6: return raw
    if raw.isalpha() and 2<=len(raw)<=8: return raw+'USDT'
    return None

def fa_direction(d): return 'لانگ' if d=='LONG' else 'شورت' if d=='SHORT' else 'بدون سیگنال'
def format_signal_message(r):
    if r.get('status')!='ACTIVE': return format_manual_analysis(r)
    return f"🚨 سیگنال خودکار\nنماد: {r.get('symbol')}\nجهت: {fa_direction(r.get('direction'))}\nوضعیت: ✅ ورود فعال\n\nورود: {r.get('entry')}\nحد ضرر: {r.get('stop_loss')}\nحد سود ۱: {r.get('tp1')}\nحد سود ۲: {r.get('tp2')}\n\nامتیاز: {r.get('score')}\nریسک: {r.get('risk_level')}\nR/R: {r.get('risk_reward')}\nاعتبار: {r.get('validity','15 تا 45 دقیقه')}"
def format_manual_analysis(r):
    if r.get('status')!='ACTIVE':
        reasons='\n'.join([f"• {x}" for x in r.get('reasons',[])[:8]]) or 'شرایط ورود کامل نیست.'
        return f"📊 تحلیل {r.get('symbol')}\n\nوضعیت: ❌ بدون سیگنال معتبر\nامتیاز: {r.get('score',0)}\nلانگ: {r.get('long_score',0)} | شورت: {r.get('short_score',0)}\nRSI: {r.get('rsi')}\nADX: {r.get('adx')}\nVWAP: {r.get('vwap_status')}\n\nدلایل:\n{reasons}"
    return f"📊 تحلیل {r.get('symbol')}\n\nوضعیت: ✅ سیگنال فعال\nجهت: {fa_direction(r.get('direction'))}\n\nورود: {r.get('entry')}\nحد ضرر: {r.get('stop_loss')}\nحد سود ۱: {r.get('tp1')}\nحد سود ۲: {r.get('tp2')}\n\nامتیاز: {r.get('score')}\nلانگ: {r.get('long_score')} | شورت: {r.get('short_score')}\nریسک: {r.get('risk_level')}\nR/R: {r.get('risk_reward')}\n\nRSI: {r.get('rsi')}\nADX: {r.get('adx')}\nVWAP: {r.get('vwap_status')}\nروند بازار: {r.get('market_regime')}\n\nاعتبار: {r.get('validity','15 تا 45 دقیقه')}"
def format_top_signals(signals):
    if not signals: return 'فعلاً سیگنال مناسبی پیدا نشد.'
    lines=['🏆 بهترین سیگنال‌های فعلی:']
    for i,s in enumerate(signals,1): lines.append(f"\n{i}) {s.get('symbol')} | {fa_direction(s.get('direction'))}\nامتیاز: {s.get('score')} | ریسک: {s.get('risk_level')}\nورود: {s.get('entry')}\nSL: {s.get('stop_loss')} | TP1: {s.get('tp1')}")
    return '\n'.join(lines)
def format_market_overview_text(r): return f"📌 بررسی کلی بازار\n\n{r.get('summary')}\n\nصعودی: {r.get('bullish_pct')}٪\nنزولی: {r.get('bearish_pct')}٪\nرنج/نامشخص: {r.get('neutral_pct')}٪\nتعداد بررسی‌شده: {r.get('scanned')}"
def attach_signal_metadata(signal,message_id,chat_id,source='auto_signal'):
    s=dict(signal); s['telegram_message_id']=message_id; s['message_id']=message_id; s['chat_id']=chat_id; s['user_id']=chat_id; s['source']=source; return s

async def start_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    await update.message.reply_text('🤖 Crypto AI Bot\n\nدستورات:\nتحلیل بیتکوین\nسیگنال سولانا\nبهترین سیگنال\nبررسی بازار\nآمار\nآمار ارزها\nسیگنال‌های فعال\nپوزیشن‌ها\nهوش مصنوعی\nریست آمار')
async def help_command(update, context): await start_command(update, context)
async def analyze_request(update, context, symbol):
    w=await update.message.reply_text('⏳ در حال تحلیل...')
    try: await w.edit_text(format_manual_analysis(analyze_symbol(symbol)))
    except Exception as e: await w.edit_text(f'❌ خطا در تحلیل:\n{str(e)[:200]}')
async def best_signal_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    w=await update.message.reply_text('⏳ در حال بررسی بازار...')
    try: await w.edit_text(format_top_signals(get_top_signals(limit=5)))
    except Exception as e: await w.edit_text(f'❌ خطا:\n{str(e)[:200]}')
async def market_overview_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    w=await update.message.reply_text('⏳ در حال بررسی بازار...')
    try: await w.edit_text(format_market_overview_text(scan_market_overview()))
    except Exception as e: await w.edit_text(f'❌ خطا:\n{str(e)[:200]}')
async def stats_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    days=parse_days_from_text(update.message.text if update.message else '')
    parts=[]
    if format_signal_stats:
        try: parts.append(format_signal_stats(days))
        except Exception: pass
    if format_paper_stats:
        try: parts.append(format_paper_stats())
        except Exception: pass
    await update.message.reply_text('\n\n'.join(parts) if parts else 'آماری موجود نیست.')
async def symbol_stats_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    if get_symbol_stats_report: await update.message.reply_text(get_symbol_stats_report(parse_days_from_text(update.message.text)))
async def positions_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    await update.message.reply_text(format_open_positions() if format_open_positions else 'ماژول Paper فعال نیست.')
async def active_signals_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    await update.message.reply_text(format_active_signals() if format_active_signals else 'ماژول Tracker فعال نیست.')
async def ai_status_command(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    parts=[]
    for fn in [format_ai_status,format_learning_summary,format_rotation_report,format_ghost_report,format_slot_report]:
        if fn:
            try: parts.append(fn())
            except Exception: pass
    await update.message.reply_text('\n\n'.join(parts) if parts else 'AI Status در دسترس نیست.')
async def reset_stats_command(update, context):
    if get_user_id(update)!=OWNER_ID: await reject_unauthorized(update); return
    if reset_signal_stats: reset_signal_stats()
    if reset_paper_trades: reset_paper_trades()
    await update.message.reply_text('✅ آمارها ریست شدند.')
async def register_sent_signal(signal, sent_message, source='auto_signal'):
    try:
        meta=attach_signal_metadata(signal,sent_message.message_id,sent_message.chat_id,source)
        if add_signal_to_tracking:
            try: add_signal_to_tracking(meta)
            except Exception: pass
        if open_paper_position:
            try: open_paper_position(meta, telegram_message_id=sent_message.message_id, chat_id=sent_message.chat_id)
            except Exception: pass
    except Exception as e: logger.error(f'register_sent_signal error: {e}')
def auto_signal_key(s): return f"{s.get('symbol')}_{s.get('direction')}"
def can_send_auto_signal(s):
    if s.get('status')!='ACTIVE' or not s.get('entry_confirmed') or int(s.get('score') or 0)<int(AUTO_DIRECT_SCORE_MIN): return False
    key=auto_signal_key(s); now=int(time.time()); last=int(LAST_AUTO_SIGNAL_TIME.get(key,0)); return now-last>=AUTO_SIGNAL_COOLDOWN_SECONDS
def mark_auto_signal_sent(s): LAST_AUTO_SIGNAL_TIME[auto_signal_key(s)]=int(time.time())
async def auto_signal_loop(app):
    if not AUTO_SIGNAL_ENABLED or not OWNER_ID: return
    await asyncio.sleep(10)
    while True:
        try:
            res=scan_for_auto_signals(max_results=3, allow_ghost=True)
            for sig in res.get('signals',[]):
                if not can_send_auto_signal(sig): continue
                sent=await app.bot.send_message(chat_id=OWNER_ID, text=format_signal_message(sig)); await register_sent_signal(sig,sent,'auto_signal'); mark_auto_signal_sent(sig); await asyncio.sleep(1)
        except Exception as e: logger.error(f'auto_signal_loop error: {e}')
        await asyncio.sleep(max(60,int(AUTO_SCAN_INTERVAL_MINUTES)*60))
async def signal_tracking_loop(app):
    if not check_active_signals: return
    await asyncio.sleep(15)
    while True:
        try:
            events=check_active_signals() or []
            if isinstance(events,dict): events=[events]
            for ev in events:
                text=ev.get('message') or ev.get('text'); chat_id=ev.get('chat_id') or OWNER_ID
                if not text: continue
                try: await app.bot.send_message(chat_id=chat_id,text=text,reply_to_message_id=ev.get('reply_to_message_id'))
                except Exception: await app.bot.send_message(chat_id=chat_id,text=text)
                await asyncio.sleep(1)
        except Exception as e: logger.error(f'signal_tracking_loop error: {e}')
        await asyncio.sleep(20)
async def handle_text(update, context):
    if not is_allowed(update): await reject_unauthorized(update); return
    text=update.message.text.strip(); low=text.lower()
    if low in ['بهترین سیگنال','بهترین','top','best']: await best_signal_command(update,context); return
    if low in ['بررسی بازار','بازار','وضعیت بازار']: await market_overview_command(update,context); return
    if low.startswith('آمار ارز') or low.startswith('امار ارز'): await symbol_stats_command(update,context); return
    if low.startswith('آمار') or low.startswith('امار') or low=='stats': await stats_command(update,context); return
    if low in ['پوزیشن‌ها','پوزیشن ها','positions']: await positions_command(update,context); return
    if low in ['سیگنال‌های فعال','سیگنال های فعال','active signals']: await active_signals_command(update,context); return
    if low in ['هوش مصنوعی','ai','وضعیت ai']: await ai_status_command(update,context); return
    if low in ['ریست آمار','reset stats']: await reset_stats_command(update,context); return
    sym=normalize_symbol_text(text)
    if sym: await analyze_request(update,context,sym); return
    await update.message.reply_text('متوجه نشدم. مثلا بنویس:\nتحلیل بیتکوین\nبهترین سیگنال\nبررسی بازار')
async def error_handler(update, context): logger.error('Telegram error', exc_info=context.error)
async def post_init(app):
    asyncio.create_task(auto_signal_loop(app)); asyncio.create_task(signal_tracking_loop(app))
def build_application():
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN is not set')
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start_command)); app.add_handler(CommandHandler('help',help_command)); app.add_handler(CommandHandler('stats',stats_command)); app.add_handler(CommandHandler('positions',positions_command)); app.add_handler(CommandHandler('active',active_signals_command)); app.add_handler(CommandHandler('ai',ai_status_command)); app.add_handler(CommandHandler('resetstats',reset_stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)); app.add_error_handler(error_handler); return app
def main():
    app=build_application(); logger.info('Crypto AI Bot started'); app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
if __name__=='__main__': main()
