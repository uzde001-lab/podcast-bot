# Podcast Bot — Render

Telegram bot YouTube havolasi yoki audio/video faylni oladi, nutqni Groq Whisper (`whisper-large-v3-turbo`) bilan matnga aylantiradi va tanlangan formatda qaytaradi. YouTube audiosi 10 daqiqalik bo‘laklarda qayta ishlanadi; video limiti 180 daqiqagacha. Natijalar: PDF, PDF + AI xulosasi, DOCX, TXT va SRT. PDF/DOCX/SRT fayllarida vaqt belgilari bor. `/lang` orqali Uzbek, Russian, English yoki Turkish tilini tanlash mumkin.

## Mavjud Render xizmati

`podcast-bot` servisi Python/Gunicorn bilan ishlaydi: `pip install -r requirements.txt` va `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 900`. `/health` endpointi sozlamalar mavjudligini secret qiymatlarini ko‘rsatmasdan qaytaradi. Aiogram webhook Flask health serveri orqali keladi; update’lar botning alohida asyncio loop’iga uzatiladi. Uzun video ishlari webhook javoblarini to‘smasligi uchun asinxron/thread pool yordamida bajariladi. Deploy paytida Telegram navbatidagi update’lar o‘chirilmaydi.

## Render environment

Render Dashboard → `podcast-bot` → Environment bo‘limida `BOT_TOKEN` va `GROQ_API_KEY` ni secret sifatida qo‘ying. Ixtiyoriy `ADMIN_IDS` Telegram user ID’larini vergul bilan qabul qiladi; adminlarga kunlik limit qo‘llanmaydi. YouTube IP cheklovi bo‘lsa `YT_PROXY` yoki `YT_COOKIES_B64`/`YT_COOKIES` qo‘shish mumkin. `PUBLIC_URL` berilmasa Render’ning `RENDER_EXTERNAL_URL` qiymati ishlatiladi. Hech qaysi kalitni `.env` yoki GitHub’ga commit qilmang.

## Limit va ishga tushish xususiyatlari

Video maksimal 180 daqiqa, Telegram’dan yuboriladigan audio/video fayl maksimal 20 MB, oddiy foydalanuvchiga esa kuniga 8 yangi ish limiti bor. Audio bo‘laklari parallel transkripsiya qilinadi; Groq limitiga uchrasa bot kutib, qayta urinadi. YouTube video yopiq, yosh yoki hududiy cheklovda, yoxud server IP manzili bloklangan bo‘lsa, proxy/cookie sozlash yoki media faylni Telegram orqali yuborish kerak bo‘lishi mumkin.

Render Free idle paytida uxlab qolishi mumkin, shuning uchun ilk webhook so‘rovida cold-start kechikishi bo‘lishi ehtimol. Free-tier uyqusini chetlab o‘tish uchun xizmatga o‘zidan ping yuborish funksiyasi qo‘shilmagan. SQLite kesh, usage sanog‘i va foydalanuvchi sozlamalari vaqtinchalik diskda; restart/sleep paytida saqlanishiga kafolat yo‘q.

## Local

Python muhitida `pip install -r requirements.txt` bajaring va `BOT_TOKEN`, `GROQ_API_KEY` ni environment’ga sozlang. `python telegram_bot.py` local polling rejimi; `gunicorn app:app` esa webhook/health wrapper’ni ishga tushiradi.
