import telebot
import requests
import os
import time
import json
import re
import collections
import hashlib
from datetime import datetime
from telebot import types, apihelper
from flask import Flask
from threading import Thread

CONFIG_FILE = "config.json"
USERS_FILE = "users.json"

# In-memory tracking for high speed detection and DM rate limits
range_hits_tracker = collections.defaultdict(list)
range_hits_count = collections.defaultdict(int)
seen_console_hits = set()
active_ranges_global = set()
active_user_watchers = set()

# --- Database Manager (Firebase + Local Fallback) ---
class DatabaseManager:
    def __init__(self, db_url=None):
        self.db_url = db_url.rstrip('/') if db_url else None
        self.local_file = "firebase_fallback_db.json"
        self.local_data = {}
        self._load_local()

    def _load_local(self):
        if os.path.exists(self.local_file):
            try:
                with open(self.local_file, "r") as f:
                    self.local_data = json.load(f)
            except:
                self.local_data = {}
        if "users" not in self.local_data: self.local_data["users"] = {}
        if "processed_numbers" not in self.local_data: self.local_data["processed_numbers"] = {}
        if "withdraws" not in self.local_data: self.local_data["withdraws"] = {}

    def _save_local(self):
        try:
            with open(self.local_file, "w") as f:
                json.dump(self.local_data, f, indent=4)
        except:
            pass

    def get_user(self, user_id):
        uid = str(user_id)
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/users/{uid}.json", timeout=5)
                if res.status_code == 200 and res.json() is not None:
                    u_data = res.json()
                    if "referrer" not in u_data: u_data["referrer"] = None
                    if "ref_earnings" not in u_data: u_data["ref_earnings"] = 0.0
                    if "ref_count" not in u_data: u_data["ref_count"] = 0
                    if "lang" not in u_data: u_data["lang"] = None
                    return u_data
            except Exception as e:
                print(f"Firebase get_user error: {e}")
        
        if uid not in self.local_data["users"]:
            self.local_data["users"][uid] = {
                "balance": 0.0, 
                "username": "", 
                "id": int(uid),
                "referrer": None,
                "ref_earnings": 0.0,
                "ref_count": 0,
                "lang": None
            }
            self._save_local()
        return self.local_data["users"][uid]

    def set_user_language(self, user_id, lang):
        user = self.get_user(user_id)
        user["lang"] = lang
        self.save_user(user_id, user)

    def get_all_user_ids(self):
        uids = set()
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/users.json", timeout=8)
                if res.status_code == 200 and res.json() is not None:
                    for k in res.json().keys():
                        try: uids.add(int(k))
                        except: pass
            except Exception as e:
                print(f"Firebase get_all_user_ids error: {e}")
        
        for k in self.local_data.get("users", {}).keys():
            try: uids.add(int(k))
            except: pass
        return uids

    def save_user(self, user_id, data):
        uid = str(user_id)
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/users/{uid}.json", json=data, timeout=5)
                return
            except Exception as e:
                print(f"Firebase save_user error: {e}")
        self.local_data["users"][uid] = data
        self._save_local()

    def update_user_balance(self, user_id, amount):
        user = self.get_user(user_id)
        current_bal = float(user.get("balance", 0.0))
        new_bal = round(current_bal + amount, 4)
        user["balance"] = new_bal
        self.save_user(user_id, user)
        return new_bal

    def add_referral_earning(self, referrer_id, amount):
        ref_user = self.get_user(referrer_id)
        if ref_user:
            current_bal = float(ref_user.get("balance", 0.0))
            current_ref_earn = float(ref_user.get("ref_earnings", 0.0))
            ref_user["balance"] = round(current_bal + amount, 4)
            ref_user["ref_earnings"] = round(current_ref_earn + amount, 4)
            self.save_user(referrer_id, ref_user)

    def set_referrer(self, user_id, referrer_id):
        if int(user_id) == int(referrer_id): return
        user = self.get_user(user_id)
        if not user.get("referrer"):
            user["referrer"] = int(referrer_id)
            self.save_user(user_id, user)
            ref_user = self.get_user(referrer_id)
            if ref_user:
                ref_user["ref_count"] = int(ref_user.get("ref_count", 0)) + 1
                self.save_user(referrer_id, ref_user)

    def has_number_received_otp(self, number):
        num = str(number).replace("+", "").strip()
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/processed_numbers/{num}.json", timeout=5)
                if res.status_code == 200 and res.json() is not None:
                    return res.json() is True
            except: pass
        return self.local_data["processed_numbers"].get(num) is True

    def mark_number_received_otp(self, number):
        num = str(number).replace("+", "").strip()
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/processed_numbers/{num}.json", json=True, timeout=5)
                return
            except: pass
        self.local_data["processed_numbers"][num] = True
        self._save_local()

    def save_withdraw(self, req_id, data):
        r_id = str(req_id)
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/withdraws/{r_id}.json", json=data, timeout=5)
                return
            except: pass
        self.local_data["withdraws"][r_id] = data
        self._save_local()

    def get_withdraw(self, req_id):
        r_id = str(req_id)
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/withdraws/{r_id}.json", timeout=5)
                if res.status_code == 200 and res.json() is not None:
                    return res.json()
            except: pass
        return self.local_data["withdraws"].get(r_id)

