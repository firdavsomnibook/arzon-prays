# Arzon Prays — by Shifoat

Optovik prayslarini davlat referent narxlari bilan solishtiruvchi dastur.
Telegramdagi prays kanallaridan yangi Excel'larni avtomatik oladi.

## Tuzilishi
| Papka | Nima |
|---|---|
| `docs/` | Sayt (GitHub Pages shu papkadan ochadi): `index.html`, `fb.js` (Firebase ulagichi), `data/referent.json` |
| `worker/` | Telegram bot: kanallarni tekshirib, yangi faylni Firebase'ga yozadi |
| `.github/workflows/telegram-prices.yml` | Botni har 10 daqiqada ishga tushiradi (06:00–24:00) |
| `tools/tg_login.py` | Bir martalik: Telegram sessiya kalitini olish |

## Maxfiy sozlamalar (Settings → Secrets and variables → Actions)
`TG_API_ID`, `TG_API_HASH`, `TG_SESSION`, `FB_BOT_EMAIL`, `FB_BOT_PASSWORD`

Ma'lumotlar (prayslar, savat, buyurtmalar) repozitoriyada emas — Firebase'da, faqat login bilan ochiladi.

## Muammo bo'lsa
- **Actions** bo'limida oxirgi ishga tushirishni oching — qizil bo'lsa, ichidagi xabarni o'qing.
- Sahifada Prayslar bo'limidagi "📲 Telegram bot" qatori bot oxirgi marta qachon tekshirganini ko'rsatadi.
- "Telegram sessiyasi bekor bo'lgan" — `tools/tg_login.py` ni qayta ishga tushirib, `TG_SESSION` ni yangilang.
