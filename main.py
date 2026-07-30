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
range_hits_count = collections.defaultdict(int) # ZENEX live hits store
last_announced_range = {}
seen_console_hits = set()
dm_range_cooldowns = {}
last_global_dm_broadcast_time = 0 
active_ranges_global = set() # লাইভ স্টক ট্র্যাকিং গ্লোবাল সেট
active_user_watchers = set() # ট্র্যাকিং একটিভ নম্বরসমূহ

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
                res = requests.get(f"{self.db_url}/users/{uid}.json", timeout=10)
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
        else:
            u_data = self.local_data["users"][uid]
            if "referrer" not in u_data: u_data["referrer"] = None
            if "ref_earnings" not in u_data: u_data["ref_earnings"] = 0.0
            if "ref_count" not in u_data: u_data["ref_count"] = 0
            if "lang" not in u_data: u_data["lang"] = None
            self.local_data["users"][uid] = u_data
        return self.local_data["users"][uid]

    def set_user_language(self, user_id, lang):
        user = self.get_user(user_id)
        user["lang"] = lang
        self.save_user(user_id, user)

    def get_all_user_ids(self):
        uids = set()
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/users.json", timeout=10)
                if res.status_code == 200 and res.json() is not None:
                    for k in res.json().keys():
                        try:
                            uids.add(int(k))
                        except:
                            pass
            except Exception as e:
                print(f"Firebase get_all_user_ids error: {e}")
        
        for k in self.local_data.get("users", {}).keys():
            try:
                uids.add(int(k))
            except:
                pass
        return uids

    def save_user(self, user_id, data):
        uid = str(user_id)
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/users/{uid}.json", json=data, timeout=10)
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
                res = requests.get(f"{self.db_url}/processed_numbers/{num}.json", timeout=10)
                if res.status_code == 200 and res.json() is not None:
                    return res.json() is True
            except Exception as e:
                print(f"Firebase check error: {e}")
        
        return self.local_data["processed_numbers"].get(num) is True

    def mark_number_received_otp(self, number):
        num = str(number).replace("+", "").strip()
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/processed_numbers/{num}.json", json=True, timeout=10)
                return
            except Exception as e:
                print(f"Firebase save number error: {e}")
        
        self.local_data["processed_numbers"][num] = True
        self._save_local()

    def save_withdraw(self, req_id, data):
        r_id = str(req_id)
        if self.db_url:
            try:
                requests.put(f"{self.db_url}/withdraws/{r_id}.json", json=data, timeout=10)
                return
            except Exception as e:
                print(f"Firebase save withdraw error: {e}")
        
        self.local_data["withdraws"][r_id] = data
        self._save_local()

    def get_withdraw(self, req_id):
        r_id = str(req_id)
        if self.db_url:
            try:
                res = requests.get(f"{self.db_url}/withdraws/{r_id}.json", timeout=10)
                if res.status_code == 200 and res.json() is not None:
                    return res.json()
            except Exception as e:
                print(f"Firebase get withdraw error: {e}")
        
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
            "-1003956226642",
            "-1004309875319"
        ],
        "NOTICE": "👋 আমাদের বটে স্বাগতম! ফুল স্পিডে ওটিপি রিসিভ করুন।",
        "CUSTOM_SERVICES": [],
        "SERVICES": {
            "facebook": {"name": "📘 Facebook", "rids": {}},
            "instagram": {"name": "📸 Instagram", "rids": {}},
            "whatsapp": {"name": "💚 WhatsApp", "rids": {}},
            "telegram": {"name": "✈️ Telegram", "rids": {}},
            "imo": {"name": "📱 Imo", "rids": {}},
            "tiktok": {"name": "🎵 TikTok", "rids": {}},
            "discord": {"name": "👾 Discord", "rids": {}}
        }
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
                if "BASE_URL" not in loaded or "zenexnetwork" not in loaded["BASE_URL"]:
                    loaded["BASE_URL"] = default_config["BASE_URL"]
                if "ZENEX_API_KEY" not in loaded:
                    loaded["ZENEX_API_KEY"] = loaded.get("FASTX_API_KEY", default_config["ZENEX_API_KEY"])
                if "PAYMENT_SETTINGS" not in loaded:
                    loaded["PAYMENT_SETTINGS"] = default_config["PAYMENT_SETTINGS"]
                if "BOT_STATUS" not in loaded:
                    loaded["BOT_STATUS"] = "ON"
                if "BOT_OFF_REASON" not in loaded:
                    loaded["BOT_OFF_REASON"] = ""
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

# Database initialization
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
        except Exception as e:
            print(f"Error loading local users file: {e}")
    return uids

def save_users(users_set):
    try:
        clean_list = [int(uid) for uid in users_set if int(uid) > 0]
        with open(USERS_FILE, "w") as f:
            json.dump(clean_list, f, indent=4)
    except Exception as e:
        print(f"Error saving users: {e}")

all_users = load_users()

apihelper.ENABLE_MIDDLEWARE = True 
bot = telebot.TeleBot(config["BOT_TOKEN"])

app = Flask('')
admin_temp_data = {}

# --- Comprehensive Prefix to Country Resolver ---
def get_country_info_by_range(range_val):
    if not range_val:
        return "Global 🌐"
    
    clean_range = str(range_val).strip().upper()
    prefix_range = clean_range.replace("XXX", "").replace("+", "")
    
    prefix_map = {
        "236749": "Central African Republic 🇨🇫",
        "236747": "Central African Republic 🇨🇫",
        "23674": "Central African Republic 🇨🇫",
        "236": "Central African Republic 🇨🇫",
        "231747": "Liberia (Lonestar) 🇱🇷",
        "23174": "Liberia 🇱🇷",
        "231": "Liberia 🇱🇷",
        "224658": "Guinea (Mobile) 🇬🇳",
        "22467": "Guinea 🇬🇳",
        "22465": "Guinea 🇬🇳",
        "2246": "Guinea 🇬🇳",
        "224": "Guinea 🇬🇳",
        "225": "Ivory Coast 🇨🇮",
        "261": "Madagascar 🇲🇬",
        "996": "Kyrgyzstan 🇰🇬",
        "380": "Ukraine 🇺🇦",
        "880": "Bangladesh 🇧🇩",
        "234": "Nigeria 🇳🇬",
        "232": "Sierra Leone 🇸🇱",
        "228": "Togo 🇹🇬",
        "351": "Portugal 🇵🇹",
        "244": "Angola 🇦🇴",
        "242": "Congo 🇨🇬",
        "243": "DR Congo 🇨🇩",
        "229": "Benin 🇧🇯",
        "220": "Gambia 🇬🇲",
        "233": "Ghana 🇬🇭",
        "221": "Senegal 🇸🇳",
        "254": "Kenya 🇰🇪",
        "255": "Tanzania 🇹🇿",
        "256": "Uganda 🇺🇬",
        "263": "Zimbabwe 🇿🇼",
        "260": "Zambia 🇿🇲",
        "251": "Ethiopia 🇪🇹",
        "212": "Morocco 🇲🇦",
        "213": "Algeria 🇩🇿",
        "216": "Tunisia 🇹🇳",
        "218": "Libya 🇱🇾",
        "20": "Egypt 🇪🇬",
        "44": "United Kingdom 🇬🇧",
        "1": "United States 🇺🇸",
        "91": "India 🇮🇳",
        "92": "Pakistan 🇵🇰",
        "62": "Indonesia 🇮🇩",
        "60": "Malaysia 🇲🇾",
        "63": "Philippines 🇵🇭",
        "84": "Vietnam 🇻🇳",
        "7": "Russia/Kazakhstan 🇷🇺",
        "992": "Tajikistan 🇹🇯",
        "382": "Montenegro 🇲🇪",
        "223": "Mali 🇲🇱",
        "98": "Iran 🇮🇷",
        "374": "Armenia 🇦🇲",
        "977": "Nepal 🇳🇵",
        "502": "Guatemala 🇬🇹",
        "972": "Israel 🇮🇱",
        "962": "Jordan 🇯🇴",
        "386": "Slovenia 🇸🇮",
        "998": "Uzbekistan 🇺🇿",
        "40": "Romania 🇷🇴",
        "855": "Cambodia 🇰🇭",
        "266": "Lesotho 🇱🇸",
        "257": "Burundi 🇧🇮",
        "291": "Eritrea 🇪🇷",
        "249": "Sudan 🇸🇩",
        "93": "Afghanistan 🇦🇫",
        "95": "Myanmar 🇲🇲",
        "995": "Georgia 🇬🇪",
        "994": "Azerbaijan 🇦🇿",
        "375": "Belarus 🇧🇾",
        "964": "Iraq 🇮🇶",
        "963": "Syria 🇸🇾",
        "965": "Kuwait 🇰🇼",
        "966": "Saudi Arabia 🇸🇦",
        "967": "Yemen 🇾🇪",
        "968": "Oman 🇴🇲",
        "971": "UAE 🇦🇪",
        "973": "Bahrain 🇧🇭",
        "974": "Qatar 🇶🇦",
        "975": "Bhutan 🇧🇹",
        "976": "Mongolia 🇲🇳",
        "856": "Laos 🇱🇦",
        "66": "Thailand 🇹🇭",
        "852": "Hong Kong 🇭🇰",
        "886": "Taiwan 🇹🇼",
        "82": "South Korea 🇰🇷",
        "81": "Japan 🇯🇵",
        "359": "Bulgaria 🇧🇬",
        "30": "Greece 🇬🇷",
        "31": "Netherlands 🇳🇱",
        "32": "Belgium 🇧🇪",
        "33": "France 🇫🇷",
        "34": "Spain 🇪🇸",
        "36": "Hungary 🇭🇺",
        "39": "Italy 🇮🇹",
        "41": "Switzerland 🇨🇭",
        "43": "Austria 🇦🇹",
        "45": "Denmark 🇩🇰",
        "46": "Sweden 🇸🇪",
        "47": "Norway 🇳🇴",
        "48": "Poland 🇵🇱",
        "49": "Germany 🇩🇪"
    }
    
    sorted_prefixes = sorted(prefix_map.keys(), key=len, reverse=True)
    for prefix in sorted_prefixes:
        if prefix_range.startswith(prefix):
            return prefix_map[prefix]
            
    if len(prefix_range) >= 3:
        p3 = prefix_range[:3]
        if p3 in prefix_map: return prefix_map[p3]
        p2 = prefix_range[:2]
        if p2 in prefix_map: return prefix_map[p2]
        p1 = prefix_range[:1]
        if p1 in prefix_map: return prefix_map[p1]
        return f"Country (+{p3}) 🌐"
    elif len(prefix_range) >= 1:
        p1 = prefix_range[:1]
        if p1 in prefix_map: return prefix_map[p1]
        return f"Country (+{prefix_range}) 🌐"
        
    return "Global 🌐"

