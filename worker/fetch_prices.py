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
SCAN_LIMIT = 40            # kanaldagi oxirgi nechta xabar ko'riladi
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


def pick(messages):
    """Eng yangi Excel; Excel umuman yo'q yoki PDF ancha yangi bo'lsa — PDF."""
    ex = pd = None
    for m in messages:  # yangidan eskiga
        f = getattr(m, "file", None)
        k = kind_of(getattr(f, "name", None)) if f else None
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


async def run(fb: Firebase, client, log=print):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = {"lastRun": now, "checked": [], "updated": [], "errors": {}}
    sup = fb.list("supinfo")
    targets = {sid: i for sid, i in sup.items() if i.get("tgAuto") and (i.get("tgPrice") or "").strip()}
    log(f"Kuzatiladigan kanallar: {len(targets)}")
    for sid, info in targets.items():
        ref = channel_ref(info["tgPrice"])
        try:
            ent = await client.get_entity(ref)
            msgs = [m async for m in client.iter_messages(ent, limit=SCAN_LIMIT)]
            m, kind = pick(msgs)
            status["checked"].append(sid)
            if m is None:
                status["errors"][sid] = "kanalda Excel yoki PDF fayl topilmadi"
                continue
            prev = fb.get(f"tgprice/{sid}") or {}
            if prev.get("tgId") == m.id and prev.get("channel") == info["tgPrice"]:
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
                                      "channel": info["tgPrice"], "fetchedAt": now})
            status["updated"].append(sid)
            log(f"{sid}: yangi {kind} — {m.file.name} ({len(data) // 1024} KB)")
        except Exception as e:  # bitta kanal xatosi boshqalarini to'xtatmasin
            msg = f"{type(e).__name__}: {e}"[:240]
            if "ChannelPrivate" in msg or "InviteHash" in msg or "not part" in msg.lower():
                msg = "kanalga kirib bo'lmadi — @shifoatuz akkaunti shu kanalga a'zo bo'lishi kerak"
            elif "No user has" in msg or "Cannot find any entity" in msg or "Nobody is using" in msg:
                msg = "kanal topilmadi — havolani tekshiring"
            status["errors"][sid] = msg
            log(f"{sid}: XATO {msg}")
    fb.set("tgprice/_status", status)
    return status


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
        await run(fb, client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
