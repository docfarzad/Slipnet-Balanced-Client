# Slipnet Balanced Client
[ [English](https://github.com/docfarzad/Slipnet-Balanced-Client/tree/main) | [فارسی / Persian](https://github.com/docfarzad/Slipnet-Balanced-Client/blob/main/README_fa.md) ]

یک ابزار ساده دسکتاپ برای پیدا کردن DNS Resolverهای سالم برای `slipnet.exe` و اجرای یک پراکسی لوکال با بالانس بار.

## این ابزار چه کاری انجام می‌دهد؟

- یک فایل متنی شامل IP Resolverها (هر خط یک IP) را لود می‌کند (خط‌های تکراری خودکار حذف می‌شوند)
- به‌صورت همزمان (parallel) آن‌ها را تست می‌کند و فقط موارد سالم را نگه می‌دارد
- به شما اجازه می‌دهد انتخاب کنید کدام Resolverها فعال شوند
- پراکسی‌های لوکال اجرا می‌کند:
  - `SOCKS5` روی `0.0.0.0:1080`
  - `HTTP` روی `0.0.0.0:8080`
- ترافیک را از طریق مجموعه Resolverهای فعال با بالانس بار و مدیریت خطا عبور می‌دهد

## پیش‌نیازها

- ویندوز (نیاز به `slipnet.exe` دارد که از https://github.com/anonvector/SlipNet/releases/ قابل دانلود است)
- پایتون 3.9 یا بالاتر (پیشنهادی)
- پکیج پایتون: `requests`
- فایل `slipnet.exe` باید در همان پوشه `Slipnet Balanced Client.py` باشد

نصب dependency:

pip install requests

## فایل‌ها

- `Slipnet Balanced Client.py` - برنامه GUI و منطق پراکسی
- `slipnet.exe` - فایل اجرایی بک‌اند برای تونل کردن Resolverها

## نحوه استفاده

1. فایل exe را اجرا کنید یا با پایتون اجرا کنید:

python "Slipnet Balanced Client.py"

2. در UI روی Browse IP List کلیک کنید و فایل لیست را انتخاب کنید
3. مقدار Slipnet connection string را وارد کنید
4. مقدار Workers را تنظیم کنید و Start Scan را بزنید
5. Resolverهای سالم را انتخاب کنید
6. روی Activate Selected کلیک کنید
7. از پراکسی‌ها استفاده کنید:
   - SOCKS5: your_local_ip:1080
   - HTTP: your_local_ip:8080

## فرمت فایل Resolver

- فایل متنی ساده
- هر خط یک Resolver
- خط‌های خالی نادیده گرفته می‌شوند
- موارد تکراری حذف می‌شوند

مثال:

1.1.1.1
8.8.8.8
9.9.9.9

## نکات

- وضعیت‌ها: OK / FAIL / PENDING
- Resolverهای خراب حذف نمی‌شوند، فقط علامت‌گذاری می‌شوند
- سیستم بالانسر Resolverهای ناپایدار را موقتاً غیرفعال می‌کند
- Resolverهای سالم دوباره به‌مرور برمی‌گردند

## توجه

- از connection stringهای encode شده پشتیبانی نمی‌شود
- استفاده فقط در محیط‌های مجاز