def get_country_code_short(range_val):
    info = get_country_info_by_range(range_val)
    if "Central African" in info: return "CF"
    if "Guinea" in info: return "GN"
    if "Liberia" in info: return "LR"
    if "Ivory Coast" in info: return "CI"
    if "Ukraine" in info: return "UA"
    if "Bangladesh" in info: return "BD"
    return "GLOBAL"

# --- Service Icon Resolver ---
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

# --- Zenex API Header Helper ---
def get_api_headers():
    return {
        "mapikey": str(config.get("ZENEX_API_KEY", "")).strip(),
        "Content-Type": "application/json"
    }

# --- Safe Telegram Message Sender ---
def safe_send_message(chat_id, text, reply_markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        try:
            clean_text = text.replace("*", "").replace("`", "").replace("_", "")
            return bot.send_message(chat_id, clean_text, reply_markup=reply_markup)
        except Exception as e2:
            print(f"Failed to send message to {chat_id}: {e2}")
            return None

# --- Phone Number Formatting Rules ---
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

# --- Bot Middleware ---
@bot.middleware_handler(update_types=['message', 'callback_query'])
def auto_track_and_check_status(bot_instance, package):
    try:
        user_id = None
        if hasattr(package, 'from_user') and package.from_user:
            user_id = package.from_user.id
            track_user(user_id)
        elif hasattr(package, 'chat') and package.chat:
            user_id = package.chat.id
            track_user(user_id)
        elif hasattr(package, 'message') and package.message and package.message.chat:
            user_id = package.message.chat.id
            track_user(user_id)
            
        if config.get("BOT_STATUS", "ON") == "OFF":
            if user_id and int(user_id) == int(config["ADMIN_ID"]):
                return
            
            is_private = False
            if hasattr(package, 'chat') and package.chat and package.chat.type == 'private':
                is_private = True
            elif hasattr(package, 'message') and package.message and package.message.chat and package.message.chat.type == 'private':
                is_private = True
                
            if is_private:
                reason = config.get("BOT_OFF_REASON", "রক্ষণাবেক্ষণ কাজের জন্য বট সাময়িকভাবে বন্ধ আছে।")
                text = f"🚫 **বটটি বর্তমানে বন্ধ রয়েছে!**\n\n💬 **কারণ:**\n`{reason}`"
                if isinstance(package, types.CallbackQuery):
                    bot.answer_callback_query(package.id, text=f"❌ বট বর্তমানে বন্ধ আছে! কারণ: {reason}", show_alert=True)
                else:
                    safe_send_message(user_id, text)
                return telebot.handler_backends.CancelUtility()
    except Exception as e:
        print(f"Error in status/tracking middleware: {e}")

@app.route('/')
def home(): return "SHS OTP HUB Engine is Live & Active!"

def run(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
def keep_alive(): Thread(target=run).start()

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
    except:
        pass

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
        
    if service_name and str(service_name).lower().strip() == "whatsapp":
        reward_amount = 0.00
    else:
        reward_amount = 0.20 
        
    db.mark_number_received_otp(clean_num)
    new_bal = db.update_user_balance(user_id, reward_amount)
    
    if reward_amount > 0:
        user_info = db.get_user(user_id)
        referrer_id = user_info.get("referrer")
        if referrer_id:
            ref_commission = round(reward_amount * 0.03, 4)
            db.add_referral_earning(referrer_id, ref_commission)
            try:
                bot.send_message(referrer_id, f"🎉 **রেফারেল বোনাস অর্জিত হয়েছে!**\n\nআপনার রেফারের ইউজার ওটিপি রিসিভ করায় আপনি ৩% কমিশন (`+{ref_commission} BDT`) পেয়েছেন!")
            except:
                pass
            
    return True, new_bal

def get_country_activity_score(platform, rid_val):
    clean_rid = format_rid(rid_val)
    score = range_hits_count.get(clean_rid, 0)
    for k, times in range_hits_tracker.items():
        r_val, plat = k
        if str(plat).lower() == str(platform).lower():
            if format_rid(r_val) == clean_rid:
                score += len(times) * 10
    return score

def get_otp_group_link():
    for grp in config.get("GROUPS_TO_JOIN", []):
        if "OTP" in grp.get("name", "") or "Group" in grp.get("name", "") or "+" in grp.get("link", ""):
            return grp["link"]
    if config.get("GROUPS_TO_JOIN"):
        return config["GROUPS_TO_JOIN"][0]["link"]
    return "https://t.me/+DXdDIm7-rRU4YTQ1"

# --- হোম কিবোর্ড ---
def send_home_keyboard(chat_id, text=None):
    track_user(chat_id)
    u_data = db.get_user(chat_id)
    lang = u_data.get("lang", "bn") or "bn"
    bot_name = config.get("BOT_NAME", "👑 SHS OTP HUB 👑")
    
    if not text:
        if lang == "en":
            text = f"👋 **Welcome to {bot_name}!**\n\n📢 **Notice:** {config.get('NOTICE', 'Welcome!')}"
        else:
            text = f"👋 **{bot_name} এ আপনাকে স্বাগতম!**\n\n📢 **নোটিশ:** {config.get('NOTICE', 'স্বাগতম!')}"
        
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if lang == "en":
        markup.row(types.KeyboardButton("📱 Get Number"))
        markup.row(types.KeyboardButton("👥 Refer & Earn"), types.KeyboardButton("📊 Dashboard"))
        markup.row(types.KeyboardButton("🔑 2FA CODE"), types.KeyboardButton("📊 Live Traffic"))
        markup.row(types.KeyboardButton("🌐 Change Language"), types.KeyboardButton("💬 Support"))
    else:
        markup.row(types.KeyboardButton("📱 Get Number"))
        markup.row(types.KeyboardButton("👥 Reffer & Earn"), types.KeyboardButton("📊 Dashboard"))
        markup.row(types.KeyboardButton("🔑 2FA CODE"), types.KeyboardButton("📊 Live Traffic"))
        markup.row(types.KeyboardButton("🌐 ভাষা পরিবর্তন"), types.KeyboardButton("💬 Support"))
    
    safe_send_message(chat_id, text, reply_markup=markup)

# --- ডায়নামিক ইনলাইন সার্ভিস মেনু (হাই-স্পিড সেরা সার্ভিস উপরে থাকবে) ---
def send_services_menu(chat_id, message_id=None, page=0):
    track_user(chat_id)
    u_data = db.get_user(chat_id)
    lang = u_data.get("lang", "bn") or "bn"
    
    markup = types.InlineKeyboardMarkup()
    services = config.get("SERVICES", {})
    
    active_services = []
    for s_id, s_info in services.items():
        rids = s_info.get("rids", {})
        total_hits = 0
        for r_val in rids.values():
            clean_r = format_rid(r_val)
            total_hits += range_hits_count.get(clean_r, 0)
            total_hits += len([t for (r, p), times in range_hits_tracker.items() if format_rid(r) == clean_r and p == s_id for t in times])
            
        icon = get_service_icon(s_id)
        name = s_info.get("name", s_id.capitalize())
        active_services.append((s_id, name, icon, total_hits, len(rids)))
        
    # সেরা স্টক ও সর্বোচ্চ ওটিপি রেঞ্জ অনুযায়ী সার্ভিস সর্টিং
    active_services = sorted(active_services, key=lambda x: (x[3], x[4]), reverse=True)

    items_per_page = 4
    total_pages = max(1, (len(active_services) + items_per_page - 1) // items_per_page)
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_items = active_services[start_idx:end_idx]
    
    row = []
    for s_id, name, icon, hits, r_count in page_items:
        clean_name = name if icon in name else f"{icon} {name}"
        btn_text = f"{clean_name}"
        row.append(types.InlineKeyboardButton(btn_text, callback_data=f"app_{s_id}"))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
    nav_buttons.append(types.InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    markup.row(*nav_buttons)
    
    close_text = "❌ Close" if lang == "en" else "❌ বন্ধ করুন"
    markup.row(types.InlineKeyboardButton(close_text, callback_data="close_menu"))
    
    text = "🛑 **Select active SERVICE** 🔻" if lang == "en" else "🛑 **সক্রিয় সার্ভিস সিলেক্ট করুন** 🔻"
    if message_id:
        try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode="Markdown")
        except: safe_send_message(chat_id, text, reply_markup=markup)
    else:
        safe_send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("page_"))
def handle_service_pagination(call):
    page_num = int(call.data.split("_")[1])
    send_services_menu(call.message.chat.id, call.message.message_id, page=page_num)

@bot.callback_query_handler(func=lambda call: call.data == "noop")
def handle_noop(call):
    bot.answer_callback_query(call.id)

# --- START COMMAND ---
@bot.message_handler(commands=['start'], chat_types=['private'])
def start_bot(message):
    chat_id = message.chat.id
    command_args = message.text.split()
    referrer_id = None
    if len(command_args) > 1 and command_args[1].isdigit():
        referrer_id = int(command_args[1])
        
    track_user(chat_id, referrer_id)
    
    if not is_subscribed_all(chat_id):
        markup = types.InlineKeyboardMarkup()
        for ch in config.get("CHANNELS_TO_JOIN", []):
            markup.row(types.InlineKeyboardButton(ch["name"], url=ch["link"]))
        for grp in config.get("GROUPS_TO_JOIN", []):
            markup.row(types.InlineKeyboardButton(grp["name"], url=grp["link"]))
        markup.row(types.InlineKeyboardButton("✅ Joined (Check)", callback_data="check_membership"))
        safe_send_message(chat_id, "⚠️ সার্ভিসটি ব্যবহার করতে নিচের সমস্ত চ্যানেল এবং গ্রুপগুলোতে অবশ্যই জয়েন করুন, এরপর 'Joined' বাটনে ক্লিক করুন।", reply_markup=markup)
        return

    u_data = db.get_user(chat_id)
    if not u_data.get("lang"):
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("English 🇬🇧", callback_data="firstlang_en"),
            types.InlineKeyboardButton("বাংলা 🇧🇩", callback_data="firstlang_bn")
        )
        safe_send_message(chat_id, "🌐 **Please Select Your Language / আপনার ভাষা বেছে নিন:**", reply_markup=markup)
    else:
        send_home_keyboard(chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("firstlang_"))
def handle_first_language_choice(call):
    lang_code = call.data.split("_")[1]
    db.set_user_language(call.from_user.id, lang_code)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    msg_text = "✅ Language set to English!" if lang_code == "en" else "✅ ভাষা বাংলা সিলেক্ট করা হয়েছে!"
    bot.answer_callback_query(call.id, text=msg_text)
    send_home_keyboard(call.message.chat.id)

# --- ADMIN PANEL COMMAND ---
@bot.message_handler(commands=['admin'], chat_types=['private'])
def handle_admin_command(message):
    if message.chat.id == int(config["ADMIN_ID"]):
        show_admin_dashboard(message.chat.id)
    else:
        safe_send_message(message.chat.id, "❌ আপনি এই কমান্ডটি ব্যবহারের অনুমতি পাননি।")

@bot.message_handler(func=lambda m: True, chat_types=['private'])
def handle_text(message):
    track_user(message.chat.id)
    u_data = db.get_user(message.chat.id)
    lang = u_data.get("lang", "bn") or "bn"
    
    if not is_subscribed_all(message.chat.id):
        markup = types.InlineKeyboardMarkup()
        for ch in config.get("CHANNELS_TO_JOIN", []):
            markup.row(types.InlineKeyboardButton(ch["name"], url=ch["link"]))
        for grp in config.get("GROUPS_TO_JOIN", []):
            markup.row(types.InlineKeyboardButton(grp["name"], url=grp["link"]))
        markup.row(types.InlineKeyboardButton("✅ Joined (Check)", callback_data="check_membership"))
        safe_send_message(message.chat.id, "❌ আপনি এখনো সমস্ত চ্যানেল বা গ্রুপে জয়েন করেননি!\n\nদয়া করে উপরের সমস্ত চ্যানেল ও গ্রুপগুলোতে জয়েন করুন, এরপর নিচের **Joined** বাটনে ক্লিক করুন।", reply_markup=markup)
        return
    
    text = message.text
    bot_username = config.get("BOT_USERNAME", "SHS_SMSHUB_bot")
    
    if text in ["📱 Get Number", "📲 Get Number"]:
        send_services_menu(message.chat.id)
        
    elif text in ["👥 Reffer & Earn", "👥 Refer & Earn"]:
        ref_count = u_data.get("ref_count", 0)
        ref_earn = u_data.get("ref_earnings", 0.0)
        ref_link = f"https://t.me/{bot_username}?start={message.chat.id}"
        
        if lang == "en":
            msg = (f"👥 **Referral Program (3% Commission)**\n\n"
                   f"🔗 **Your Referral Link:**\n`{ref_link}`\n\n"
                   f"📊 **Stats:**\n"
                   f" ├ 👤 Total Referrals: `{ref_count}` users\n"
                   f" └ 💰 Earnings: `{ref_earn:.4f} BDT`\n\n"
                   f"💡 Get 3% commission on every OTP received by your referrals!")
        else:
            msg = (f"👥 **রেফারেল প্রোগ্রাম (৩% কমিশন)**\n\n"
                   f"🔗 **আপনার ইউনিক রেফার লিঙ্ক:**\n`{ref_link}`\n\n"
                   f"📊 **রেফারেল পরিসংখ্যান:**\n"
                   f" ├ 👤 মোট রেফারেল: `{ref_count}` জন\n"
                   f" └ 💰 রেফার থেকে আয়: `{ref_earn:.4f} BDT`\n\n"
                   f"💡 আপনার রেফারেল লিঙ্কে যুক্ত হওয়া কোনো ইউজার সফল ওটিপি রিসিভ করলেই তার আয়ের **৩% কমিশন** সরাসরি আপনার অ্যাকাউন্টে যুক্ত হবে!")
        safe_send_message(message.chat.id, msg)
        
    elif text in ["📊 Dashboard", "💎 Balance", "💰 Balance"]:
        current_bal = u_data.get("balance", 0.0)
        if lang == "en":
            bal_text = (f"📊 **Your Dashboard & Balance**\n\n"
                        f"• User ID: `{message.chat.id}`\n"
                        f"• Balance: `{current_bal} BDT`\n"
                        f"• Per OTP Income: `0.20 BDT`\n\n"
                        f"{config.get('BALANCE_TEXT', '')}")
        else:
            bal_text = (f"📊 **আপনার ড্যাশবোর্ড ও ব্যালেন্স**\n\n"
                        f"• ইউজার আইডি: `{message.chat.id}`\n"
                        f"• বর্তমান ব্যালেন্স: `{current_bal} BDT`\n"
                        f"• প্রতি ওটিপিতে আয়: `0.20 BDT`\n\n"
                        f"{config.get('BALANCE_TEXT', '')}")
        
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("📉 Withdraw", callback_data="btn_withdraw_init"))
        safe_send_message(message.chat.id, bal_text, reply_markup=markup)
        
    elif text in ["🌐 Change Language", "🌐 ভাষা পরিবর্তন"]:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("English 🇬🇧", callback_data="setlang_en"),
            types.InlineKeyboardButton("বাংলা 🇧🇩", callback_data="setlang_bn")
        )
        safe_send_message(message.chat.id, "🌐 Select your preferred language / আপনার ভাষা বেছে নিন:", reply_markup=markup)
        
    elif text == "📉 Withdraw":
        initiate_withdraw(message.chat.id)
    elif text == "📊 Live Traffic":
        fetch_live_traffic(message.chat.id)
    elif text == "🔑 2FA CODE":
        safe_send_message(message.chat.id, "🔐 **2FA Code Generator:**\nSend your secret 2FA key below:")
    elif text == "💬 Support":
        markup = types.InlineKeyboardMarkup()
        for grp in config.get("GROUPS_TO_JOIN", []):
            markup.add(types.InlineKeyboardButton(f"💬 {grp['name']}", url=grp['link']))
        markup.add(types.InlineKeyboardButton("📞 Admin Support", url=f"https://t.me/{config.get('DEV_USERNAME', 'Saku_143')}"))
        safe_send_message(message.chat.id, "💬 **Support & Help:**", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("setlang_"))