def load_config():
    default_config = {
        "BOT_TOKEN": "8979736100:AAGisd9PFE9jwtThFCKR-TAPqkgbsTIZDVs", 
        "ZENEX_API_KEY": "ZNX_GWKKMCVK6JX425VXRTVP5NYV",  
        "BASE_URL": "https://api.zenexnetwork.com/v1", 
        "ADMIN_ID": 8262679678,
        "BOT_NAME": "👑 SHS OTP HUB 👑", 
        "BOT_USERNAME": "SHS_SMSHUB_bot", 
        "DEV_USERNAME": "Saku_143",
        "FIREBASE_DB_URL": "https://shsotpbot-default-rtdb.firebaseio.com/",
        "BALANCE_TEXT": "💰 ওটিপি রিসিভ করে টাকা ইনকাম করুন! প্রতি সফল ওটিপিতে পাবেন ০.২০ টাকা।",
        "WITHDRAW_TEXT": "📉 মিনিমাম উইথড্র ৫০ টাকা। পেমেন্ট গেটওয়েগুলো চেক করুন।",
        "BOT_STATUS": "ON",
        "BOT_OFF_REASON": "",
        "PAYMENT_SETTINGS": {
            "bkash": True,
            "nagad": True,
            "binance": True
        },
        "CHANNELS_TO_JOIN": [
            {"id": "-1003956226642", "link": "https://t.me/SHS_Otp_Channel", "name": "📢 Payment Channel"},
            {"id": "-1002183552076", "link": "https://t.me/winfanti", "name": "💬 Support Channel"}
        ],
        "GROUPS_TO_JOIN": [
            {"id": "-1004309875319", "link": "https://t.me/+DXdDIm7-rRU4YTQ1", "name": "👥 OTP Support Group"}
        ],
        "OTP_DESTINATIONS": [
            "-1004309875319"  # শুধুমাত্র নির্দিষ্ট ওটিপি গ্রুপে ওটিপি ফিড যাবে
        ],
        "NOTICE": "👋 আমাদের বটে স্বাগতম! ফুল স্পিডে ওটিপি রিসিভ করুন।",
        "CUSTOM_SERVICES": [],
        "SERVICES": {
            "facebook": {
                "name": "📘 Facebook", 
                "rids": {
                    "Togo 🇹🇬": "228964XXX",
                    "Central African Republic 🇨🇫": "23672XXX",
                    "Madagascar 🇲🇬": "26134XXX",
                    "Tajikistan 🇹🇯": "99290XXX"
                }
            },
            "instagram": {"name": "📸 Instagram", "rids": {"Central African Republic 🇨🇫": "23672XXX"}},
            "whatsapp": {"name": "💚 WhatsApp", "rids": {"Tajikistan 🇹🇯": "99290XXX"}},
            "telegram": {"name": "✈️ Telegram", "rids": {"Togo 🇹🇬": "228964XXX"}},
            "imo": {"name": "📱 Imo", "rids": {}},
            "tiktok": {"name": "🎵 TikTok", "rids": {}},
            "discord": {"name": "👾 Discord", "rids": {}}
        }
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
                loaded["OTP_DESTINATIONS"] = ["-1004309875319"] # গ্রুপ আইডি লক করা হয়েছে
                return loaded
        except:
            return default_config
    else:
        with open(CONFIG_FILE, "w") as f:
            json.dump(default_config, f, indent=4)
        return default_config

def save_config(config_data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=4)

config = load_config()
db = DatabaseManager(config.get("FIREBASE_DB_URL"))

def load_users():
    uids = db.get_all_user_ids()
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                file_uids = json.load(f)
                for u in file_uids:
                    try: uids.add(int(u))
                    except: pass
        except: pass
    return uids

def save_users(users_set):
    try:
        clean_list = [int(uid) for uid in users_set if int(uid) > 0]
        with open(USERS_FILE, "w") as f:
            json.dump(clean_list, f, indent=4)
    except: pass

all_users = load_users()
apihelper.ENABLE_MIDDLEWARE = True 
bot = telebot.TeleBot(config["BOT_TOKEN"], parse_mode=None)

app = Flask('')
admin_temp_data = {}

# --- Country Code Resolver ---
def get_country_info_by_range(range_val):
    if not range_val: return "Global 🌐"
    
    clean_range = str(range_val).strip().upper().replace("XXX", "").replace("+", "")
    
    prefix_map = {
        "228": "Togo 🇹🇬",
        "236": "Central African Republic 🇨🇫",
        "261": "Madagascar 🇲🇬",
        "992": "Tajikistan 🇹🇯",
        "231": "Liberia 🇱🇷",
        "224": "Guinea 🇬🇳",
        "225": "Ivory Coast 🇨🇮",
        "996": "Kyrgyzstan 🇰🇬",
        "380": "Ukraine 🇺🇦",
        "880": "Bangladesh 🇧🇩",
        "234": "Nigeria 🇳🇬",
        "232": "Sierra Leone 🇸🇱",
        "1": "United States 🇺🇸",
        "44": "United Kingdom 🇬🇧",
        "91": "India 🇮🇳",
        "92": "Pakistan 🇵🇰"
    }
    
    sorted_prefixes = sorted(prefix_map.keys(), key=len, reverse=True)
    for prefix in sorted_prefixes:
        if clean_range.startswith(prefix):
            return prefix_map[prefix]
            
    return f"Country (+{clean_range[:3]}) 🌐" if len(clean_range) >= 3 else "Global 🌐"

def get_country_code_short(range_val):
    info = get_country_info_by_range(range_val)
    if "Togo" in info: return "TG"
    if "Central African" in info: return "CF"
    if "Madagascar" in info: return "MG"
    if "Tajikistan" in info: return "TJ"
    if "Liberia" in info: return "LR"
    if "Guinea" in info: return "GN"
    return "GLOBAL"

def get_service_icon(service_name):
    s = str(service_name).lower()
    if "facebook" in s or "fb" in s: return "📘"
    if "instagram" in s or "ig" in s or "insta" in s: return "📸"
    if "whatsapp" in s or "wa" in s: return "💚"
    if "telegram" in s or "tg" in s: return "✈️"
    if "imo" in s: return "📱"
    if "tiktok" in s or "tt" in s: return "🎵"
    if "discord" in s: return "👾"
    return "✨"

def get_api_headers():
    return {
        "mapikey": str(config.get("ZENEX_API_KEY", "")).strip(),
        "Content-Type": "application/json"
    }

def safe_send_message(chat_id, text, reply_markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        try:
            clean_text = str(text).replace("*", "").replace("`", "").replace("_", "")
            return bot.send_message(chat_id, clean_text, reply_markup=reply_markup)
        except Exception as e2:
            print(f"Failed to send message to {chat_id}: {e2}")
            return None

def format_full_phone_number(num):
    num_str = str(num).replace("+", "").strip()
    return f"+{num_str}"

def format_group_phone_number(num):
    num_str = str(num).replace("+", "").strip()
    if len(num_str) <= 8:
        return f"+{num_str[:2]}****{num_str[-2:]}"
    first_4 = num_str[:4]
    last_4 = num_str[-4:]
    masked_part = "*" * (len(num_str) - 8)
    return f"{first_4}{masked_part}**"

def track_user(user_id, referrer_id=None):
    global all_users
    try:
        u_id = int(user_id)
        if u_id > 0:  
            if u_id not in all_users:
                all_users.add(u_id)
                db.get_user(u_id)
                if referrer_id:
                    db.set_referrer(u_id, referrer_id)
                save_users(all_users)
            else:
                db.get_user(u_id)
    except: pass

def is_subscribed_all(user_id):
    if user_id == int(config["ADMIN_ID"]): return True 
    for ch in config.get("CHANNELS_TO_JOIN", []):
        try:
            status = bot.get_chat_member(int(ch["id"]), user_id).status
            if status in ['left', 'kicked', 'restricted']: return False
        except: pass 
    for grp in config.get("GROUPS_TO_JOIN", []):
        try:
            status = bot.get_chat_member(int(grp["id"]), user_id).status
            if status in ['left', 'kicked', 'restricted']: return False
        except: pass
    return True

def format_rid(rid):
    rid_str = str(rid).strip().replace("+", "")
    if not rid_str.upper().endswith("XXX"):
        return f"{rid_str}XXX"
    return rid_str

def reward_user_for_otp(user_id, phone_number, service_name=None):
    clean_num = str(phone_number).replace("+", "").strip()
    if db.has_number_received_otp(clean_num):
        return False, db.get_user(user_id).get("balance", 0.0)
        
    reward_amount = 0.00 if (service_name and str(service_name).lower().strip() == "whatsapp") else 0.20
    db.mark_number_received_otp(clean_num)
    new_bal = db.update_user_balance(user_id, reward_amount)
    
    if reward_amount > 0:
        user_info = db.get_user(user_id)
        referrer_id = user_info.get("referrer")
        if referrer_id:
            ref_commission = round(reward_amount * 0.03, 4)
            db.add_referral_earning(referrer_id, ref_commission)
            try:
                bot.send_message(referrer_id, f"🎉 **রেফারেল বোনাস!**\n\nআপনার রেফারের ইউজার ওটিপি রিসিভ করায় পেয়ে গেছেন: `+{ref_commission} BDT`")
            except: pass
            
    return True, new_bal

def get_otp_group_link():
    return "https://t.me/+DXdDIm7-rRU4YTQ1"

# --- UI Keyboards ---
def send_home_keyboard(chat_id, text=None):
    track_user(chat_id)
    u_data = db.get_user(chat_id)
    lang = u_data.get("lang", "bn") or "bn"
    bot_name = config.get("BOT_NAME", "👑 SHS OTP HUB 👑")
    
    if not text:
        text = f"👋 **{bot_name} এ আপনাকে স্বাগতম!**\n\n📢 **নোটিশ:** {config.get('NOTICE', 'স্বাগতম!')}"
        
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("📱 Get Number"))
    markup.row(types.KeyboardButton("👥 Reffer & Earn"), types.KeyboardButton("📊 Dashboard"))
    markup.row(types.KeyboardButton("🔑 2FA CODE"), types.KeyboardButton("📊 Live Traffic"))
    markup.row(types.KeyboardButton("🌐 ভাষা পরিবর্তন"), types.KeyboardButton("💬 Support"))
    
    safe_send_message(chat_id, text, reply_markup=markup)

def send_services_menu(chat_id, message_id=None):
    track_user(chat_id)
    markup = types.InlineKeyboardMarkup()
    services = config.get("SERVICES", {})
    
    row = []
    for s_id, s_info in services.items():
        icon = get_service_icon(s_id)
        name = s_info.get("name", s_id.capitalize())
        row.append(types.InlineKeyboardButton(f"{icon} {name}", callback_data=f"app_{s_id}"))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row: markup.row(*row)
    markup.row(types.InlineKeyboardButton("❌ বন্ধ করুন", callback_data="close_menu"))
    
    text = "🛑 **সক্রিয় সার্ভিস সিলেক্ট করুন** 🔻"
    if message_id:
        try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode="Markdown")
        except: safe_send_message(chat_id, text, reply_markup=markup)
    else:
        safe_send_message(chat_id, text, reply_markup=markup)

