# Podcast Transcript Bot — Render

`main.py` YouTube havolasi yoki Telegramga yuborilgan video/audio faylni Gemini yordamida matnga aylantirib, PDF qaytaradi. YouTube rejimi bitta podcastni 60 daqiqagacha qabul qilishga sozlangan.

## Render sozlamalari

GitHub repository ichida `main.py`, `requirements.txt` va `render.yaml` bo‘lsin. Render Dashboard → New → Web Service → GitHub repository tanlang. Manual sozlama kerak bo‘lsa:

- Language: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 900`
- Plan: Free

Environment Variables bo‘limiga `TELEGRAM_BOT_TOKEN` va `GEMINI_API_KEY` qo‘ying. `PUBLIC_URL` ga Render bergan `https://...onrender.com` manzilini yozing. Deploy tugagach `/health` ochilib `telegram: true`, `gemini: true` ko‘rinishi kerak.

## Muhim limit

Render Free servisida 512 MB RAM, ephemeral disk va idle paytida sleep bor. 30–60 daqiqalik podcastni YouTube havolasi orqali serverda yuklab, bitta PDF qilishga mo‘ljallangan. YouTube havolasi bilan ishlash YouTube anti-bot chekloviga ham bog‘liq; bloklangan havolada audio/video faylni Telegramga yuborish kerak bo‘ladi. Oddiy Telegram Bot API katta fayl yuklashda cheklangan.