def handle_set_language(call):
    lang_code = call.data.split("_")[1]
    db.set_user_language(call.from_user.id, lang_code)
    msg_text = "✅ Language updated to English!" if lang_code == "en" else "✅ ভাষা সফলভাবে বাংলায় পরিবর্তন করা হয়েছে!"
    bot.answer_callback_query(call.id, text=msg_text, show_alert=True)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    send_home_keyboard(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_menu")
def close_in_menu(call):
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data == "btn_withdraw_init")
def btn_withdraw_trigger(call):
    initiate_withdraw(call.message.chat.id)

def initiate_withdraw(chat_id):
    user_data = db.get_user(chat_id)
    bal = user_data.get("balance", 0.0)
    msg = safe_send_message(chat_id, f"📉 **উইথড্র করার পরিমাণ (BDT) লিখুন:**\n\n• আপনার বর্তমান ব্যালেন্স: `{bal} BDT`\n• মিনিমাম উইথড্র: `50 BDT`")
    if msg: bot.register_next_step_handler(msg, process_withdraw_amount)

def render_payment_toggle(chat_id, message_id=None):
    settings = config.get("PAYMENT_SETTINGS", {"bkash": True, "nagad": True, "binance": True})
    markup = types.InlineKeyboardMarkup()
    
    b_status = "✅ ON" if settings.get("bkash", True) else "❌ OFF"
    n_status = "✅ ON" if settings.get("nagad", True) else "❌ OFF"
    bi_status = "✅ ON" if settings.get("binance", True) else "❌ OFF"
    
    markup.add(types.InlineKeyboardButton(f"bKash: {b_status}", callback_data="toggle_pay_bkash"))
    markup.add(types.InlineKeyboardButton(f"Nagad: {n_status}", callback_data="toggle_pay_nagad"))
    markup.add(types.InlineKeyboardButton(f"Binance: {bi_status}", callback_data="toggle_pay_binance"))
    markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_back"))
    
    text = "⚙️ **উইথড্র গেটওয়ে অন/অফ করুন:**"
    if message_id:
        try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode="Markdown")
        except: safe_send_message(chat_id, text, reply_markup=markup)
    else:
        safe_send_message(chat_id, text, reply_markup=markup)