# --- Start & Handler Setup ---
@bot.message_handler(commands=['start'], chat_types=['private'])
def start_bot(message):
    chat_id = message.chat.id
    command_args = message.text.split()
    referrer_id = int(command_args[1]) if len(command_args) > 1 and command_args[1].isdigit() else None
    track_user(chat_id, referrer_id)
    
    if not is_subscribed_all(chat_id):
        markup = types.InlineKeyboardMarkup()
        for grp in config.get("GROUPS_TO_JOIN", []):
            markup.row(types.InlineKeyboardButton(grp["name"], url=grp["link"]))
        markup.row(types.InlineKeyboardButton("✅ Joined (Check)", callback_data="check_membership"))
        safe_send_message(chat_id, "⚠️ ওটিপি পেতে গ্রুপে জয়েন করুন:", reply_markup=markup)
        return

    send_home_keyboard(chat_id)

@bot.message_handler(commands=['admin'], chat_types=['private'])
def handle_admin_command(message):
    if message.chat.id == int(config["ADMIN_ID"]):
        show_admin_dashboard(message.chat.id)

@bot.message_handler(func=lambda m: True, chat_types=['private'])
def handle_text(message):
    track_user(message.chat.id)
    text = message.text
    
    if text in ["📱 Get Number", "📲 Get Number"]:
        send_services_menu(message.chat.id)
    elif text in ["👥 Reffer & Earn", "👥 Refer & Earn"]:
        u_data = db.get_user(message.chat.id)
        ref_link = f"https://t.me/{config.get('BOT_USERNAME', 'SHS_SMSHUB_bot')}?start={message.chat.id}"
        msg = f"👥 **রেফারেল প্রোগ্রাম (৩% কমিশন)**\n\n🔗 **রেফার লিঙ্ক:**\n`{ref_link}`\n\n💰 **রেফার আয়:** `{u_data.get('ref_earnings', 0.0):.4f} BDT`"
        safe_send_message(message.chat.id, msg)
    elif text in ["📊 Dashboard", "💎 Balance"]:
        u_data = db.get_user(message.chat.id)
        msg = f"📊 **ড্যাশবোর্ড**\n\n• ব্যালেন্স: `{u_data.get('balance', 0.0)} BDT`\n• প্রতি ওটিপি: `0.20 BDT`"
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("📉 Withdraw", callback_data="btn_withdraw_init"))
        safe_send_message(message.chat.id, msg, reply_markup=markup)
    elif text == "💬 Support":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💬 OTP Group", url=get_otp_group_link()))
        markup.add(types.InlineKeyboardButton("📞 Admin Support", url=f"https://t.me/{config.get('DEV_USERNAME', 'Saku_143')}"))
        safe_send_message(message.chat.id, "💬 **সাপোর্ট অপশন:**", reply_markup=markup)

