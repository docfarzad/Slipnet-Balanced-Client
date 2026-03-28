# Slipnet Balanced Client (ترجمه فارسی)

یک ابزار ساده دسکتاپ برای پیدا کردن DNS Resolverهای سالم برای
slipnet.exe و سپس اجرای یک مجموعه پروکسی متعادل به صورت محلی.

## این ابزار چه کار می‌کند

-   یک فایل متنی شامل IPهای Resolver را می‌خواند (هر خط یک IP). خطوط
    تکراری حذف می‌شوند.
-   بررسی همزمان (parallel) و نگه داشتن فقط موارد سالم
-   امکان انتخاب Resolverهای سالم برای فعال‌سازی
-   اجرای پروکسی: SOCKS5 روی 0.0.0.0:1080 HTTP روی 0.0.0.0:8080
-   عبور ترافیک با لود بالانس و مدیریت خطا

## پیش‌نیازها

-   ویندوز
-   Python 3.9+
-   requests
-   وجود slipnet.exe در کنار فایل برنامه

نصب: pip install requests

## نحوه استفاده

1.  اجرای برنامه: python "Slipnet Balanced Client.py"

2.  انتخاب فایل IP

3.  وارد کردن connection string

4.  تنظیم Workers و Start Scan

5.  انتخاب Resolverهای سالم

6.  Activate Selected

7.  استفاده از: SOCKS5: your_local_ip:1080 HTTP: your_local_ip:8080

## هشدار

فقط در صورت داشتن مجوز استفاده کنید.
