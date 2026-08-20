import os
from flask import Flask, request
from upstash_redis import Redis
import telebot

app = Flask(__name__)

# these come from Vercel secrets
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start', 'balance'])
def handle_balance(m):
    bal = redis.get(f"pulse:{m.from_user.id}") or 0
    bot.reply_to(m, f"Your balance: {float(bal):.4f} PULSE")

@app.route("/api/reward", methods=["GET"])
def reward():
    user_id = request.args.get("userid")
    if not user_id:
        return "missing userid", 400
    redis.incrbyfloat(f"pulse:{user_id}", 0.0050)
    return "OK", 200

@app.route("/api/telegram", methods=["POST"])
def telegram():
    update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=["GET"])
def home():
    return "PulseVault Permanent - OK"