# --- Fast Country & Get Number Engine ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("app_"))
def show_countries(call):
    bot.answer_callback_query(call.id)
    selected_app = call.data.split("_")[1]
    services = config.get("SERVICES", {})
    if selected_app not in services: return
    
    markup = types.InlineKeyboardMarkup()
    rids = services[selected_app]["rids"]
    
    row = []
    for country in rids.keys():
        btn_text = f"🔥 {country}"
        row.append(types.InlineKeyboardButton(btn_text, callback_data=f"c_{country}_{selected_app}"))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row: markup.row(*row)
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_services"))
    
    icon = get_service_icon(selected_app)
    text = f"🌐 **{icon} {selected_app.upper()} - দেশ সিলেক্ট করুন:**"
    try: bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except: safe_send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("c_"))
def request_number(call):
    bot.answer_callback_query(call.id, text="⚡ নম্বর খোঁজা হচ্ছে...")
    _, country, selected_app = call.data.split("_")
    rid = config["SERVICES"][selected_app]["rids"].get(country)
    
    if not rid:
        bot.send_message(call.message.chat.id, "⚠️ এই দেশের কোন সচল রেঞ্জ পাওয়া যায়নি!")
        return
        
    formatted_rid = format_rid(rid)
    base_url = str(config['BASE_URL']).strip().rstrip('/')
    url = f"{base_url}/getnum"
    payload = {"range": str(formatted_rid), "is_national": False, "remove_plus": False}
    
    try:
        response = requests.post(url, json=payload, headers=get_api_headers(), timeout=12)
        res = response.json()
        meta = res.get("meta", {})
        
        if meta.get("code") == 200 or meta.get("status") == "success":
            data_obj = res.get("data", {})
            num_raw = data_obj.get("full_number") or data_obj.get("number")
            full_num = format_full_phone_number(num_raw)
            icon = get_service_icon(selected_app)
            
            msg = (f"⚡ **নম্বর নেওয়া হয়েছে!**\n\n"
                   f"📱 Service ➔ **{icon} {selected_app.upper()}**\n"
                   f"🌐 Country ➔ **{country}**\n"
                   f"📞 Number: `{full_num}`\n\n"
                   f"⏳ Status: **ওটিপির জন্য অপেক্ষা করা হচ্ছে...**")
            
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("🔄 Fetch Code (Manual)", callback_data=f"fetch_{selected_app}_{country}_{num_raw}"),
                types.InlineKeyboardButton("🔄 Change Number", callback_data=f"c_{country}_{selected_app}")
            )
            markup.row(types.InlineKeyboardButton("📋 Copy Number", callback_data=f"copynum_{full_num}"))
            markup.row(types.InlineKeyboardButton("🔗 View OTP Group", url=get_otp_group_link()))
            
            safe_send_message(call.message.chat.id, msg, reply_markup=markup)
            
            # ব্যাকগ্রাউন্ডে ইনবক্স ওটিপি ওয়াচার চালু
            Thread(target=background_user_otp_watcher, args=(call.message.chat.id, selected_app, country, num_raw), daemon=True).start()
        else:
            safe_send_message(call.message.chat.id, f"❌ স্টক খালি: {res.get('message', 'অন্য একটি দেশ চেষ্টা করুন')}")
    except Exception as e:
        safe_send_message(call.message.chat.id, "⚠️ নেটওয়ার্ক সমস্যা, আবার চেষ্টা করুন!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("copynum_"))
def copy_number_alert(call):
    bot.answer_callback_query(call.id, text=f"📞 {call.data.split('_')[1]}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("copyotp_"))
def copy_otp_alert(call):
    bot.answer_callback_query(call.id, text=f"🔑 Code: {call.data.split('_')[1]}", show_alert=True)

# --- OTP Receiver for User (Full Number in DM + Group Alert) ---
def background_user_otp_watcher(chat_id, selected_app, country, num):
    num_clean = str(num).replace("+", "").strip()
    if num_clean in active_user_watchers: return
    active_user_watchers.add(num_clean)
    
    checks = 0
    base_url = str(config['BASE_URL']).strip().rstrip('/')
    url = f"{base_url}/numsuccess/info"
    
    try:
        while checks < 100:
            time.sleep(3)
            checks += 1
            try:
                res = requests.get(url, headers=get_api_headers(), timeout=8).json()
                if res.get("meta", {}).get("code") == 200:
                    otps_list = res.get("data", {}).get("otps", [])
                    for item in otps_list:
                        item_num = str(item.get("number")).replace("+", "").strip()
                        if item_num == num_clean or num_clean.endswith(item_num):
                            found_msg = item.get("otp") or item.get("message")
                            code_match = re.search(r'\b\d{4,8}\b', found_msg)
                            isolated_code = code_match.group(0) if code_match else "N/A"
                            
                            rewarded, new_bal = reward_user_for_otp(chat_id, num, selected_app)
                            full_num = format_full_phone_number(num)
                            icon = get_service_icon(selected_app)
                            
                            # ১. ইনবক্সে পুরো নম্বর ও ওটিপি যাবে
                            dm_text = (f"🎉 **ওটিপি রিসিভ হয়েছে!**\n\n"
                                       f"📱 অ্যাপ: **{icon} {selected_app.upper()}**\n"
                                       f"📞 পুরো নম্বর: `{full_num}`\n"
                                       f"🌐 দেশ: {country}\n\n"
                                       f"🔑 **OTP Code:** `{isolated_code}`\n"
                                       f"💎 নতুন ব্যালেন্স: `{new_bal} BDT`\n\n"
                                       f"💬 মেসেজ:\n`{found_msg}`")
                            
                            markup = types.InlineKeyboardMarkup()
                            markup.row(types.InlineKeyboardButton("📋 Copy OTP", callback_data=f"copyotp_{isolated_code}"),
                                       types.InlineKeyboardButton("📋 Copy Number", callback_data=f"copynum_{full_num}"))
                            safe_send_message(chat_id, dm_text, reply_markup=markup)
                            
                            # ২. ওটিপি গ্রুপে অটো নোটিফিকেশন যাবে
                            group_text = (f"🔥 **USER OTP SUCCESSFUL** 🔥\n\n"
                                          f"📱 Service: {icon} {selected_app.upper()}\n"
                                          f"📞 Number: `{format_group_phone_number(num)}`\n"
                                          f"🔑 Code: `{isolated_code}`")
                            for dest in config["OTP_DESTINATIONS"]:
                                safe_send_message(int(dest), group_text)
                            return
            except: pass
    finally:
        if num_clean in active_user_watchers:
            active_user_watchers.remove(num_clean)

# --- 100% Live Console Stream to Group Only ---
def background_live_sms_monitor():
    global seen_console_hits
    while True:
        try:
            time.sleep(3)
            base_url = str(config['BASE_URL']).strip().rstrip('/')
            url = f"{base_url}/numsuccess/info"
            
            res = requests.get(url, headers=get_api_headers(), timeout=10).json()
            if res.get("meta", {}).get("code") == 200:
                otps_list = res.get("data", {}).get("otps", [])
                
                for item in otps_list:
                    nid = item.get("nid", "")
                    msg_body = item.get("otp", "") or item.get("message", "")
                    num = item.get("number", "")
                    
                    if not msg_body or not str(msg_body).strip(): continue
                    
                    hit_id = hashlib.md5(f"{nid}_{num}_{msg_body[:10]}".encode()).hexdigest()
                    if hit_id in seen_console_hits: continue
                    seen_console_hits.add(hit_id)
                    
                    if len(seen_console_hits) > 2000: seen_console_hits.clear()
                    
                    service_raw = str(item.get("service") or "fb").lower()
                    icon = get_service_icon(service_raw)
                    country_short = get_country_code_short(num)
                    masked_num = format_group_phone_number(num)
                    
                    code_match = re.search(r'\b\d{4,8}\b', msg_body)
                    isolated_code = code_match.group(0) if code_match else "N/A"
                    
                    bot_user = config.get("BOT_USERNAME", "SHS_SMSHUB_bot")
                    
                    # সুনির্দিষ্ট ওটিপি গ্রুপে পাঠানোর মেসেজ ফরম্যাট
                    live_alert = (f"**{service_raw.upper()} SMS Number x TNE**              `Admin` \n"
                                  f"🇨🇫 {country_short} | {icon} | 📱 `{masked_num}` | 🔊 English \n\n"
                                  f"💬 Message:\n`{msg_body}`")
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.row(
                        types.InlineKeyboardButton("📢 Channel", url="https://t.me/SHS_Otp_Channel"),
                        types.InlineKeyboardButton(f"🔑 {isolated_code}", callback_data=f"copyotp_{isolated_code}")
                    )
                    markup.row(types.InlineKeyboardButton("📞 Get Number ↗️", url=f"https://t.me/{bot_user}?start=getnum"))
                    
                    # শুধুমাত্র নির্দিষ্ট ওটিপি গ্রুপে মেসেজ পাঠানো
                    for dest_id in config.get("OTP_DESTINATIONS", []):
                        try: safe_send_message(int(dest_id), live_alert, reply_markup=markup)
                        except: pass
        except Exception as e:
            time.sleep(4)

# --- Non-blocking Broadcast Engine ---
def process_broadcast(message):
    chat_id = message.chat.id
    all_target_users = list(set(all_users).union(db.get_all_user_ids()))
    target_users = [int(uid) for uid in all_target_users if int(uid) > 0 and int(uid) != int(config["ADMIN_ID"])]
    
    if not target_users:
        safe_send_message(chat_id, "❌ ডাটাবেজে কোনো ইউজার পাওয়া যায়নি।")
        return

    def broadcast_thread():
        success, failed = 0, 0
        for uid in target_users:
            try:
                bot.copy_message(chat_id=int(uid), from_chat_id=chat_id, message_id=message.message_id)
                success += 1
                time.sleep(0.03) # ব্রডকাস্ট ব্লকিং এড়াতে ক্ষুদ্র ডিলে
            except:
                failed += 1
        safe_send_message(chat_id, f"✅ **ব্রডকাস্ট সম্পন্ন!**\n\n• সফল: `{success}` জন\n• ব্যর্থ: `{failed}` জন")

    Thread(target=broadcast_thread, daemon=True).start()
    safe_send_message(chat_id, f"🚀 **{len(target_users)} জন ইউজারের কাছে ব্যাকগ্রাউন্ডে ব্রডকাস্ট শুরু হয়েছে...**")

def show_admin_dashboard(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast"))
    safe_send_message(chat_id, "🛠 **Admin Panel**", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "adm_broadcast")
def adm_broad(call):
    msg = bot.send_message(call.message.chat.id, "👉 ব্রডকাস্ট করার মেসেজটি পাঠান:")
    bot.register_next_step_handler(msg, process_broadcast)

@bot.callback_query_handler(func=lambda call: call.data == "close_menu")
def close_in_menu(call):
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data == "back_services")
def back_to_serv(call): send_services_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def check(call):
    if is_subscribed_all(call.from_user.id):
        send_home_keyboard(call.message.chat.id, "✅ ভেরিফিকেশন সফল!")
    else:
        bot.answer_callback_query(call.id, text="❌ গ্রুপে জয়েন করুন!", show_alert=True)

@app.route('/')
def home(): return "SHS OTP HUB Engine is Online!"

def run(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
def keep_alive(): Thread(target=run).start()

if __name__ == "__main__":
    print("🚀 SHS OTP HUB রিয়েল-টাইম ইঞ্জিন স্টার্ট হচ্ছে...")
    keep_alive()
    Thread(target=background_live_sms_monitor, daemon=True).start()
    
    try: bot.delete_webhook(drop_pending_updates=True)
    except: pass
    bot.polling(none_stop=True)
