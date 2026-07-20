import os
import time
import uuid
import random
import string
import logging
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from threading import Thread

from flask import Flask, request, jsonify
from flask_cors import CORS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, 
    MessageHandler, Filters, ConversationHandler, CallbackContext
)

# ======================
# CONFIGURATION
# ======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# DALAWANG DATABASE URLS
DB_INJECTOR = os.environ.get("DATABASE_URL_INJECTOR") or os.environ.get("DATABASE_URL")
DB_SCRIPT = os.environ.get("DATABASE_URL_SCRIPT") or os.environ.get("DATABASE_URL")

TOKEN_EXPIRY = 20
COOLDOWN = 120
KEY_LIMIT = 120

db_cache = {
    "tokens": {},
    "ip_limit": {},
    "cooldowns": {}
}

# ======================
# FLASK APP SETUP
# ======================
app = Flask(__name__)
CORS(app)

def get_db_connection(target_db="injector"):
    """Pumipili ng database base sa request target ('injector' o 'script')"""
    db_url = DB_SCRIPT if str(target_db).lower() == "script" else DB_INJECTOR
    if not db_url:
        raise ValueError(f"Database URL for {target_db} is missing!")
    return psycopg2.connect(db_url)

def cleanup():
    now = time.time()
    for t in list(db_cache["tokens"].keys()):
        if now - db_cache["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db_cache["tokens"][t]
    for ip in list(db_cache["ip_limit"].keys()):
        if now - db_cache["ip_limit"][ip] > KEY_LIMIT:
            del db_cache["ip_limit"][ip]

def send_telegram_alert(message: str):
    if not BOT_TOKEN or not OWNER_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass

def format_remaining_time(seconds: int) -> str:
    if seconds <= 0:
        return "Expired"
    if seconds >= 900000000:
        return "Lifetime"
        
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    
    parts = []
    if days > 0: parts.append(f"{int(days)}d")
    if hours > 0: parts.append(f"{int(hours)}h")
    if minutes > 0: parts.append(f"{int(minutes)}m")
    
    if not parts: return "Less than 1m"
    return " ".join(parts)

def convert_duration(duration: str):
    duration = str(duration).lower()
    if duration.endswith("m"): return int(duration[:-1]) * 60
    if duration.endswith("h"): return int(duration[:-1]) * 3600
    if duration.endswith("d"): return int(duration[:-1]) * 86400
    if duration == "lifetime": return 999999999
    return 1800

# ======================
# API ENDPOINTS
# ======================
@app.route("/")
def home():
    return "KAZE CENTRAL SERVER ONLINE"

@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    source = request.args.get("src", "site")

    if source != "bot":
        if ip in db_cache["cooldowns"]:
            elapsed = now - db_cache["cooldowns"][ip]
            if elapsed < COOLDOWN:
                return jsonify({
                    "status": "cooldown",
                    "redirect": "https://kazefreekeysite.onrender.com"
                })

    token_id = str(uuid.uuid4())
    db_cache["tokens"][token_id] = {"ip": ip, "time": now}

    return jsonify({
        "status": "success",
        "token": token_id
    })

@app.route("/getkey")
def getkey():
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1")
    target_db = request.args.get("target", "injector") # 'injector' o 'script'
    now = time.time()

    if not token_id or token_id not in db_cache["tokens"]:
        return jsonify({"status": "error", "message": "Token expired"}), 403

    token_data = db_cache["tokens"][token_id]
    ip = token_data["ip"]

    if ip in db_cache["ip_limit"]:
        wait = int(KEY_LIMIT - (now - db_cache["ip_limit"][ip]))
        if wait > 0:
            return jsonify({"status": "wait", "message": "Bypass detected!"}), 403

    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"
    key = prefix + ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(target_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
        """, (key, now + expiry_seconds, int(max_dev)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500

    db_cache["ip_limit"][ip] = now
    del db_cache["tokens"][token_id]

    return jsonify({
        "status": "success",
        "key": key,
        "expires_in": expiry_seconds,
        "max_devices": max_dev
    })

@app.route("/customkey")
def custom_key():
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1")
    target_db = request.args.get("target", "injector")
    now = time.time()

    if not custom_name:
        return jsonify({"status": "error", "message": "Custom key name is missing"}), 400

    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(target_db)
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"status": "error", "message": "Key name already exists!"}), 409

        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
        """, (key, now + expiry_seconds, int(max_dev)))
        conn.commit()
        cur.close(); conn.close()
        
        send_telegram_alert(f"🎁 *Custom Key Created ({target_db.upper()})*\nKey: `{key}`\nDuration: `{duration}`\nMax Devices: `{max_dev}`")
        return jsonify({"status": "success", "key": key, "expires_in": expiry_seconds, "max_devices": max_dev})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/verify")
def verify():
    cleanup()
    key = request.args.get("key")
    device = request.args.get("device")
    target_db = request.args.get("target", "injector")
    if not key or not device: return "invalid"

    try:
        conn = get_db_connection(target_db)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM keys WHERE key_code = %s;", (key,))
        data = cur.fetchone()

        if not data:
            cur.close(); conn.close()
            return "invalid"

        if data["revoked"]:
            cur.close(); conn.close()
            send_telegram_alert(f"❌ *Key Revoked Attempt*\nKey: `{key}`\nDevice: `{device}`")
            return "revoked"

        if time.time() > data["expiry"]:
            cur.close(); conn.close()
            send_telegram_alert(f"❌ *Key Expired Attempt*\nKey: `{key}`\nDevice: `{device}`")
            return "expired"

        current_devices = data["device"].split(",") if data["device"] else []
        max_allowed = data.get("max_devices", 1)
        remaining_seconds = int(data["expiry"] - time.time())
        time_left_str = format_remaining_time(remaining_seconds)

        if device in current_devices:
            cur.close(); conn.close()
            device_index = current_devices.index(device) + 1
            counter_str = f" ({device_index}/{max_allowed})" if max_allowed > 1 else ""
            send_telegram_alert(f"✓ *Key Used{counter_str}*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{time_left_str}`")
            return "valid"

        if len(current_devices) < max_allowed:
            current_devices.append(device)
            new_device_string = ",".join(current_devices)
            
            cur.execute("UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;", (new_device_string, time.time(), key))
            conn.commit()
            cur.close(); conn.close()
            
            counter_str = f" ({len(current_devices)}/{max_allowed})" if max_allowed > 1 else ""
            send_telegram_alert(f"✓ *Key Used{counter_str}*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{time_left_str}`")
            return "valid"

        cur.close(); conn.close()
        send_telegram_alert(f"🔒 *Max Device Limit Reached*\nKey: `{key}`\nAttempt Device: `{device}`\nSlots: `{len(current_devices)}/{max_allowed}`")
        return "locked"
    except Exception:
        return "error", 500

@app.route("/revoke")
def revoke():
    key = request.args.get("key")
    target_db = request.args.get("target", "injector")
    if not key: return jsonify({"status": "error"}), 400
    
    conn = get_db_connection(target_db)
    cur = conn.cursor()
    cur.execute("UPDATE keys SET revoked = TRUE WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close(); conn.close()
    
    if count == 0: return jsonify({"status": "error"}), 404
    send_telegram_alert(f"🚫 *Key Revoked ({target_db.upper()})*\nKey: `{key}`")
    return jsonify({"status": "success"})

@app.route("/unrevoke")
def unrevoke_key():
    key = request.args.get("key")
    target_db = request.args.get("target", "injector")
    if not key: 
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(target_db)
        cur = conn.cursor()
        cur.execute("UPDATE keys SET revoked = FALSE WHERE key_code = %s;", (key,))
        conn.commit()
        count = cur.rowcount
        cur.close(); conn.close()
        
        if count == 0: 
            return jsonify({"status": "error", "message": "Key not found"}), 404
            
        send_telegram_alert(f"🟢 *Key Successfully Unrevoked ({target_db.upper()})*\nKey: `{key}`")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/reset")
def reset_device():
    key = request.args.get("key")
    target_db = request.args.get("target", "injector")
    if not key: return jsonify({"status": "error"}), 400
    
    conn = get_db_connection(target_db)
    cur = conn.cursor()
    cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close(); conn.close()
    
    if count == 0: return jsonify({"status": "error"}), 404
    send_telegram_alert(f"🔄 *Key Device Reset ({target_db.upper()})*\nKey: `{key}`")
    return jsonify({"status": "success"})

@app.route("/list")
def list_keys():
    try:
        status_filter = request.args.get("status", "active")
        target_db = request.args.get("target", "injector")
        now = time.time()
        
        conn = get_db_connection(target_db)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if status_filter == "revoked":
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = TRUE ORDER BY expiry DESC;")
        else:
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = FALSE AND expiry > %s ORDER BY expiry ASC;", (now,))
            
        rows = cur.fetchall()
        cur.close(); conn.close()

        result = []
        for r in rows:
            result.append({
                "key": r.get("key_code") or "UNKNOWN",
                "device": r.get("device"),
                "max_devices": r.get("max_devices") or 1
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Backend list failure: {str(e)}"}), 500

@app.route("/delete")
def delete_key():
    key = request.args.get("key")
    target_db = request.args.get("target", "injector")
    if not key: 
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(target_db)
        cur = conn.cursor()
        cur.execute("DELETE FROM keys WHERE key_code = %s;", (key,))
        conn.commit()
        count = cur.rowcount
        cur.close(); conn.close()
        if count == 0: 
            return jsonify({"status": "error", "message": "Key not found"}), 404
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stats")
def stats():
    try:
        target_db = request.args.get("target", "injector")
        now = time.time()
        conn = get_db_connection(target_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM keys;")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
        active = cur.fetchone()[0]
        cur.close(); conn.close()
        
        return jsonify({"total_keys": total, "active_keys": active, "expired_keys": total - active})
    except Exception as e:
        return jsonify({"total_keys": 0, "active_keys": 0, "expired_keys": 0})

# ======================
# TELEGRAM BOT LOGIC
# ======================
(
    SELECT_ACTION, SELECT_DB, 
    INPUT_REVOKE_KEY, INPUT_RESET_KEY,
    INPUT_CUSTOM_NAME, INPUT_CUSTOM_DURATION, INPUT_CUSTOM_MAX,
    INPUT_DELETE_KEY, INPUT_UNREVOKE_KEY
) = range(9)

def is_owner(update: Update):
    return update.effective_user.id == OWNER_ID

def bot_start(update: Update, context: CallbackContext):
    if not is_owner(update):
        update.message.reply_text("🚫 Access Denied. Private Panel.")
        return ConversationHandler.END

    context.user_data.clear()

    text = """━━━━━━━━━━━━━━━━━━━━━━━━━━━━
          KAZEHAYAMODZ PANEL          
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[SYSTEM STATUS]
> ALL SYSTEMS OPERATIONAL
> STATUS: ONLINE // SECURE

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Please select an option from the menu Kaze:"""
    
    keyboard = [
        [InlineKeyboardButton("🔑 Generate Key", callback_data="act_gen"), 
         InlineKeyboardButton("🔄 Reset Key", callback_data="act_reset")],
        [InlineKeyboardButton("🚫 Revoke Key", callback_data="act_revoke"), 
         InlineKeyboardButton("🟢 Unrevoke Key", callback_data="act_unrevoke")],
        [InlineKeyboardButton("❌ Delete Key", callback_data="act_delete"),
         InlineKeyboardButton("📊 Stats", callback_data="act_stats")],
        [InlineKeyboardButton("⚡ List Keys", callback_data="act_listact"), 
         InlineKeyboardButton("🔴 Revoked History", callback_data="act_listhist")],
        [InlineKeyboardButton("🔥 Custom Key", callback_data="act_custom")]
    ]
    
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SELECT_ACTION

def handle_db(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    if query.data == "back_main":
        try: query.message.delete()
        except: pass
        
        keyboard = [
            [InlineKeyboardButton("🔑 Generate Key", callback_data="act_gen"), InlineKeyboardButton("🔄 Reset Key", callback_data="act_reset")],
            [InlineKeyboardButton("🚫 Revoke Key", callback_data="act_revoke"), InlineKeyboardButton("🗑️ Delete Key", callback_data="act_delete")],
            [InlineKeyboardButton("🟢 Active Keys", callback_data="act_listact"), InlineKeyboardButton("🔴 Revoked History", callback_data="act_listhist")],
            [InlineKeyboardButton("📊 Stats", callback_data="act_stats"), InlineKeyboardButton("🔥 Custom Key", callback_data="act_custom")]
        ]
        context.bot.send_message(chat_id=query.message.chat_id, text="🎮 **KAZE CENTRAL CONTROL PANEL**\n\nPumili ng aksyon sa ibaba:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return SELECT_ACTION

    if query.data.startswith("db_"):
        target_db = query.data.replace("db_", "")
        context.user_data["target_db"] = target_db

    local_url = "http://127.0.0.1:" + str(os.environ.get("PORT", 10000))
    context.user_data["panel_url"] = local_url
    action = context.user_data.get("action")
    target_db = context.user_data.get("target_db", "injector")

    try: query.message.delete()
    except: pass

    if action == "gen":
        keyboard = [
            [InlineKeyboardButton("1 Day", callback_data="dur_1d"), InlineKeyboardButton("3 Days", callback_data="dur_3d")],
            [InlineKeyboardButton("7 Days", callback_data="dur_7d"), InlineKeyboardButton("30 Days", callback_data="dur_30d")],
            [InlineKeyboardButton("Lifetime", callback_data="dur_lifetime")]
        ]
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🔑 **[{target_db.upper()}] Select Key Duration:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return SELECT_DB

    elif action == "revoke":
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🚫 **[{target_db.upper()}] Enter key to REVOKE:**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
        return INPUT_REVOKE_KEY

    elif action == "delete":
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🗑️ **[{target_db.upper()}] Enter key to DELETE:**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
        return INPUT_DELETE_KEY

    elif action == "reset":
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🔰 **[{target_db.upper()}] Enter key to RESET:**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
        return INPUT_RESET_KEY

    elif action == "unrevoke":
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🟢 **[{target_db.upper()}] Enter key to UNREVOKE:**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
        return INPUT_UNREVOKE_KEY

    elif action in ["listact", "listhist"]:
        try:
            target_status = "active" if action == "listact" else "revoked"
            response = requests.get(f"{local_url}/list?status={target_status}&target={target_db}", timeout=15)
            
            if response.status_code != 200:
                context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Server error {response.status_code}")
                return ConversationHandler.END
                
            r = response.json()
            if not isinstance(r, list) or len(r) == 0:
                header_title = "ACTIVE" if target_status == "active" else "REVOKED HISTORY"
                context.bot.send_message(chat_id=query.message.chat_id, text=f"📋 **[{target_db.upper()} - {header_title}]**\nNo keys found.")
                return ConversationHandler.END
            
            msg = f"🟢 **[{target_db.upper()}] ACTIVE KEYS**\n\n" if target_status == "active" else f"🔴 **[{target_db.upper()}] REVOKED HISTORY**\n\n"
            for k in r[:20]:
                msg += f"`{k.get('key')}` | Dev: {k.get('device') or 'None'} (Max: {k.get('max_devices', 1)})\n"
                
            context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Failed to fetch keys: {e}")
        return ConversationHandler.END

    elif action == "stats":
        try:
            r = requests.get(f"{local_url}/stats?target={target_db}", timeout=15).json()
            msg = f"📊 **[{target_db.upper()}] PANEL STATISTICS**\n\nTotal Keys: {r['total_keys']}\nActive Keys: {r['active_keys']}\nExpired Keys: {r['expired_keys']}"
            context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode="Markdown")
        except Exception:
            context.bot.send_message(chat_id=query.message.chat_id, text="❌ Failed to fetch stats.")
        return ConversationHandler.END

    elif action == "custom":
        context.bot.send_message(chat_id=query.message.chat_id, text=f"🔰 **[{target_db.upper()}] Enter Custom Name:**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
        return INPUT_CUSTOM_NAME

    if query.data.startswith("dur_"):
        duration = query.data.replace("dur_", "")
        try:
            token_res = requests.get(f"{local_url}/token?src=bot", timeout=15).json()
            tok = token_res.get("token")
            r = requests.get(f"{local_url}/getkey?token={tok}&src=bot&duration={duration}&target={target_db}", timeout=15).json()
            key = r.get("key", "ERROR")
            
            msg = f"🔑 **[{target_db.upper()}] KEY GENERATED**\n━━━━━━━━━━━━━━━━━━━━\n🔑 KEY: `{key}`\n⏳ EXPIRATION: `{duration}`\n🚫 SLOTS: 1 Device\n━━━━━━━━━━━━━━━━━━━━"
            context.bot.send_message(chat_id=query.message.chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Error Generating Key: {e}")
        return ConversationHandler.END

def execute_revoke(update: Update, context: CallbackContext):
    key = update.message.text.strip()
    local_url = context.user_data.get("panel_url")
    target_db = context.user_data.get("target_db", "injector")
    try:
        r = requests.get(f"{local_url}/revoke?key={key}&target={target_db}", timeout=15)
        if r.status_code == 200:
            update.message.reply_text(f"🚫 **[{target_db.upper()}] KEY REVOKED**\n\n**Key:** `{key}`", parse_mode="Markdown")
        else:
            update.message.reply_text(f"❌ Failed to revoke key `{key}`.", parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def execute_unrevoke(update: Update, context: CallbackContext):
    key = update.message.text.strip()
    local_url = context.user_data.get("panel_url")
    target_db = context.user_data.get("target_db", "injector")
    try:
        r = requests.get(f"{local_url}/unrevoke?key={key}&target={target_db}", timeout=15)
        if r.status_code == 200:
            update.message.reply_text(f"🟢 **[{target_db.upper()}] KEY UNREVOKED**\n\n**Key:** `{key}`", parse_mode="Markdown")
        else:
            update.message.reply_text(f"❌ Failed to unrevoke key `{key}`.", parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def execute_delete(update: Update, context: CallbackContext):
    key = update.message.text.strip()
    local_url = context.user_data.get("panel_url")
    target_db = context.user_data.get("target_db", "injector")
    try:
        r = requests.get(f"{local_url}/delete?key={key}&target={target_db}", timeout=15)
        if r.status_code == 200:
            update.message.reply_text(f"🗑️ **[{target_db.upper()}] KEY DELETED**\n\n**Key:** `{key}`", parse_mode="Markdown")
        else:
            update.message.reply_text(f"❌ Failed to delete key `{key}`.", parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def execute_reset(update: Update, context: CallbackContext):
    key = update.message.text.strip()
    local_url = context.user_data.get("panel_url")
    target_db = context.user_data.get("target_db", "injector")
    try:
        r = requests.get(f"{local_url}/reset?key={key}&target={target_db}", timeout=15)
        if r.status_code == 200:
            update.message.reply_text(f"🔄 **[{target_db.upper()}] KEY RESET**\n\n**Key:** `{key}`", parse_mode="Markdown")
        else:
            update.message.reply_text(f"❌ Failed to reset key `{key}`.", parse_mode="Markdown")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def execute_custom_name(update: Update, context: CallbackContext):
    context.user_data["custom_name"] = update.message.text.strip()
    update.message.reply_text("➡️ **Enter Duration (e.g., 1d, 7d, 30d, lifetime):**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
    return INPUT_CUSTOM_DURATION

def execute_custom_duration(update: Update, context: CallbackContext):
    context.user_data["custom_duration"] = update.message.text.strip()
    update.message.reply_text("➡️ **Enter Max Devices / Slots (e.g., 1, 5, 9999):**", reply_markup=ForceReply(selective=True), parse_mode="Markdown")
    return INPUT_CUSTOM_MAX

def execute_custom_max(update: Update, context: CallbackContext):
    max_dev = update.message.text.strip()
    name = context.user_data.get("custom_name")
    duration = context.user_data.get("custom_duration")
    local_url = context.user_data.get("panel_url")
    target_db = context.user_data.get("target_db", "injector")

    try:
        r = requests.get(f"{local_url}/customkey?name={name}&duration={duration}&max={max_dev}&target={target_db}", timeout=15)
        if r.status_code == 200:
            generated_key = r.json().get("key")
            msg = f"🎁 **[{target_db.upper()}] CUSTOM KEY CREATED**\n━━━━━━━━━━━━━━━━━━━━\n🔑 KEY: `{generated_key}`\n⏳ DURATION: `{duration}`\n🚫 SLOTS: {max_dev} Device(s)\n━━━━━━━━━━━━━━━━━━━━"
            update.message.reply_text(msg, parse_mode="Markdown")
        else:
            update.message.reply_text("❌ Failed to create custom key.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

def bot_cancel(update: Update, context: CallbackContext):
    update.message.reply_text("❌ Process cancelled.")
    return ConversationHandler.END

def run_telegram_bot():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing, skipping Telegram Bot startup.")
        return

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", bot_start)],
        states={
            SELECT_ACTION: [CallbackQueryHandler(handle_action, pattern="^act_")],
            SELECT_DB: [
                CallbackQueryHandler(handle_db, pattern="^db_"),
                CallbackQueryHandler(handle_db, pattern="^dur_"),
                CallbackQueryHandler(handle_db, pattern="^back_main")
            ],
            INPUT_REVOKE_KEY: [MessageHandler(Filters.text & ~Filters.command, execute_revoke)],
            INPUT_DELETE_KEY: [MessageHandler(Filters.text & ~Filters.command, execute_delete)],
            INPUT_UNREVOKE_KEY: [MessageHandler(Filters.text & ~Filters.command, execute_unrevoke)],
            INPUT_RESET_KEY: [MessageHandler(Filters.text & ~Filters.command, execute_reset)],
            INPUT_CUSTOM_NAME: [MessageHandler(Filters.text & ~Filters.command, execute_custom_name)],
            INPUT_CUSTOM_DURATION: [MessageHandler(Filters.text & ~Filters.command, execute_custom_duration)],
            INPUT_CUSTOM_MAX: [MessageHandler(Filters.text & ~Filters.command, execute_custom_max)],
        },
        fallbacks=[CommandHandler("cancel", bot_cancel)]
    )

    dp.add_handler(conv_handler)
    updater.start_polling()

# ======================
# STARTUP RUNNER
# ======================
if __name__ == "__main__":
    bot_thread = Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
