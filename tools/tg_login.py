"""Bir martalik: Telegram sessiya kalitini (TG_SESSION) olish.

GitHub Codespaces terminalida ishga tushiring:
    pip install telethon
    python tools/tg_login.py

Dastur api_id, api_hash, telefon raqami va Telegramga kelgan kodni so'raydi
(2 bosqichli parol bo'lsa, uni ham). Oxirida uzun kalit chiqadi — uni
GitHub → Settings → Secrets and variables → Actions → TG_SESSION ga qo'ying.
Kalit akkauntga to'liq kirish beradi: hech kimga yubormang, chatga yozmang.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("api_id (raqam): ").strip())
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    print("\nKirildi:", me.first_name, "(@" + str(me.username) + ")")
    print("\n===== TG_SESSION (shu qatorni to'liq nusxalang) =====\n")
    print(client.session.save())
    print("\n=====================================================")
