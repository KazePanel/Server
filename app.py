import os
import random
import string
import time
import uuid
from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

app = Flask(__name__)
CORS(app)

# ======================
# CONSTANTS & LOCAL MEMORY (RAM)
# ======================
TOKEN_EXPIRY = 20
COOLDOWN = 120
KEY_LIMIT = 120
COOLDOWN_LIMIT = 86400  # 24 Hours in seconds

db_cache = {"tokens": {}, "device_limit": {}, "daily_limit": {}}

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

DB_URL_INJECTOR = os.getenv("DATABASE_URL_INJECTOR") or os.getenv("DATABASE_URL")
DB_URL_SCRIPT = os.getenv("DATABASE_URL_SCRIPT")


def get_db_connection(db_type="injector"):
    if db_type == "script":
        url = DB_URL_SCRIPT
        db_name = "DATABASE_URL_SCRIPT"
    else:
        url = DB_URL_INJECTOR
        db_name = "DATABASE_URL_INJECTOR"

    if not url:
        raise ValueError(f"{db_name} environment variable is missing sa Render!")
    return psycopg2.connect(url)


def cleanup():
    now = time.time()
    for t in list(db_cache["tokens"].keys()):
        if now - db_cache["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db_cache["tokens"][t]
    for dev in list(db_cache["device_limit"].keys()):
        if now - db_cache["device_limit"][dev] > KEY_LIMIT:
            del db_cache["device_limit"][dev]
            
    for dev in list(db_cache["daily_limit"].keys()):
        if now - db_cache["daily_limit"][dev]["time"] > COOLDOWN_LIMIT:
            del db_cache["daily_limit"][dev]


def is_vpn_or_proxy(ip: str) -> bool:
    if ip in ["127.0.0.1", "localhost", "::1"]:
        return False
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}?fields=status,hosting", timeout=3)
        data = response.json()
        if data.get("status") == "success":
            if data.get("hosting") == True:
                return True
    except Exception:
        pass
    return False


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not OWNER_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass


def format_remaining_time(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "Expired"
    if seconds >= 900000000:
        return "Lifetime"

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")

    if not parts:
        return "Less than 1m"
    return " ".join(parts)


def convert_duration(duration: str) -> int:
    if not duration:
        return 10800
    duration = str(duration).lower().strip()
    try:
        if duration.endswith("m"):
            return int(duration[:-1]) * 60
        if duration.endswith("h"):
            return int(duration[:-1]) * 3600
        if duration.endswith("d"):
            return int(duration[:-1]) * 86400
        if duration == "lifetime":
            return 999999999
        return int(duration)
    except ValueError:
        return 10800


@app.route("/")
def home():
    return "KAZE SERVER ONLINE"


@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    device_id = request.args.get("device_id")

    if is_vpn_or_proxy(ip):
        return jsonify({
            "status": "error",
            "type": "vpn",
            "message": "VPN detected please turn off your vpn"
        }), 403

    if not device_id:
        return jsonify({"status": "error", "message": "Missing device ID"}), 400

    if device_id in db_cache["daily_limit"]:
        elapsed = now - db_cache["daily_limit"][device_id]["time"]
        remaining_sec = int(COOLDOWN_LIMIT - elapsed)
        return jsonify({
            "status": "limit",
            "type": "limit",
            "remaining_seconds": remaining_sec,
            "message": "Your free key has ended please try again tomorrow"
        }), 403

    token_id = str(uuid.uuid4())
    db_cache["tokens"][token_id] = {"device_id": device_id, "time": now}
    return jsonify({"status": "success", "token": token_id})


def handle_getkey(db_type):
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "3h")
    max_dev = request.args.get("max", "1")
    now = time.time()

    if not token_id or token_id not in db_cache["tokens"]:
        return jsonify({"status": "error", "message": "Token expired"}), 403

    token_data = db_cache["tokens"][token_id]
    device_id = token_data["device_id"]
    ip = request.remote_addr

    if is_vpn_or_proxy(ip):
        return jsonify({"status": "error", "type": "vpn", "message": "VPN detected please turn off your vpn"}), 403

    if device_id in db_cache["daily_limit"]:
        elapsed = now - db_cache["daily_limit"][device_id]["time"]
        remaining_sec = int(COOLDOWN_LIMIT - elapsed)
        return jsonify({
            "status": "limit",
            "type": "limit",
            "remaining_seconds": remaining_sec,
            "message": "Your free key has ended please try again tomorrow"
        }), 403

    if device_id in db_cache["device_limit"]:
        wait = int(KEY_LIMIT - (now - db_cache["device_limit"][device_id]))
        if wait > 0:
            return jsonify({"status": "wait", "message": "Bypass detected!"}), 403

    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"
    key = prefix + "".join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
            """,
            (key, now + expiry_seconds, int(max_dev)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500

    db_cache["device_limit"][device_id] = now
    db_cache["daily_limit"][device_id] = {"time": now}
    del db_cache["tokens"][token_id]

    return jsonify({
        "status": "success",
        "key": key,
        "expires_in": expiry_seconds,
        "max_devices": max_dev,
    })


def handle_customkey(db_type):
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "3h")
    max_dev = request.args.get("max", "1")
    now = time.time()

    if not custom_name:
        return jsonify({"status": "error", "message": "Custom key name is missing"}), 400

    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key name already exists!"}), 409

        cur.execute(
            """
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
            """,
            (key, now + expiry_seconds, int(max_dev)),
        )
        conn.commit()
        cur.close()
        conn.close()

        tag = "[SCRIPT]" if db_type == "script" else "[INJECTOR]"
        send_telegram_alert(
            f"🎁 *{tag} Custom Key Created*\n"
            f"Key: `{key}`\n"
            f"Duration: `{duration}`\n"
            f"Max Devices: `{max_dev}`"
        )
        return jsonify({
            "status": "success",
            "key": key,
            "expires_in": expiry_seconds,
            "max_devices": max_dev,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_verify(db_type):
    cleanup()
    key = request.args.get("key")
    device = request.args.get("device")
    if not key or not device:
        return jsonify({"status": "invalid"}), 400

    conn = get_db_connection(db_type)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM keys WHERE key_code = %s;", (key,))
    data = cur.fetchone()

    tag = "[SCRIPT]" if db_type == "script" else "[INJECTOR]"

    if not data:
        cur.close()
        conn.close()
        return jsonify({"status": "invalid"})

    if data["revoked"]:
        cur.close()
        conn.close()
        send_telegram_alert(f"❌ *{tag} Key Revoked Attempt*\nKey: `{key}`\nDevice: `{device}`")
        return jsonify({"status": "revoked"})

    now = time.time()
    if now > data["expiry"]:
        cur.close()
        conn.close()
        send_telegram_alert(f"❌ *{tag} Key Expired Attempt*\nKey: `{key}`\nDevice: `{device}`")
        return jsonify({"status": "expired"})

    current_devices = data["device"].split(",") if data["device"] else []
    max_allowed = data.get("max_devices", 1)
    remaining_seconds = int(data["expiry"] - now)
    time_left_str = format_remaining_time(remaining_seconds)

    def success_response():
        return jsonify({
            "status": "valid",
            "expires_in_sec": remaining_seconds,
            "expire_str": time_left_str,
        })

    if device in current_devices:
        cur.close()
        conn.close()
        device_index = current_devices.index(device) + 1
        counter_str = f" ({device_index}/{max_allowed})" if max_allowed > 1 else ""
        send_telegram_alert(
            f"✓ *{tag} Key Used{counter_str}*\n"
            f"Key: `{key}`\n"
            f"Device: `{device}`\n"
            f"Expires in: `{time_left_str}`"
        )
        return success_response()

    if len(current_devices) < max_allowed:
        current_devices.append(device)
        new_device_string = ",".join(current_devices)

        cur.execute(
            "UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;",
            (new_device_string, now, key),
        )
        conn.commit()
        cur.close()
        conn.close()

        counter_str = (
            f" ({len(current_devices)}/{max_allowed})" if max_allowed > 1 else ""
        )
        send_telegram_alert(
            f"✓ *{tag} Key Used{counter_str}*\n"
            f"Key: `{key}`\n"
            f"Device: `{device}`\n"
            f"Expires in: `{time_left_str}`"
        )
        return success_response()

    cur.close()
    conn.close()
    send_telegram_alert(
        f"🔒 *{tag} Max Device Limit Reached*\n"
        f"Key: `{key}`\n"
        f"Attempt Device: `{device}`\n"
        f"Slots: `{len(current_devices)}/{max_allowed}`"
    )
    return jsonify({"status": "locked"})


def handle_unrevoke(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("UPDATE keys SET revoked = FALSE WHERE key_code = %s;", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_reset(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error"}), 400
    conn = get_db_connection(db_type)
    cur = conn.cursor()
    cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "success"})


def handle_list(db_type):
    try:
        status_filter = request.args.get("status", "active")
        now = time.time()
        conn = get_db_connection(db_type)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if status_filter == "revoked":
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = TRUE ORDER BY expiry DESC;")
        else:
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = FALSE AND expiry > %s ORDER BY expiry ASC;", (now,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for r in rows:
            result.append({
                "key": r.get("key_code") or "UNKNOWN",
                "device": r.get("device"),
                "max_devices": r.get("max_devices") or 1,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_delete(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("DELETE FROM keys WHERE key_code = %s;", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_stats(db_type):
    try:
        now = time.time()
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM keys;")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
        active = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({"total_keys": total, "active_keys": active, "expired_keys": total - active})
    except Exception:
        return jsonify({"total_keys": 0, "active_keys": 0, "expired_keys": 0})

from flask import request

@app.route("/upload_screenshot", methods=["POST"])
def upload_screenshot():
    db_type = request.form.get("db_type", "injector")
    device_id = request.form.get("device", "Unknown Device")
    
    # Kunin ang file na pinadala ng injector
    if "screenshot" not in request.files:
        return jsonify({"status": "error", "message": "No image attached"}), 400
    
    file = request.files["screenshot"]
    
    # I-forward ang screenshot sa Telegram Bot mo
    if TELEGRAM_BOT_TOKEN and OWNER_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {"photo": (file.filename, file.read(), file.content_type)}
        payload = {
            "chat_id": OWNER_ID,
            "caption": f"📸 *Auto Feedback / Screenshot*\nDevice ID: `{device_id}`"
        }
        try:
            requests.post(url, data=payload, files=files, timeout=10)
        except Exception as e:
            pass

    return jsonify({"status": "success"})
    

@app.route("/getkey")
def getkey_injector(): return handle_getkey("injector")
@app.route("/customkey")
def custom_key_injector(): return handle_customkey("injector")
@app.route("/verify")
def verify_injector(): return handle_verify("injector")
@app.route("/revoke")
def revoke_injector(): return jsonify({"status": "success"})
@app.route("/unrevoke")
def unrevoke_injector(): return handle_unrevoke("injector")
@app.route("/reset")
def reset_injector(): return handle_reset("injector")
@app.route("/list")
def list_injector(): return handle_list("injector")
@app.route("/delete")
def delete_injector(): return handle_delete("injector")
@app.route("/stats")
def stats_injector(): return handle_stats("injector")

@app.route("/script/getkey")
def getkey_script(): return handle_getkey("script")
@app.route("/script/customkey")
def custom_key_script(): return handle_customkey("script")
@app.route("/script/verify")
def verify_script(): return handle_verify("script")
@app.route("/script/revoke")
def revoke_script(): return jsonify({"status": "success"})
@app.route("/script/unrevoke")
def unrevoke_script(): return handle_unrevoke("script")
@app.route("/script/reset")
def reset_script(): return handle_reset("script")
@app.route("/script/list")
def list_script(): return handle_list("script")
@app.route("/script/delete")
def delete_script(): return handle_delete("script")
@app.route("/script/stats")
def stats_script(): return handle_stats("script")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