def fetch_live_traffic(chat_id):
    msg = "📊 **SHS OTP HUB Real-Time Active Traffic:**\n\n"
    services = config.get("SERVICES", {})
    active_count = 0
    
    for s_id in list(services.keys()):
        s_info = services[s_id]
        rids = s_info.get("rids", {})
        active_list = []
        
        for country, r_val in rids.items():
            clean_rid = format_rid(r_val)
            hits = range_hits_count.get(clean_rid, 0)
            score = get_country_activity_score(s_id, r_val)
            if hits > 0 or score > 0 or clean_rid in active_ranges_global:
                pct = min(99, 85 + min(hits, 14))
                active_list.append((country, r_val, pct, hits))
        
        if active_list:
            active_count += 1
            icon = get_service_icon(s_id)
            msg += f"*{icon} {s_info.get('name', s_id.upper())}*:\n"
            active_list = sorted(active_list, key=lambda x: (x[3], x[2]), reverse=True)[:5]
            for country, r_val, pct, hits in active_list:
                msg += f" ├ {country} (Range: `{r_val}`) ➔ ⚡ **{pct}% Active** ({hits} Hits)\n"
            msg += "\n"
                
    if active_count == 0:
        msg += "⚠️ বর্তমানে কোনো সচল ট্রাফিক রেঞ্জ পাওয়া যায়নি। অনুগ্রহ করে একটু পর চেষ্টা করুন।"
    else:
        msg += "💡 **টিপস:** ওটিপি দ্রুত পেতে তালিকায় বেশি হিটস থাকা দেশগুলো সিলেক্ট করুন।"
        
    safe_send_message(chat_id, msg)

def show_admin_dashboard(chat_id):
    markup = types.InlineKeyboardMarkup()
    
    bot_status_label = "🤖 Bot Status: ✅ ON" if config.get("BOT_STATUS", "ON") == "ON" else "🤖 Bot Status: ❌ OFF"
    markup.row(types.InlineKeyboardButton(bot_status_label, callback_data="adm_toggle_bot_status"))
    
    markup.row(types.InlineKeyboardButton("💎 Edit User Balance", callback_data="adm_edituserbal"))
    markup.row(types.InlineKeyboardButton("➕ Add Range ID", callback_data="adm_addrid"),
               types.InlineKeyboardButton("✨ Add Custom App", callback_data="adm_addcustom"))
    markup.row(types.InlineKeyboardButton("🗑 Delete Range ID", callback_data="adm_delrid"))
    markup.row(types.InlineKeyboardButton("⚙️ Manage Payments", callback_data="adm_togglepay"),
               types.InlineKeyboardButton("📢 Manage Channels/Groups", callback_data="adm_channels"))
    markup.row(types.InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast"))
    markup.row(types.InlineKeyboardButton("✍️ Set Notice", callback_data="adm_setnotice"),
               types.InlineKeyboardButton("🤖 Set Bot Name", callback_data="adm_setname"))
    markup.row(types.InlineKeyboardButton("💎 Edit Balance Text", callback_data="adm_setbal"),
               types.InlineKeyboardButton("📉 Edit Withdraw Text", callback_data="adm_setwith"))
    markup.row(types.InlineKeyboardButton("🔗 Set Bot Username", callback_data="adm_setbotuser"),
               types.InlineKeyboardButton("🔗 Set Firebase DB URL", callback_data="adm_setfirebase"))
    markup.row(types.InlineKeyboardButton("🔑 Update Zenex API Key", callback_data="adm_setkey"))
    
    bot_title = config.get("BOT_NAME", "👑 SHS OTP HUB 👑")
    bot_user = config.get("BOT_USERNAME", "SHS_SMSHUB_bot")
    
    all_db_uids = list(set(all_users).union(db.get_all_user_ids()))
    text = (f"🛠 **Admin Control Panel**\n\n"
            f"• Bot Name: `{bot_title}`\n"
            f"• Bot Username: `@{bot_user}`\n"
            f"• Total Active Users: `{len(all_db_uids)}`\n"
            f"• মোট সচল অ্যাপ: {len(config['SERVICES'])}\n"
            f"• বর্তমান নোটিশ: {config.get('NOTICE', 'নেই')}\n"
            f"• বট স্ট্যাটাস: `{config.get('BOT_STATUS', 'ON')}`\n"
            f"• অফ করার কারণ: `{config.get('BOT_OFF_REASON', 'নেই')}`")
    safe_send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_"))
def handle_admin_callbacks(call):
    if call.message.chat.id != int(config["ADMIN_ID"]): return
    data = call.data
    chat_id = call.message.chat.id
    
    if data == "adm_toggle_bot_status":
        current_status = config.get("BOT_STATUS", "ON")
        if current_status == "ON":
            msg = bot.send_message(chat_id, "✍️ বটটি অফ করার কারণটি লিখে পাঠান:")
            bot.register_next_step_handler(msg, process_bot_turn_off_reason)
        else:
            config["BOT_STATUS"] = "ON"
            config["BOT_OFF_REASON"] = ""
            save_config(config)
            bot.answer_callback_query(call.id, text="✅ বট সফলভাবে অন করা হয়েছে!", show_alert=True)
            try: bot.delete_message(chat_id, call.message.message_id)
            except: pass
            show_admin_dashboard(chat_id)
            
    elif data == "adm_edituserbal":
        msg = bot.send_message(chat_id, "👤 **ইউজারের ব্যালেন্স পরিবর্তন করতে তার Telegram ID দিন:**")
        bot.register_next_step_handler(msg, process_admin_get_user_id)
            
    elif data == "adm_addrid":
        markup = types.InlineKeyboardMarkup()
        for s_id, s_info in config["SERVICES"].items():
            markup.add(types.InlineKeyboardButton(f"➕ {s_info['name']} - এ রেঞ্জ যোগ করুন", callback_data=f"addrid_target_{s_id}"))
        markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_back"))
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, 
                              text="📌 **রেঞ্জ আইডি যুক্তকরণ:**\nনিচের কোন অ্যাপে নতুন রেঞ্জ আইডি যোগ করতে চান সিলেক্ট করুন:", 
                              reply_markup=markup, parse_mode="Markdown")
        
    elif data == "adm_addcustom":
        msg = bot.send_message(chat_id, "✍️ নতুন কাস্টম অ্যাপের নাম লিখুন (যেমন: `uber` বা `netflix`):")
        bot.register_next_step_handler(msg, wizard_get_custom_app_name)
        
    elif data == "adm_delrid":
        markup = types.InlineKeyboardMarkup()
        for s_id, s_info in config["SERVICES"].items():
            markup.add(types.InlineKeyboardButton(f"❌ ডিলিট রেঞ্জ: {s_info['name']}", callback_data=f"delapp_{s_id}"))
        markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_back"))
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text="🗑 **রেঞ্জ আইডি মুছে ফেলা:**\nকোন অ্যাপের রেঞ্জ ডিলিট করতে চান সিলেক্ট করুন:", reply_markup=markup, parse_mode="Markdown")

    elif data == "adm_channels":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ নতুন চ্যানেল/গ্রুপ অ্যাড করুন", callback_data="ch_add"))
        markup.add(types.InlineKeyboardButton("🗑 চ্যানেল/গ্রুপ রিমুভ করুন", callback_data="ch_remove"))
        markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_back"))
        
        c_list = "\n".join([f"📢 {c['name']} (`{c['id']}`)" for c in config["CHANNELS_TO_JOIN"]])
        g_list = "\n".join([f"👥 {g['name']} (`{g['id']}`)" for g in config["GROUPS_TO_JOIN"]])
        text = f"📢 **চ্যানেল ও গ্রুপ ম্যানেজমেন্ট**\n\n**বর্তমান চ্যানেলসমূহ:**\n{c_list}\n\n**বর্তমান গ্রুপসমূহ:**\n{g_list}"
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")

    elif data == "adm_togglepay":
        render_payment_toggle(chat_id, call.message.message_id)

    elif data == "adm_broadcast":
        msg = bot.send_message(chat_id, "📢 আপনি সকল ইউজারদের কাছে যে মেসেজটি পাঠাতে চান তা লিখে বা ফরোয়ার্ড করে পাঠান:")
        bot.register_next_step_handler(msg, process_broadcast)
    elif data == "adm_setnotice":
        msg = bot.send_message(chat_id, "👉 ইউজারদের জন্য নতুন নোটিশটি লিখে পাঠান:")
        bot.register_next_step_handler(msg, save_notice)
    elif data == "adm_setname":
        msg = bot.send_message(chat_id, "👉 নতুন বটের নাম লিখে পাঠান:")
        bot.register_next_step_handler(msg, save_bot_name)
    elif data == "adm_setbal":
        msg = bot.send_message(chat_id, "👉 নতুন Balance মেসেজটি লিখে পাঠান:")
        bot.register_next_step_handler(msg, save_balance_text)
    elif data == "adm_setwith":
        msg = bot.send_message(chat_id, "👉 নতুন Withdraw মেসেজটি লিখে পাঠান:")
        bot.register_next_step_handler(msg, save_withdraw_text)
    elif data == "adm_setbotuser":
        msg = bot.send_message(chat_id, "👉 বটের ইউজারনেম লিখুন (@ ছাড়া):")
        bot.register_next_step_handler(msg, save_bot_username)
    elif data == "adm_setfirebase":
        msg = bot.send_message(chat_id, "👉 আপনার Firebase Database URL দিন:")
        bot.register_next_step_handler(msg, save_firebase_url)
    elif data == "adm_setkey":
        msg = bot.send_message(chat_id, "👉 আপনার নতুন Zenex API Key টি পাঠান:")
        bot.register_next_step_handler(msg, save_api_key)
    elif data == "adm_back":
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass
        show_admin_dashboard(chat_id)

