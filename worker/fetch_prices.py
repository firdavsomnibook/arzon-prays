"""Arzon Prays — Telegram prays kanallarini tekshiruvchi bot.

GitHub Actions har 10 daqiqada ishga tushiradi. Ish tartibi:
  1. Firebase'ga bot login bilan kiradi.
  2. "supinfo" dan "Avtomatik yuklash" yoqilgan va prays kanali yozilgan optoviklarni oladi.
  3. Har bir kanaldagi oxirgi Excel (bo'lmasa PDF) faylni topadi.
  4. Fayl yangi bo'lsa — yuklab olib, Firebase'ga ("tgfiles/<id>__N" bo'laklari) yozadi
     va "tgprice/<id>" ga ma'lumot qoldiradi. Arzon Prays sahifasi shuni ko'rib tabloni yangilaydi.

Kerakli maxfiy sozlamalar (GitHub → Settings → Secrets and variables → Actions):
  TG_API_ID, TG_API_HASH, TG_SESSION, FB_BOT_EMAIL, FB_BOT_PASSWORD
"""
import asyncio
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

FB_API_KEY = os.environ.get("FB_API_KEY", "AIzaSyA50NZ2snvTz25g1f1xQ5L4d7T1FJhdmXc")
FB_PROJECT = os.environ.get("FB_PROJECT", "arzon-prays")
FS = f"https://firestore.googleapis.com/v1/projects/{FB_PROJECT}/databases/(default)/documents"
CHUNK = 900_000            # bitta Firestore hujjatiga sig'adigan base64 bo'lak
MAX_BYTES = 8 * 1024 * 1024
SCAN_LIMIT = 60            # kanaldagi oxirgi nechta FAYLLI xabar ko'riladi
FOLDER = os.environ.get("TG_FOLDER", "FIRMALAR")  # Telegramdagi ta'minotchilar papkasi
# shu so'zlar fayl nomida bo'lsa — bu prays emas (masalan, "Новый приход" — narxsiz yangi kelganlar e'loni)
SKIP_WORDS = ("приход", "prixod", "prihod", "новинк")
EXCEL = (".xlsx", ".xls", ".xlsm")
PDF_NEWER_HOURS = 12       # PDF Excel'dan shuncha soat yangi bo'lsagina PDF tanlanadi