def process_admin_get_user_id(message):
    chat_id = message.chat.id
    try:
        target_uid = int(message.text.strip())
        user_data = db.get_user(target_uid)
        current_bal = user_data.get("balance", 0.0)
        
        msg = bot.send_message(chat_id, 
                               f"👤 **ইউজার আইডি:** `{target_uid}`\n"
                               f"💎 **বর্তমান ব্যালেন্স:** `{current_bal} BDT`\n\n"
                               f"ব্যালেন্স পরিবর্তন করার জন্য পরিমাণটি লিখুন:")
        bot.register_next_step_handler(msg, process_admin_save_user_balance, target_uid)
    except ValueError:
        bot.send_message(chat_id, "❌ অনুগ্রহ করে একটি সঠিক সংখ্যামূলক ইউজার আইডি দিন।")
        show_admin_dashboard(chat_id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ সমস্যা হয়েছে: {e}")
        show_admin_dashboard(chat_id)

def process_admin_save_user_balance(message, target_uid):
    chat_id = message.chat.id
    try:
        amount_diff = float(message.text.strip())
        user_data = db.get_user(target_uid)
        old_bal = float(user_data.get("balance", 0.0))
        
        new_bal = db.update_user_balance(target_uid, amount_diff)
        
        bot.send_message(chat_id, f"✅ **ব্যালেন্স আপডেট সফল!**\n\n• ইউজার: `{target_uid}`\n• পূর্বের ব্যালেন্স: `{old_bal} BDT`\n• নতুন ব্যালেন্স: `{new_bal} BDT`")
        
        try:
            bot.send_message(target_uid, 
                             f"💎 **আপনার ব্যালেন্স এডমিন কর্তৃক আপডেট করা হয়েছে!**\n\n"
                             f"• পূর্বের ব্যালেন্স: `{old_bal} BDT`\n"
                             f"• নতুন ব্যালেন্স: `{new_bal} BDT`", 
                             parse_mode="Markdown")
        except:
            pass
    except ValueError:
        bot.send_message(chat_id, "❌ অনুগ্রহ করে একটি সঠিক সংখ্যা দিন (যেমন: 10 বা -5)।")
    except Exception as e:
        bot.send_message(chat_id, f"❌ পরিবর্তন ব্যর্থ হয়েছে: {e}")
    show_admin_dashboard(chat_id)

def process_bot_turn_off_reason(message):
    chat_id = message.chat.id
    reason_text = message.text.strip()
    if not reason_text:
        reason_text = "রক্ষণাবেক্ষণ কাজের জন্য বট সাময়িকভাবে বন্ধ রয়েছে।"
    config["BOT_STATUS"] = "OFF"
    config["BOT_OFF_REASON"] = reason_text
    save_config(config)
    bot.send_message(chat_id, f"❌ বট অফ (OFF) করা হয়েছে!\n💬 কারণ: {reason_text}")
    show_admin_dashboard(chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_pay_"))
def handle_payment_toggle(call):
    if call.message.chat.id != int(config["ADMIN_ID"]): return
    method = call.data.replace("toggle_pay_", "")
    if "PAYMENT_SETTINGS" not in config:
        config["PAYMENT_SETTINGS"] = {"bkash": True, "nagad": True, "binance": True}
    config["PAYMENT_SETTINGS"][method] = not config["PAYMENT_SETTINGS"].get(method, True)
    save_config(config)
    bot.answer_callback_query(call.id, text=f"✅ {method.upper()} পরিবর্তন করা হয়েছে!")
    render_payment_toggle(call.message.chat.id, call.message.message_id)

def save_firebase_url(message):
    config["FIREBASE_DB_URL"] = message.text.strip()
    save_config(config)
    global db
    db = DatabaseManager(config["FIREBASE_DB_URL"])
    bot.send_message(message.chat.id, "✅ Firebase Database URL সফলভাবে সংযুক্ত করা হয়েছে!")
    show_admin_dashboard(message.chat.id)

def process_broadcast(message):
    chat_id = message.chat.id
    success = 0
    failed = 0
    
    all_target_users = list(set(all_users).union(db.get_all_user_ids()))
    target_users = [int(uid) for uid in all_target_users if int(uid) > 0 and int(uid) != int(config["ADMIN_ID"])]
    
    if not target_users:
        bot.send_message(chat_id, "❌ **ব্রডকাস্ট ব্যর্থ!**\n\nডাটাবেজে কোনো ইউজার পাওয়া যায়নি।", parse_mode="Markdown")
        return
        
    status_msg = bot.send_message(chat_id, f"🚀 **{len(target_users)} জন ইউজারের কাছে ব্রডকাস্ট শুরু হয়েছে...**")
    
    for uid in target_users:
        try:
            bot.copy_message(chat_id=int(uid), from_chat_id=chat_id, message_id=message.message_id)
            success += 1
            time.sleep(0.04)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = e.result_json.get('parameters', {}).get('retry_after', 3)
                time.sleep(retry_after + 1)
                try:
                    bot.copy_message(chat_id=int(uid), from_chat_id=chat_id, message_id=message.message_id)
                    success += 1
                except:
                    failed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            
    bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, 
                          text=f"✅ **ব্রডকাস্ট সম্পন্ন!**\n\n• সফল: `{success}` জন\n• ব্যর্থ/ব্লক: `{failed}` জন", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("addrid_target_"))
def wizard_add_rid_target(call):
    chat_id = call.message.chat.id
    app_target = call.data.replace("addrid_target_", "")
    admin_temp_data[chat_id] = {"app": app_target}
    msg = bot.send_message(chat_id, f"🌍 আপনি **{app_target.upper()}** সিলেক্ট করেছেন।\n\nএখন দেশের কোড এবং রেঞ্জ আইডি এভাবে পাঠান:\n*উদাহরণ:* `US 447384XXX`")
    bot.register_next_step_handler(msg, wizard_save_rid)

def wizard_get_custom_app_name(message):
    chat_id = message.chat.id
    app_name = message.text.strip().lower()
    admin_temp_data[chat_id] = {"app": app_name}
    
    if "CUSTOM_SERVICES" not in config:
        config["CUSTOM_SERVICES"] = []
    if app_name not in config["CUSTOM_SERVICES"]:
        config["CUSTOM_SERVICES"].append(app_name)
        
    if app_name not in config["SERVICES"]:
        config["SERVICES"][app_name] = {"name": f"✨ {app_name.capitalize()}", "rids": {}}
        save_config(config)
    
    bot.send_message(chat_id, f"🎉 কাস্টম সার্ভিস **{app_name.upper()}** যুক্ত হয়েছে!")
    show_admin_dashboard(chat_id)

def wizard_save_rid(message):
    chat_id = message.chat.id
    try:
        parts = message.text.strip().split()
        country = parts[0].upper()
        rid_val = parts[1]
        app_id = admin_temp_data.get(chat_id, {}).get("app")
        if not app_id:
            bot.send_message(chat_id, "❌ সেশন মেয়াদোত্তীর্ণ। আবার চেষ্টা করুন।")
            return
        if app_id not in config["SERVICES"]:
            config["SERVICES"][app_id] = {"name": f"✨ {app_id.capitalize()}", "rids": {}}
        config["SERVICES"][app_id]["rids"][country] = format_rid(rid_val)
        save_config(config)
        bot.send_message(chat_id, f"🎉 সফলভাবে সেভ হয়েছে!\nApp: {app_id.upper()}\nCountry: {country}\nRange ID: {format_rid(rid_val)}")
    except:
        bot.send_message(chat_id, "❌ ফরম্যাট ভুল হয়েছে! (যেমন: `US 447384XXX`)")
    show_admin_dashboard(chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delapp_"))
def wizard_del_app(call):
    chat_id = call.message.chat.id
    app_id = call.data.split("_")[1]
    if app_id not in config["SERVICES"]: return
    markup = types.InlineKeyboardMarkup()
    rids = config["SERVICES"][app_id]["rids"]
    for country in rids.keys():
        markup.add(types.InlineKeyboardButton(f"❌ ডিলিট দেশ: {country} (RID: {rids[country]})", callback_data=f"delsel_{app_id}_{country}"))
    markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_addrid"))
    bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=f"🗑 **{app_id.upper()}** এর কোন দেশের রেঞ্জটি ডিলিট করতে চান?", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delsel_"))
def wizard_execute_delete(call):
    chat_id = call.message.chat.id
    _, app_id, country = call.data.split("_")
    if app_id in config["SERVICES"] and country in config["SERVICES"][app_id]["rids"]:
        del config["SERVICES"][app_id]["rids"][country]
        save_config(config)
        bot.answer_callback_query(call.id, text=f"✅ {country} এর রেঞ্জ ডিলিট করা হয়েছে!", show_alert=True)
    show_admin_dashboard(chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "ch_add")
def wizard_add_channel(call):
    msg = bot.send_message(call.message.chat.id, "👉 নতুন চ্যানেল/গ্রুপ এভাবে পাঠান:\n`channel -100123456789 https://t.me/mychannel MyChannel`")
    bot.register_next_step_handler(msg, process_save_channel_group)

def process_save_channel_group(message):
    try:
        parts = message.text.strip().split(maxsplit=3)
        ch_type = parts[0].lower()
        ch_id = parts[1]
        ch_link = parts[2]
        ch_name = parts[3]
        item = {"id": ch_id, "link": ch_link, "name": ch_name}
        if ch_type == "channel":
            config["CHANNELS_TO_JOIN"].append(item)
            if ch_id not in config["OTP_DESTINATIONS"]: config["OTP_DESTINATIONS"].append(ch_id)
        elif ch_type == "group":
            config["GROUPS_TO_JOIN"].append(item)
            if ch_id not in config["OTP_DESTINATIONS"]: config["OTP_DESTINATIONS"].append(ch_id)
        save_config(config)
        bot.send_message(message.chat.id, "✅ চ্যানেল/গ্রুপ যুক্ত করা হয়েছে!")
    except:
        bot.send_message(message.chat.id, "❌ ফরম্যাট সঠিক নয়!")
    show_admin_dashboard(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "ch_remove")
def wizard_remove_channel(call):
    markup = types.InlineKeyboardMarkup()
    for idx, c in enumerate(config["CHANNELS_TO_JOIN"]):
        markup.add(types.InlineKeyboardButton(f"❌ চ্যানেল ডিলিট: {c['name']}", callback_data=f"delch_c_{idx}"))
    for idx, g in enumerate(config["GROUPS_TO_JOIN"]):
        markup.add(types.InlineKeyboardButton(f"❌ গ্রুপ ডিলিট: {g['name']}", callback_data=f"delch_g_{idx}"))
    markup.add(types.InlineKeyboardButton("⬅️ ব্যাক", callback_data="adm_channels"))
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="🗑 যে চ্যানেল বা গ্রুপটি রিমুভ করতে চান তাতে ক্লিক করুন:", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delch_"))
def execute_remove_channel(call):
    _, target_type, idx_str = call.data.split("_")
    idx = int(idx_str)
    if target_type == "c" and len(config["CHANNELS_TO_JOIN"]) > 1:
        removed = config["CHANNELS_TO_JOIN"].pop(idx)
        if removed["id"] in config["OTP_DESTINATIONS"]: config["OTP_DESTINATIONS"].remove(removed["id"])
        save_config(config)
        bot.answer_callback_query(call.id, text="✅ চ্যানেল রিমুভ হয়েছে!", show_alert=True)
    elif target_type == "g":
        removed = config["GROUPS_TO_JOIN"].pop(idx)
        if removed["id"] in config["OTP_DESTINATIONS"]: config["OTP_DESTINATIONS"].remove(removed["id"])
        save_config(config)
        bot.answer_callback_query(call.id, text="✅ গ্রুপ রিমুভ হয়েছে!", show_alert=True)
    show_admin_dashboard(call.message.chat.id)

def save_notice(message):
    config["NOTICE"] = message.text.strip()
    save_config(config)
    bot.send_message(message.chat.id, "✅ নোটিশ আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

def save_bot_name(message):
    config["BOT_NAME"] = message.text.strip()
    save_config(config)
    bot.send_message(message.chat.id, "✅ বটের নাম আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

def save_balance_text(message):
    config["BALANCE_TEXT"] = message.text.strip()
    save_config(config)
    bot.send_message(message.chat.id, "✅ ব্যালেন্স টেক্সট আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

def save_withdraw_text(message):
    config["WITHDRAW_TEXT"] = message.text.strip()
    save_config(config)
    bot.send_message(message.chat.id, "✅ ওটিপি টেক্সট আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

def save_bot_username(message):
    config["BOT_USERNAME"] = message.text.strip().replace("@", "")
    save_config(config)
    bot.send_message(message.chat.id, "✅ বটের ইউজারনেম আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

def save_api_key(message):
    config["ZENEX_API_KEY"] = message.text.strip()
    save_config(config)
    bot.send_message(message.chat.id, "✅ Zenex API Key আপডেট হয়েছে।")
    show_admin_dashboard(message.chat.id)

# --- ডাইনামিক কান্ট্রি শো করা ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("app_"))
def show_countries(call):
    selected_app = call.data.split("_")[1]
    services = config.get("SERVICES", {})
    if selected_app not in services: return
    markup = types.InlineKeyboardMarkup()
    rids = services[selected_app]["rids"]
    
    available_countries = list(rids.keys())
    
    available_countries = sorted(
        available_countries,
        key=lambda c: (range_hits_count.get(format_rid(rids[c]), 0), get_country_activity_score(selected_app, rids[c])),
        reverse=True
    )
    
    row = []
    for country in available_countries:
        score = get_country_activity_score(selected_app, rids[country])
        hits = range_hits_count.get(format_rid(rids[country]), 0)
        badge = "🔥 " if (hits > 0 or score > 0) else "⭐ "
        callback_data = f"c_{country}_{selected_app}"
        btn_text = f"{badge}{country}"
        
        row.append(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row: markup.row(*row)
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_services"))
    
    icon = get_service_icon(selected_app)
    text = f"🌐 **{icon} {selected_app.upper()} - Select Country:**"
    try: bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except: safe_send_message(call.message.chat.id, text, reply_markup=markup)

# --- GET NUMBER ENGINE (100% REAL LIVE RANGES ONLY) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("c_"))
def request_number(call):
    _, country, selected_app = call.data.split("_")
    rid = config["SERVICES"][selected_app]["rids"].get(country)
    
    if not rid:
        bot.answer_callback_query(call.id, text="⚠️ বর্তমানে এই দেশের কোনো রেঞ্জ খুঁজে পাওয়া যায়নি!", show_alert=True)
        return
        
    formatted_rid = format_rid(rid)
    base_url = str(config['BASE_URL']).strip().rstrip('/')
    url = f"{base_url}/getnum"
    
    payload = {
        "range": str(formatted_rid),
        "is_national": False,
        "remove_plus": False
    }
    
    try:
        response = requests.post(url, json=payload, headers=get_api_headers(), timeout=20)
        
        if response.status_code == 400 or response.status_code != 200:
            bot.answer_callback_query(call.id, text="⚠️ এই সচল রেঞ্জের স্টক এই মুহূর্তে খালি! একটু পর চেষ্টা করুন।", show_alert=True)
            return
            
        res = response.json()
        meta = res.get("meta", {})
        
        if meta.get("code") == 200 or meta.get("status") == "success":
            data_obj = res.get("data", {})
            num_raw = data_obj.get("full_number") or data_obj.get("number") or data_obj.get("copy")
            
            full_num = format_full_phone_number(num_raw)
            icon = get_service_icon(selected_app)
            
            msg = (f"⚡ **Number Provisioned!**\n\n"
                   f"📱 Service ➔ **{icon} {selected_app.upper()}**\n"
                   f"🌐 Country ➔ **{country}**\n"
                   f"📡 Operator ➔ `{data_obj.get('operator', 'Global')}`\n\n"
                   f"📞 Number: `{full_num}`\n\n"
                   f"⏳ Status: **Waiting For OTP (Auto-Detecting...)**\n"
                   f"⏰ Validity ➔ 15 minutes\n"
                   f"⚡ ওটিপি আসামাত্রই বট আপনাকে স্বয়ংক্রিয়ভাবে নোটিফিকেশন পাঠাবে!")
            
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("🔄 Fetch Code (Manual)", callback_data=f"fetch_{selected_app}_{country}_{num_raw}"),
                types.InlineKeyboardButton("🔄 Change Number", callback_data=f"c_{country}_{selected_app}")
            )
            markup.row(types.InlineKeyboardButton("📋 Copy Number", callback_data=f"copynum_{full_num}"))
            markup.row(types.InlineKeyboardButton("🔗 View OTP Group", url=get_otp_group_link()))
            
            try: bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=markup, parse_mode="Markdown")
            except: safe_send_message(call.message.chat.id, msg, reply_markup=markup)
            
            Thread(target=background_user_otp_watcher, args=(call.message.chat.id, call.message.message_id, selected_app, country, num_raw), daemon=True).start()
        else:
            bot.answer_callback_query(call.id, text=f"❌ স্টক খালি: {res.get('message', 'অন্য একটি দেশ বেছে নিন')}", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, text="⚠️ কানেকশন সমস্যা! আবার চেষ্টা করুন।", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("copynum_"))
def copy_number_alert(call):
    num = call.data.split("_")[1]
    bot.answer_callback_query(call.id, text=f"📞 Number: {num}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("copyotp_"))
def copy_otp_alert(call):
    code = call.data.split("_")[1]
    bot.answer_callback_query(call.id, text=f"🔑 OTP Code: {code}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("fetch_"))
def manual_fetch(call):
    data_parts = call.data.split("_")
    selected_app = data_parts[1]
    country = data_parts[2]
    num = data_parts[3]
    
    bot.answer_callback_query(call.id, text="🔄 Fetching OTP... Please wait ⏳", show_alert=False)
    
    found = check_and_send_otp_manual(call.message.chat.id, selected_app, country, num, call.message.message_id)
    if not found:
        bot.answer_callback_query(call.id, text="⚠️ ওটিপি এখনো আসেনি! অটোমেটিক ট্রাই করা হচ্ছে, সাথেই থাকুন...", show_alert=True)

def check_and_send_otp_manual(chat_id, selected_app, country, num, message_id=None):
    base_url = str(config['BASE_URL']).strip().rstrip('/')
    url = f"{base_url}/numsuccess/info"
    
    try:
        res = requests.get(url, headers=get_api_headers(), timeout=15).json()
        meta = res.get("meta", {})
        if meta.get("code") == 200 or meta.get("status") == "success":
            text_otps_list = res.get("data", {}).get("otps", [])
            clean_num = str(num).replace("+", "").strip()
            
            found_msg = None
            for item in text_otps_list:
                item_num = str(item.get("number")).replace("+", "").strip()
                if item_num == clean_num or clean_num.endswith(item_num) or item_num.endswith(clean_num):
                    found_msg = item.get("otp") or item.get("message")
                    break
            
            if found_msg:
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                bot_title = config.get("BOT_NAME", "👑 SHS OTP HUB 👑")
                
                code_match = re.search(r'\b\d{4,8}\b', found_msg)
                isolated_code = code_match.group(0) if code_match else found_msg[:10]
                
                rewarded, new_bal = reward_user_for_otp(chat_id, num, selected_app)
                
                if str(selected_app).lower().strip() == "whatsapp":
                    reward_text = f"💎 **Balance:** `{new_bal} BDT`"
                else:
                    reward_text = f"💎 **Earned:** +0.20 BDT (New Bal: `{new_bal} BDT`)" if rewarded else "ℹ️ *এই নম্বরের রিওয়ার্ড ইতোমধ্যে যোগ হয়েছে।*"
                
                full_num = format_full_phone_number(num)
                icon = get_service_icon(selected_app)
                
                card_update_text = (f"✅ **OTP Received Successfully!**\n\n"
                                    f"📱 Service ➔ **{icon} {selected_app.upper()}**\n"
                                    f"🌐 Country ➔ **{country}**\n"
                                    f"📞 Number: `{full_num}`\n\n"
                                    f"🔑 **OTP Code:** `{isolated_code}`\n"
                                    f"⚡ status: **Done**")
                
                card_markup = types.InlineKeyboardMarkup()
                card_markup.row(types.InlineKeyboardButton("📋 Copy OTP", callback_data=f"copyotp_{isolated_code}"),
                                types.InlineKeyboardButton("📞 Copy Number", callback_data=f"copynum_{full_num}"))
                card_markup.row(types.InlineKeyboardButton("🔗 View OTP Group", url=get_otp_group_link()))

                if message_id:
                    try: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=card_update_text, reply_markup=card_markup, parse_mode="Markdown")
                    except: pass

                user_alert_text = (f"🎉 **NEW OTP RECEIVED!** 🎉\n\n"
                                   f"🤖 **{bot_title}**\n"
                                   f"🕒 Time: `{current_time}`\n"
                                   f"📱 Service: **{icon} {selected_app.upper()}**\n"
                                   f"📞 Number: `{full_num}`\n"
                                   f"🌐 Country: {country}\n\n"
                                   f"🔑 **OTP Code:** `{isolated_code}`\n\n"
                                   f"{reward_text}\n\n"
                                   f"💬 Message:\n`{found_msg}`")
                
                user_markup = types.InlineKeyboardMarkup()
                user_markup.row(
                    types.InlineKeyboardButton("📋 Copy OTP Code", callback_data=f"copyotp_{isolated_code}"),
                    types.InlineKeyboardButton("📞 Copy Number", callback_data=f"copynum_{full_num}")
                )
                user_markup.row(types.InlineKeyboardButton("🔗 View OTP Group", url=get_otp_group_link()))
                
                safe_send_message(chat_id, user_alert_text, reply_markup=user_markup)
                return True
    except Exception as e:
        print(f"Error in check_and_send_otp_manual: {e}")
    return False

def background_user_otp_watcher(chat_id, message_id, selected_app, country, num):
    num_clean = str(num).replace("+", "").strip()
    if num_clean in active_user_watchers:
        return
    active_user_watchers.add(num_clean)
    
    checks = 0
    max_checks = 150 
    
    try:
        while checks < max_checks:
            time.sleep(3)
            checks += 1
            success = check_and_send_otp_manual(chat_id, selected_app, country, num, message_id)
            if success:
                break
    finally:
        if num_clean in active_user_watchers:
            active_user_watchers.remove(num_clean)

def process_withdraw_amount(message):
    chat_id = message.chat.id
    try:
        amount = float(message.text.strip())
        user_data = db.get_user(chat_id)
        bal = float(user_data.get("balance", 0.0))
        
        if amount < 50.0:
            safe_send_message(chat_id, "❌ **উইথড্র ব্যর্থ!**\n\nমিনিমাম উইথড্র অ্যামাউন্ট হচ্ছে **৫০ টাকা**।")
            return
            
        if amount > bal:
            safe_send_message(chat_id, f"❌ **উত্তোলন করার মতো পর্যাপ্ত ব্যালেন্স নেই!**\n\n• আপনার বর্তমান ব্যালেন্স: `{bal} BDT`\n• আপনি তুলতে চেয়েছেন: `{amount} BDT`")
            return
            
        settings = config.get("PAYMENT_SETTINGS", {"bkash": True, "nagad": True, "binance": True})
        markup = types.InlineKeyboardMarkup()
        
        has_active_method = False
        if settings.get("bkash", True):
            markup.add(types.InlineKeyboardButton("📱 bKash", callback_data=f"usrwd_bkash_{amount}"))
            has_active_method = True
        if settings.get("nagad", True):
            markup.add(types.InlineKeyboardButton("📱 Nagad", callback_data=f"usrwd_nagad_{amount}"))
            has_active_method = True
        if settings.get("binance", True):
            markup.add(types.InlineKeyboardButton("💳 Binance", callback_data=f"usrwd_binance_{amount}"))
            has_active_method = True
            
        if not has_active_method:
            safe_send_message(chat_id, "❌ দুঃখিত, বর্তমানে সকল উইথড্র গেটওয়ে বন্ধ আছে।")
            return
            
        safe_send_message(chat_id, f"📉 **উইথড্র গেটওয়ে সিলেক্ট করুন:**\n\n• উত্তোলন করার পরিমাণ: `{amount} BDT`", reply_markup=markup)
    except ValueError:
        safe_send_message(chat_id, "❌ অনুগ্রহ করে একটি সঠিক সংখ্যা দিন (যেমন: 50)।")

@bot.callback_query_handler(func=lambda call: call.data.startswith("usrwd_"))
def handle_user_withdraw_selection(call):
    parts = call.data.split("_")
    method = parts[1]
    amount = float(parts[2])
    chat_id = call.message.chat.id
    
    settings = config.get("PAYMENT_SETTINGS", {"bkash": True, "nagad": True, "binance": True})
    if not settings.get(method, True):
        bot.answer_callback_query(call.id, text=f"⚠️ দুঃখিত, বর্তমানে {method.upper()} গেটওয়ে বন্ধ।", show_alert=True)
        return
        
    try: bot.delete_message(chat_id, call.message.message_id)
    except: pass
    
    prompt_text = "Binance ID" if method == "binance" else f"{method.capitalize()} Personal Number"
    msg = safe_send_message(chat_id, f"👉 আপনার **{method.upper()}** {prompt_text} দিন:")
    if msg: bot.register_next_step_handler(msg, process_withdraw_address, method, amount)

def process_withdraw_address(message, method, amount):
    chat_id = message.chat.id
    address = message.text.strip()
    
    user_data = db.get_user(chat_id)
    bal = float(user_data.get("balance", 0.0))
    if amount > bal:
        safe_send_message(chat_id, "❌ ব্যালেন্স সংক্রান্ত অমিল! উইথড্র বাতিল করা হলো।")
        return
        
    db.update_user_balance(chat_id, -amount)
    
    req_id = f"wd_{int(time.time())}_{chat_id}"
    username_raw = message.from_user.username or "N/A"
    req_data = {
        "id": req_id,
        "user_id": chat_id,
        "username": username_raw,
        "method": method,
        "amount": amount,
        "address": address,
        "status": "pending",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    db.save_withdraw(req_id, req_data)
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("📢 Payment Channel", url="https://t.me/SHS_Otp_Channel"))
    markup.row(types.InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{config.get('DEV_USERNAME', 'Saku_143')}"))
    
    user_confirm_text = (
        f"⏱ **উইথড্র রিকোয়েস্ট প্রসেসিং-এ রয়েছে!** ⏱\n\n"
        f"💖 **প্রিয় গ্রাহক,** আপনার পেমেন্ট রিকোয়েস্টটি সফলভাবে সিস্টেমে যুক্ত হয়েছে। "
        f"খুব শীঘ্রই পেমেন্ট পাঠিয়ে দেওয়া হবে। ✨\n\n"
        f"📊 **উইথড্র বিবরণী:**\n"
        f" ├ 💎 পরিমাণ: `{amount:.2f} BDT`\n"
        f" ├ 📱 পেমেন্ট মেথড: `{method.upper()}`\n"
        f" └ 📌 অ্যাকাউন্ট / আইডি: `{address}`\n\n"
        f"🔔 **পেমেন্ট সংক্রান্ত যেকোনো আপডেটের জন্য পেমেন্ট চ্যানেলে চোখ রাখুন!** 👇"
    )
    
    safe_send_message(chat_id, user_confirm_text, reply_markup=markup)
    
    admin_markup = types.InlineKeyboardMarkup()
    admin_markup.row(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_wd_{req_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_wd_{req_id}")
    )
    
    safe_username = str(username_raw).replace("_", "\\_")
    admin_text = (f"📥 **নতুন উইথড্র রিকোয়েস্ট এসেছে!**\n\n"
                  f"👤 ইউজার আইডি: `{chat_id}` (Username: @{safe_username})\n"
                  f"💎 পরিমাণ: `{amount} BDT`\n"
                  f"📱 মেথড: `{method.upper()}`\n"
                  f"📌 অ্যাড্রেস: `{address}`\n"
                  f"🕒 সময়: `{req_data['time']}`")
    try:
        safe_send_message(int(config["ADMIN_ID"]), admin_text, reply_markup=admin_markup)
    except Exception as e:
        print(f"Error sending wd request to admin: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_wd_") or call.data.startswith("reject_wd_"))
def handle_admin_withdraw_action(call):
    if call.message.chat.id != int(config["ADMIN_ID"]): return
    
    is_approve = call.data.startswith("approve_wd_")
    req_id = call.data.replace("approve_wd_" if is_approve else "reject_wd_", "")
    
    req_data = db.get_withdraw(req_id)
    if not req_data:
        bot.answer_callback_query(call.id, text="❌ রিকোয়েস্টটি ডাটাবেজে পাওয়া যায়নি!", show_alert=True)
        return
        
    if req_data.get("status") != "pending":
        bot.answer_callback_query(call.id, text=f"⚠️ এটি ইতিমধ্যে {req_data.get('status')} করা হয়েছে!", show_alert=True)
        return
        
    user_id = req_data["user_id"]
    amount = req_data["amount"]
    method = req_data["method"]
    address = req_data["address"]
    
    if is_approve:
        req_data["status"] = "approved"
        db.save_withdraw(req_id, req_data)
        bot.answer_callback_query(call.id, text="✅ পেমেন্ট অ্যাপ্রুভ করা হয়েছে!", show_alert=True)
        
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"✅ **অ্যাপ্রুভড:** উইথড্র সফলভাবে সম্পন্ন!\n\n• User: `{user_id}`\n• Amount: `{amount} BDT`\n• Address: `{address}`", parse_mode="Markdown")
        
        user_msg = (f"🎉 **আপনার উইথড্র রিকোয়েস্টটি এপ্রুভ করা হয়েছে!**\n\n"
                    f"💎 পরিমাণ: `{amount} BDT`\n"
                    f"📱 মেথড: `{method.upper()}`")
        try: safe_send_message(user_id, user_msg)
        except: pass
        
        payment_channel_id = "-1003956226642"
        pay_alert = (f"🎉 **SUCCESSFUL WITHDRAWAL** 🎉\n\n"
                     f"👤 User ID: `{str(user_id)[:4]}***`\n"
                     f"📱 Method: `{method.upper()}`\n"
                     f"💎 Amount: `{amount} BDT`\n"
                     f"📌 Account: `{address[:4]}***{address[-3:] if len(address) > 6 else ''}`\n"
                     f"✅ Status: Paid & Completed!")
        try:
            safe_send_message(int(payment_channel_id), pay_alert)
        except Exception as e:
            print(f"Error posting to payment channel: {e}")
            
    else:
        req_data["status"] = "rejected"
        db.save_withdraw(req_id, req_data)
        db.update_user_balance(user_id, amount)
        
        bot.answer_callback_query(call.id, text="❌ পেমেন্ট রিজেক্ট এবং রিফান্ড করা হয়েছে!", show_alert=True)
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, 
                              text=f"❌ **রিজেক্টেড:** উইথড্র রিকোয়েস্ট বাতিল এবং রিফান্ড সম্পন্ন।", parse_mode="Markdown")
        
        user_msg = (f"❌ **আপনার উইথড্র রিকোয়েস্টটি বাতিল করা হয়েছে!**\n\n"
                    f"💎 পরিমাণ: `{amount} BDT`\n"
                    f"উইথড্র অ্যামাউন্ট ব্যালেন্সে ফেরত দেওয়া হয়েছে।")
        try: safe_send_message(user_id, user_msg)
        except: pass

# --- STRICT SERVICE DETECTOR FROM SMS CONTENT & API METADATA ---
def detect_service_from_message(msg_body, fallback_platform=""):
    body_lower = str(msg_body).lower()
    plat_lower = str(fallback_platform).lower().strip()
    
    # Check explicitly from SMS Text
    if any(kw in body_lower for kw in ["instagram", "ig-", "ig code", "insta", "ig_"]):
        return "instagram"
    elif any(kw in body_lower for kw in ["facebook", "fb-", "fb code", "meta"]):
        return "facebook"
    elif any(kw in body_lower for kw in ["whatsapp", "wa code", "wa-"]):
        return "whatsapp"
    elif any(kw in body_lower for kw in ["telegram", "tg code", "tg-"]):
        return "telegram"
    elif "imo" in body_lower:
        return "imo"
    elif "discord" in body_lower:
        return "discord"
    elif any(kw in body_lower for kw in ["tiktok", "tt code", "tt-"]):
        return "tiktok"
        
    # Check from API fallback tag
    if plat_lower in ["tg", "telegram"]: return "telegram"
    elif plat_lower in ["ig", "instagram", "ins", "insta", "inst"]: return "instagram"
    elif plat_lower in ["fb", "facebook"]: return "facebook"
    elif plat_lower in ["wa", "whatsapp"]: return "whatsapp"
    elif plat_lower in ["tt", "tiktok"]: return "tiktok"
    elif plat_lower in ["imo"]: return "imo"
    elif plat_lower in ["discord"]: return "discord"
    
    return "facebook" # Default safe fallback

# --- SMS / OTP LIVE MONITOR ENGINE (FORWARD REALTIME HITS TO OTP GROUP) ---
def background_live_sms_monitor():
    global seen_console_hits, range_hits_tracker, last_announced_range
    while True:
        try:
            time.sleep(3)
            base_url = str(config['BASE_URL']).strip().rstrip('/')
            url = f"{base_url}/numsuccess/info"
            
            res = requests.get(url, headers=get_api_headers(), timeout=15).json()
            meta = res.get("meta", {})
            
            if meta.get("code") == 200 or meta.get("status") == "success":
                otps_list = res.get("data", {}).get("otps", [])
                
                for item in otps_list:
                    nid = item.get("nid", "")
                    msg_body = item.get("otp", "") or item.get("message", "")
                    num = item.get("number", "")
                    
                    if not msg_body or not str(msg_body).strip():
                        continue
                        
                    raw_id_string = f"{nid}_{num}_{msg_body[:15]}"
                    hit_id = hashlib.md5(raw_id_string.encode()).hexdigest()
                    
                    if hit_id in seen_console_hits:
                        continue
                    seen_console_hits.add(hit_id)
                    
                    if len(seen_console_hits) > 3000:
                        seen_console_hits.clear()
                        
                    platform = detect_service_from_message(msg_body, item.get("service") or "")
                    icon = get_service_icon(platform)
                    country_short = get_country_code_short(num)
                    
                    num_clean = str(num).replace("+", "").strip()
                    range_val = num_clean[:8] + "XXX" if len(num_clean) >= 8 else "Global"
                    country_name = get_country_info_by_range(num)
                    
                    # কড়া ম্যাচিং: সঠিক সার্ভিস ফোল্ডারে সচল রেঞ্জ আপডেট হবে
                    if range_val != "Global" and platform in config["SERVICES"]:
                        active_ranges_global.add(range_val)
                        config["SERVICES"][platform]["rids"][country_name] = range_val
                        save_config(config)
                    
                    current_time_epoch = time.time()
                    key = (range_val, platform)
                    range_hits_tracker[key].append(current_time_epoch)
                    range_hits_tracker[key] = [t for t in range_hits_tracker[key] if current_time_epoch - t < 180]
                    
                    code_match = re.search(r'\b\d{4,8}\b', msg_body)
                    isolated_code = code_match.group(0) if code_match else "N/A"
                    masked_num = format_group_phone_number(num)
                    
                    bot_user = config.get("BOT_USERNAME", "SHS_SMSHUB_bot")
                    
                    live_alert = (f"**{platform.upper()} SMS Number x TNE**              `Admin` \n"
                                  f"🇨🇫 {country_short} | {icon} | 📱 `{masked_num}` | 🔊 English \n\n"
                                  f"💬 Message:\n`{msg_body}`")
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.row(
                        types.InlineKeyboardButton("📢 Channel", url="https://t.me/SHS_Otp_Channel"),
                        types.InlineKeyboardButton(f"🔑 {isolated_code}", callback_data=f"copyotp_{isolated_code}")
                    )
                    markup.row(types.InlineKeyboardButton("📞 Get Number ↗️", url=f"https://t.me/{bot_user}?start=getnum_{platform}"))
                    
                    for dest_id in config.get("OTP_DESTINATIONS", []):
                        try:
                            safe_send_message(int(dest_id), live_alert, reply_markup=markup)
                        except: pass
        except Exception as e:
            print(f"Monitor loop error: {e}")
            time.sleep(4)

# --- ACTIVE RANGES SYNC ENGINE ---
def sync_services_once():
    global active_ranges_global, range_hits_count
    try:
        base_url = str(config['BASE_URL']).strip().rstrip('/')
        url = f"{base_url}/active-ranges"
        response = requests.get(url, headers=get_api_headers(), timeout=15)
        
        if response.status_code == 200:
            res = response.json()
            if res.get("success") is True or res.get("message") == "Global routing ranges fetched":
                active_list = res.get("data", {}).get("active_ranges", [])
                
                for item in active_list:
                    r_str = item.get("range", "")
                    service_raw = str(item.get("service", "")).lower().strip()
                    hits = item.get("hits", 0)
                    
                    if not r_str or not service_raw:
                        continue
                        
                    clean_r = format_rid(r_str)
                    range_hits_count[clean_r] = hits
                    active_ranges_global.add(clean_r)
                    
                    if service_raw in ["tg", "telegram"]: service_id = "telegram"
                    elif service_raw in ["ig", "instagram", "ins", "insta", "inst"]: service_id = "instagram"
                    elif service_raw in ["fb", "facebook"]: service_id = "facebook"
                    elif service_raw in ["wa", "whatsapp"]: service_id = "whatsapp"
                    elif service_raw in ["tt", "tiktok"]: service_id = "tiktok"
                    elif service_raw in ["imo"]: service_id = "imo"
                    elif service_raw in ["discord"]: service_id = "discord"
                    else: service_id = service_raw
                    
                    country_name = get_country_info_by_range(clean_r)
                    
                    if service_id in config["SERVICES"]:
                        config["SERVICES"][service_id]["rids"][country_name] = clean_r
                
                save_config(config)
    except Exception as e:
        print(f"Active Ranges Sync error: {e}")

def background_services_sync():
    while True:
        try:
            sync_services_once()
        except Exception as e:
            print(f"Background Sync Error: {e}")
        time.sleep(15)

@bot.callback_query_handler(func=lambda call: call.data == "back_services")
def back_to_serv(call): send_services_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back(call): 
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    send_home_keyboard(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def check(call):
    if is_subscribed_all(call.from_user.id): 
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        send_home_keyboard(call.message.chat.id, "✅ ভেরিфикации সফল! এখন থেকে সার্ভিস ব্যবহার করতে পারবেন।")
    else: 
        bot.answer_callback_query(call.id, text="❌ আপনি এখনো সমস্ত বাধ্যতামূলক চ্যানেল বা গ্রুপে জয়েন করেননি!", show_alert=True)

if __name__ == "__main__":
    print("⏳ SHS OTP HUB রিয়েল-টাইম ইঞ্জিন স্টার্ট হচ্ছে...")
    sync_services_once()
    
    keep_alive()
    Thread(target=background_live_sms_monitor, daemon=True).start()
    Thread(target=background_services_sync, daemon=True).start()
    
    try: bot.delete_webhook(drop_pending_updates=True)
    except: pass
    print("🚀 SHS OTP HUB Premium Multi-Threaded Bot রানিং...")
    bot.polling(none_stop=True)