# ---------------- Firebase (REST) ----------------
class Firebase:
    def __init__(self, opener=urllib.request.urlopen):
        self.token = None
        self._open = opener

    def _req(self, method, url, body=None, headers=None, form=False):
        data = None
        h = dict(headers or {})
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                h["content-type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                h["content-type"] = "application/json"
        if self.token and "googleapis.com/v1/projects" in url:
            h["authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self._open(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw.decode(errors="replace")[:300]}

    def login(self, email, password):
        st, j = self._req("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FB_API_KEY}",
                          {"email": email, "password": password, "returnSecureToken": True})
        if st != 200:
            raise RuntimeError(f"Firebase'ga kirib bo'lmadi: {j.get('error', j)}")
        self.token = j["idToken"]

    @staticmethod
    def _path(p):
        return "/".join(urllib.parse.quote(s, safe="") for s in p.split("/"))

    @staticmethod
    def _decode(fields):
        if fields and "j" in fields and "stringValue" in fields["j"]:
            try:
                return json.loads(fields["j"]["stringValue"])
            except Exception:
                return {}
        return {}

    def get(self, path):
        st, j = self._req("GET", f"{FS}/{self._path(path)}")
        if st == 404:
            return None
        if st != 200:
            raise RuntimeError(f"o'qib bo'lmadi {path}: {st} {j}")
        return self._decode(j.get("fields"))

    def set(self, path, obj):
        fields = {"j": {"stringValue": json.dumps(obj, ensure_ascii=False)},
                  "u": {"timestampValue": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}
        st, j = self._req("PATCH", f"{FS}/{self._path(path)}", {"fields": fields})
        if st != 200:
            raise RuntimeError(f"yozib bo'lmadi {path}: {st} {j}")

    def delete(self, path):
        st, j = self._req("DELETE", f"{FS}/{self._path(path)}")
        if st not in (200, 404):
            raise RuntimeError(f"o'chirib bo'lmadi {path}: {st} {j}")

    def list(self, coll):
        out, token = {}, ""
        for _ in range(50):
            q = "?pageSize=300" + (("&pageToken=" + urllib.parse.quote(token)) if token else "")
            st, j = self._req("GET", f"{FS}/{self._path(coll)}{q}")
            if st == 404:
                break
            if st != 200:
                raise RuntimeError(f"ro'yxatni o'qib bo'lmadi {coll}: {st} {j}")
            for d in j.get("documents", []):
                out[d["name"].rsplit("/", 1)[1]] = self._decode(d.get("fields"))
            token = j.get("nextPageToken")
            if not token:
                break
        return out

    def put_file(self, sid, data: bytes, prev_n: int, meta: dict) -> int:
        b64 = base64.b64encode(data).decode()
        n = max(1, -(-len(b64) // CHUNK))
        for i in range(n):
            self.set(f"tgfiles/{sid}__{i}", {"b": b64[i * CHUNK:(i + 1) * CHUNK]})
        self.set(f"tgfiles/{sid}", dict(meta, n=n, size=len(data)))
        for i in range(n, prev_n or 0):  # eski faylning ortiqcha bo'laklari
            self.delete(f"tgfiles/{sid}__{i}")
        return n


# ---------------- Telegram ----------------
def channel_ref(v: str):
    """'@nom', 't.me/nom', 'https://t.me/s/nom', 't.me/+HASH', 't.me/joinchat/HASH', '-100…' ni Telethon tushunadigan ko'rinishga keltiradi."""
    v = (v or "").strip()
    if not v:
        return None
    if re.fullmatch(r"-?\d{5,}", v):
        return int(v)
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?(\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{4,})", v, re.I)
    if m:
        part = m.group(1)
        if part.startswith("+") or part.lower().startswith("joinchat/"):
            return "https://t.me/" + part   # yopiq kanal taklif havolasi — akkaunt a'zo bo'lishi kerak
        return part
    if v.startswith("@"):
        return v[1:]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", v):
        return v
    return v


def kind_of(name: str):
    n = (name or "").lower()
    if n.endswith(EXCEL):
        return "xlsx"
    if n.endswith(".pdf"):
        return "pdf"
    return None


def name_ok(name: str, match: str = "") -> bool:
    n = (name or "").lower()
    if any(w in n for w in SKIP_WORDS):
        return False
    words = [w.strip().lower() for w in re.split(r"[,;]", match or "") if w.strip()]
    return not words or any(w in n for w in words)


def pick(messages, match: str = ""):
    """Eng yangi Excel; Excel umuman yo'q yoki PDF ancha yangi bo'lsa — PDF.
    "приход" fayllari va (agar berilgan bo'lsa) nomida kalit so'z bo'lmagan fayllar o'tkazib yuboriladi."""
    ex = pd = None
    for m in messages:  # yangidan eskiga
        f = getattr(m, "file", None)
        name = getattr(f, "name", None) if f else None
        k = kind_of(name)
        if not k or not name_ok(name, match):
            continue
        if k == "xlsx" and ex is None:
            ex = m
        elif k == "pdf" and pd is None:
            pd = m
        if ex is not None and pd is not None:
            break
    if ex is None:
        return (pd, "pdf") if pd is not None else (None, None)
    if pd is not None and (pd.date - ex.date).total_seconds() > PDF_NEWER_HOURS * 3600:
        return pd, "pdf"
    return ex, "xlsx"


def _title(t):
    return getattr(t, "text", t) if t is not None else ""


async def read_folder(client, fb, log=print):
    """Telegramdagi FOLDER papkasidagi chatlar ro'yxatini "tgprice/_chats" ga yozadi; {chat_id: entity} qaytaradi."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon import utils
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)
    folder = None
    for f in filters or []:
        if _title(getattr(f, "title", None)).strip().lower() == FOLDER.lower():
            folder = f
            break
    out, ents = [], {}
    if folder is None:
        log(f"'{FOLDER}' papkasi topilmadi")
    else:
        peers = list(getattr(folder, "pinned_peers", []) or []) + list(getattr(folder, "include_peers", []) or [])
        for p in peers:
            try:
                e = await client.get_entity(p)
            except Exception as ex:  # noqa
                log(f"papkadagi chatni ochib bo'lmadi: {ex}")
                continue
            cid = str(utils.get_peer_id(e))
            if cid in ents:
                continue
            ents[cid] = e
            out.append({"id": cid, "title": getattr(e, "title", None) or getattr(e, "first_name", "") or cid,
                        "username": getattr(e, "username", None) or ""})
    fb.set("tgprice/_chats", {"folder": FOLDER, "found": folder is not None, "chats": out,
                              "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    log(f"Papkada {len(out)} ta chat")
    return ents


async def run(fb: Firebase, client, log=print, folder_reader=None):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = {"lastRun": now, "checked": [], "updated": [], "errors": {}}
    try:
        ents = await (folder_reader or read_folder)(client, fb, log)
    except Exception as e:
        ents = {}
        status["folderError"] = f"{type(e).__name__}: {e}"[:200]
        log("papkani o'qib bo'lmadi: " + status["folderError"])
    sup = fb.list("supinfo")
    targets = {sid: i for sid, i in sup.items()
               if i.get("tgAuto") and ((i.get("tgChat") or "").strip() or (i.get("tgPrice") or "").strip())}
    log(f"Kuzatiladigan ta'minotchilar: {len(targets)}")
    for sid, info in targets.items():
        chat = (info.get("tgChat") or "").strip()
        source = chat or info["tgPrice"].strip()
        try:
            if chat:
                ent = ents.get(chat)
                if ent is None:
                    ent = await client.get_entity(int(chat))
            else:
                ent = await client.get_entity(channel_ref(info["tgPrice"]))
            try:
                from telethon.tl.types import InputMessagesFilterDocument
                flt = {"filter": InputMessagesFilterDocument}
            except Exception:
                flt = {}
            msgs = [m async for m in client.iter_messages(ent, limit=SCAN_LIMIT, **flt)]
            m, kind = pick(msgs, info.get("tgMatch") or "")
            status["checked"].append(sid)
            if m is None:
                status["errors"][sid] = ("kalit so'z bo'yicha fayl topilmadi: " + info["tgMatch"]) if info.get("tgMatch") else "kanalda Excel yoki PDF fayl topilmadi"
                continue
            prev = fb.get(f"tgprice/{sid}") or {}
            if prev.get("tgId") == m.id and prev.get("channel") == source:
                log(f"{sid}: yangi fayl yo'q ({prev.get('fileName')})")
                continue
            size = getattr(m.file, "size", 0) or 0
            if size > MAX_BYTES:
                status["errors"][sid] = f"fayl juda katta ({size // 1024} KB)"
                continue
            # sahifa solishtiradigan raqam: fayl vaqti; har doim oldingisidan katta bo'lishi kafolatlanadi
            key = max(int(m.date.timestamp()), (prev.get("msgId") or 0) + 1)
            data = await client.download_media(m, file=bytes)
            prev_files = fb.get(f"tgfiles/{sid}") or {}
            n = fb.put_file(sid, data, prev_files.get("n", 0), {"fileName": m.file.name})
            fb.set(f"tgprice/{sid}", {"msgId": key, "tgId": m.id, "date": m.date.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "fileName": m.file.name, "kind": kind, "n": n, "size": len(data),
                                      "channel": source, "fetchedAt": now})
            status["updated"].append(sid)
            log(f"{sid}: yangi {kind} — {m.file.name} ({len(data) // 1024} KB)")
        except Exception as e:  # bitta kanal xatosi boshqalarini to'xtatmasin
            msg = f"{type(e).__name__}: {e}"[:240]
            if "ChannelPrivate" in msg or "InviteHash" in msg or "not part" in msg.lower():
                msg = "kanalga kirib bo'lmadi — @shifoatuz akkaunti shu kanalga a'zo bo'lishi kerak"
            elif "No user has" in msg or "Cannot find any entity" in msg or "Nobody is using" in msg:
                msg = "kanal topilmadi — havolani yoki papkani tekshiring"
            status["errors"][sid] = msg
            log(f"{sid}: XATO {msg}")
    fb.set("tgprice/_status", status)
    return status


# ---------------- ✈️ Buyurtmalarni agentlarga yuborish (sayt "Hammasiga jo'natish") ----------------
OUTBOX_LOOP_SEC = int(os.environ.get("OUTBOX_LOOP_SEC", "540"))  # bitta ishga tushishda navbatni shuncha soniya kuzatadi
OUTBOX_POLL = 20


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def out_file(fb, key, n):
    b64 = "".join((fb.get(f"outfiles/{key}__{i}") or {}).get("b", "") for i in range(int(n or 1)))
    return base64.b64decode(b64) if b64 else b""


async def resolve_to(client, to, name=""):
    """@nom, t.me/nom, +998... raqam yoki 'me' -> Telegram suhbatdoshi"""
    v = str(to or "").strip()
    if not v:
        raise ValueError("manzil yo'q")
    if v == "me":
        return "me"
    m = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]+)", v, re.I)
    if m:
        return await client.get_entity(m.group(1))
    if v.startswith("@"):
        return await client.get_entity(v[1:])
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 9:
        if len(digits) == 9:
            digits = "998" + digits
        phone = "+" + digits
        try:
            return await client.get_entity(phone)
        except Exception:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact
            r = await client(ImportContactsRequest([InputPhoneContact(client_id=0, phone=phone, first_name=(name or "Agent")[:60], last_name="")]))
            if r.users:
                return r.users[0]
            raise ValueError(f"{phone} raqami Telegram'da topilmadi (yoki raqam yashirilgan) — agentning @username'ini kiriting")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", v):
        return await client.get_entity(v)
    raise ValueError("manzil tushunarsiz: " + v)


async def process_outbox(fb, client, log=print):
    box = fb.list("outbox")
    now = dt.datetime.now(dt.timezone.utc)
    sent = 0
    for oid, o in sorted(box.items(), key=lambda kv: kv[1].get("at", "")):
        st = o.get("status")
        # eski (7 kundan) yozuvlarni tozalash
        try:
            age = (now - dt.datetime.fromisoformat(str(o.get("at", "")).replace("Z", "+00:00"))).days
        except Exception:
            age = 0
        if st in ("sent", "error") and age > 7:
            fb.delete(f"outbox/{oid}")
            continue
        if st != "queued":
            continue
        o["status"] = "sending"
        fb.set(f"outbox/{oid}", o)
        try:
            ent = await resolve_to(client, o.get("to"), o.get("supName", ""))
            await client.send_message(ent, o.get("text") or "Buyurtma")
            import io
            for f in o.get("files", []):
                data = out_file(fb, f["key"], f.get("n"))
                if not data:
                    raise ValueError("fayl topilmadi: " + f.get("name", ""))
                bio = io.BytesIO(data)
                bio.name = f.get("name") or "buyurtma"
                await client.send_file(ent, bio, caption=f.get("caption") or None, force_document=not f.get("photo"))
                await asyncio.sleep(1)
            o.update(status="sent", sentAt=_now(), error="")
            for f in o.get("files", []):
                for i in range(int(f.get("n") or 1)):
                    fb.delete(f"outfiles/{f['key']}__{i}")
            sent += 1
            log(f"yuborildi: {o.get('supName')} -> {o.get('to')}")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:240]
            if "No user has" in msg or "Nobody is using" in msg or "Cannot find any entity" in msg:
                msg = "Telegram'da bunday foydalanuvchi topilmadi — manzilni tekshiring"
            elif "FloodWait" in msg or "PeerFlood" in msg:
                msg = "Telegram vaqtincha chekladi (juda ko'p xabar) — keyinroq qayta jo'nating"
            o.update(status="error", error=msg)
            log(f"XATO {o.get('supName')}: {msg}")
        fb.set(f"outbox/{oid}", o)
        await asyncio.sleep(3)
    return sent


async def outbox_loop(fb, client, seconds=OUTBOX_LOOP_SEC, log=print):
    end = asyncio.get_event_loop().time() + seconds
    while True:
        try:
            await process_outbox(fb, client, log)
        except Exception as e:
            log(f"navbatni o'qib bo'lmadi: {e}")
        if asyncio.get_event_loop().time() + OUTBOX_POLL >= end:
            break
        await asyncio.sleep(OUTBOX_POLL)


async def main():
    need = ["TG_API_ID", "TG_API_HASH", "TG_SESSION", "FB_BOT_EMAIL", "FB_BOT_PASSWORD"]
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        print("Yetishmayotgan maxfiy sozlamalar: " + ", ".join(miss))
        sys.exit(1)
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    fb = Firebase()
    fb.login(os.environ["FB_BOT_EMAIL"], os.environ["FB_BOT_PASSWORD"])
    client = TelegramClient(StringSession(os.environ["TG_SESSION"]), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            fb.set("tgprice/_status", {"lastRun": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "error": "Telegram sessiyasi bekor bo'lgan — tools/tg_login.py ni qayta ishga tushiring"})
            print("Telegram sessiyasi bekor bo'lgan")
            sys.exit(2)
        await client.get_dialogs()  # chatlar ro'yxatini keshga oladi (id bo'yicha topish uchun)
        try:
            await process_outbox(fb, client)  # navbatda turgan buyurtmalar — avval shular
        except Exception as e:
            print(f"navbat: {e}")
        await run(fb, client)
        await outbox_loop(fb, client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
