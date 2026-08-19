<div align="center">

# 📈 GetSignal AI

**Autonomous trading agent for Hyperliquid perpetuals**

[![Channel](https://img.shields.io/badge/Telegram-@GetSignalAI-229ED9?style=flat-square&logo=telegram)](https://t.me/GetSignalAI)
![Exchange](https://img.shields.io/badge/Exchange-Hyperliquid-000?style=flat-square)
![Mode](https://img.shields.io/badge/Mode-Testnet-orange?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-2ea44f?style=flat-square)

[![English](https://img.shields.io/badge/lang-English-2ea44f?style=for-the-badge)](#english)
[![فارسی](https://img.shields.io/badge/زبان-فارسی-2ea44f?style=for-the-badge)](#فارسی)

</div>

---

<a name="english"></a>

## What it does

Scans 20 liquid perpetuals every hour, opens positions with automatic stop-loss
and take-profit, posts each signal to a public Telegram channel, and updates
that message live until the trade closes. Every night it reviews its own
results and can rewrite its parameters and code.

---

## Strategy

**Mean reversion at extremes.** Crypto overshoots on liquidation cascades; the
snap back is tradeable, but only when no trend is driving the move.

A position opens when all three hold on a closed 1h candle:

| Condition | Threshold |
|---|---|
| RSI at an extreme | ≤ 25 (long) · ≥ 75 (short) |
| Price stretched from its mean | ≥ 1.5 ATR from EMA(100) |
| Market not trending | ADX < 25 |

**Exit — progressive risk-free ratchet.** At +1R the resting take-profit is
cancelled, the stop locks at +1R, and from there it ratchets up 0.25R for every
0.25R gained. The position exits only when that stop is taken out, so once armed
the trade cannot return less than +1R. A take-profit still rests at 3R purely as
a backstop in case the bot dies mid-trade — it is not the intended exit.

---

## Risk

| Parameter | Value |
|---|---|
| Risk per trade | 1% of account |
| Max concurrent positions | 2 |
| Max leveraged loss per trade | 20% |
| Take-profit backstop | 3R |
| Max leverage | 25x |
| Stop-loss | 1.5 × ATR |
| Timeframe | 1h |

Position size is derived from stop distance (`notional = risk / stop%`), so
every stop-out costs the same 1% regardless of how wide the stop is.

---

## Live results

Real trades on the exchange. Every one is posted to the channel when it opens,
updated while it runs, and left there when it closes — including the losers.

| Metric | Value |
|---|---|
| Closed trades | 5 |
| Win rate | 60% (3W / 2L) |
| Total | +7.14% leveraged · +$5.46 |
| Avg R:R | +0.16R |
| Since | 2026-07-27 |

**Five trades proves nothing.** It is posted because it is real, not because it
is significant. Judge this again at 30+.

---

## Backtest

20 coins, ~7 months, exchange fees included, one position per coin, capped at 2
concurrent — the same limits the live bot runs under.

| Model | Net (account) | Win rate |
|---|---|---|
| Optimistic fill | +40.5% | ~58% |
| Pessimistic fill | +14.5% | ~54% |

The two rows bracket how a trailing stop can fill in reality: the ratchet raises
the stop the moment price prints a level, and whether that stop then survives
the same candle is not knowable from hourly bars. Live execution sits somewhere
between them.

> Earlier versions of this file quoted 67.7% win rate and −4.2% drawdown. Those
> came from a backtest that drove the ratchet off bar highs, which credits wicks
> the bot could never have filled, and from Heikin-Ashi rather than real prices.
> Both were wrong and the figures are void. The numbers above are what survived
> the correction.

---

## Architecture

```
live.py         main loop, entries, stop ratchet, nightly triggers
strategy2.py    signal logic and parameters
executor.py     Hyperliquid order placement
tracker.py      live message updates, dashboard, closed-trade stats
backtest2.py    single-coin backtest engine
portfolio2.py   portfolio simulation with the live concurrency cap
sweep_*.py      parameter sweeps (TP, ratchet, fill model)
test_ratchet.py unit tests for the exit ladder
s2_report.py    per-trade CSV + monthly report export
indicators.py   RSI, ATR, ADX, EMA, order blocks, liquidity pools
tg.py           Telegram formatting
journal.py      trade history
review.py       nightly/weekly review and parameter tuner
ai_brain.py     nightly AI code review
```

`trader.py` and `backtest.py` hold a retired liquidity-pool strategy, kept
switchable via `S1_ENABLED` in `live.py`.

---

## Setup

```bash
pip install hyperliquid-python-sdk eth-account loguru requests pandas numpy

cp config.example.py config.py    # then fill in your keys
python live.py
```

`config.py` is gitignored and never committed.

```python
HYPERLIQUID_PRIVATE_KEY = "0x..."
HYPERLIQUID_ACCOUNT     = "0x..."
USE_TESTNET             = True

TELEGRAM_TOKEN          = "..."
TELEGRAM_CHANNEL        = "@YourChannel"
TELEGRAM_OWNER_ID       = "..."
```

---

## Nightly self-learning

At 02:00 UTC the agent reviews its own performance, tunes parameters against
its trade history, and validates every proposed code change (syntax check,
secrets untouched) before applying it. Changes are versioned and pushed with a
changelog; the bot restarts itself only when no position is open.

---

## License

MIT — see [LICENSE](LICENSE).

---

<div dir="rtl">
<a name="فارسی"></a>

## فارسی — چه کار می کند

هر ساعت ۲۰ ارز دیجیتال نقدشونده را بررسی می کند، با حد ضرر و حد سود خودکار
پوزیشن باز می کند، هر سیگنال را در کانال عمومی تلگرام منتشر می کند و آن پیام
را تا بسته شدن معامله زنده به روز نگه می دارد. هر شب نتایج خودش را مرور
می کند و می تواند پارامترها و کدش را بازنویسی کند.

---

## استراتژی

**بازگشت به میانگین در نقاط اشباع.** قیمت در آبشار لیکویید شدن بیش از حد پرت
می شود و بازگشتش قابل معامله است، ولی فقط وقتی روند قوی پشتش نباشد.

پوزیشن وقتی باز می شود که هر سه شرط روی کندل بسته شده یک ساعته برقرار باشد:

| شرط | مقدار |
|---|---|
| RSI در نقطه اشباع | زیر ۲۵ برای خرید · بالای ۷۵ برای فروش |
| فاصله قیمت از میانگین | حداقل ۱.۵ برابر ATR از EMA صد |
| بازار بدون روند | ADX زیر ۲۵ |

**خروج با نردبان ریسک فری.** در سود یک آر، حد سود لغو می شود و استاپ روی همان
یک آر قفل می شود. از آنجا به بعد به ازای هر ربع آر سود بیشتر، استاپ یک پله بالا
می رود. خروج فقط وقتی اتفاق می افتد که استاپ بخورد، پس بعد از مسلح شدن معامله
نمی تواند کمتر از یک آر برگرداند. یک حد سود روی سه آر هم باقی می ماند، فقط به
عنوان پشتیبان اگر ربات وسط معامله از کار بیفتد. آن مسیر خروج اصلی نیست.

---

## مدیریت ریسک

| پارامتر | مقدار |
|---|---|
| ریسک هر معامله | ۱ درصد حساب |
| حداکثر پوزیشن همزمان | ۲ |
| حداکثر ضرر اهرمی هر معامله | ۲۰ درصد |
| حد سود پشتیبان | ۳ آر |
| حداکثر اهرم | ۲۵ برابر |
| حد ضرر | ۱.۵ برابر ATR |
| تایم فریم | یک ساعته |

حجم پوزیشن از فاصله استاپ محاسبه می شود، پس هر بار که استاپ بخورد دقیقا همان
یک درصد حساب از دست می رود، فارغ از اینکه استاپ چقدر دور باشد.

---

## نتایج واقعی

معاملات واقعی روی صرافی. هر کدام موقع باز شدن در کانال منتشر می شود، در طول
معامله به روز می شود، و بعد از بسته شدن همانجا می ماند. از جمله ضررها.

| معیار | مقدار |
|---|---|
| معاملات بسته شده | ۵ |
| وین ریت | ۶۰ درصد (۳ برد / ۲ باخت) |
| مجموع | ۷.۱۴ درصد اهرمی · ۵.۴۶ دلار |
| میانگین R:R | ۰.۱۶ |
| از تاریخ | ۲۰۲۶-۰۷-۲۷ |

**پنج معامله چیزی را ثابت نمی کند.** منتشر شده چون واقعی است، نه چون معنادار
است. بعد از سی معامله دوباره قضاوت کنید.

---

## بک تست

بیست ارز، حدود هفت ماه، با کارمزد صرافی، یک پوزیشن در هر ارز، حداکثر دو پوزیشن
همزمان. همان محدودیت هایی که ربات زنده با آن کار می کند.

| مدل | سود خالص حساب | وین ریت |
|---|---|---|
| خوش بینانه | ۴۰.۵ درصد | حدود ۵۸ درصد |
| بدبینانه | ۱۴.۵ درصد | حدود ۵۴ درصد |

این دو ردیف بازه ای را نشان می دهند که پر شدن استاپ متحرک در واقعیت می تواند
داشته باشد. نردبان به محض اینکه قیمت یک سطح را بزند استاپ را بالا می برد، و
اینکه آن استاپ در همان کندل دوام می آورد یا نه از داده ساعتی قابل فهمیدن نیست.
اجرای واقعی جایی بین این دو است.

> نسخه های قبلی این فایل وین ریت ۶۷.۷ درصد و افت ۴.۲ درصد را نوشته بودند. آن
> اعداد از بک تستی می آمدند که نردبان را از سقف کندل حساب می کرد، یعنی سایه هایی
> را برد می شمرد که ربات هرگز نمی توانست رویشان پر کند، و روی قیمت هیکن اشی بود
> نه قیمت واقعی. هر دو غلط بودند و آن اعداد باطل هستند. جدول بالا چیزی است که
> بعد از اصلاح باقی ماند.

---

## معماری

</div>

```
live.py         حلقه اصلی، ورودها، نردبان استاپ، تریگرهای شبانه
strategy2.py    منطق سیگنال و پارامترها
executor.py     ثبت سفارش در Hyperliquid
tracker.py      به روزرسانی پیام زنده، داشبورد، آمار معاملات بسته
backtest2.py    موتور بک تست تک ارزی
portfolio2.py   شبیه سازی سبد با محدودیت پوزیشن همزمان
sweep_*.py      جاروب پارامترها (هدف، نردبان، مدل پر شدن)
test_ratchet.py تست واحد نردبان خروج
s2_report.py    خروجی CSV هر معامله و گزارش ماهانه
indicators.py   اندیکاتورها شامل RSI و ATR و ADX و EMA
tg.py           فرمت بندی پیام تلگرام
journal.py      تاریخچه معاملات
review.py       بررسی شبانه و هفتگی و تنظیم پارامتر
ai_brain.py     بازبینی شبانه کد با هوش مصنوعی
```

<div dir="rtl">

فایل های `trader.py` و `backtest.py` استراتژی بازنشسته لیکوییدیتی پول را نگه
داشته اند که با کلید `S1_ENABLED` در `live.py` قابل روشن کردن است.

---

## راه اندازی

</div>

```bash
pip install hyperliquid-python-sdk eth-account loguru requests pandas numpy

cp config.example.py config.py    # سپس کلیدهای خود را وارد کنید
python live.py
```

<div dir="rtl">

فایل `config.py` در gitignore است و هرگز کامیت نمی شود. کلیدهای Hyperliquid و
تلگرام را در همان فایل وارد کنید (`HYPERLIQUID_PRIVATE_KEY`، `TELEGRAM_TOKEN` و …).

---

## یادگیری شبانه

ساعت دو بامداد به وقت UTC ربات عملکرد خودش را مرور می کند، پارامترها را بر
اساس تاریخچه معاملاتش تنظیم می کند، و هر تغییر کد پیشنهادی را قبل از اعمال
اعتبارسنجی می کند. تغییرات نسخه بندی و پوش می شوند و ربات فقط وقتی خودش را
ری استارت می کند که هیچ پوزیشن بازی نداشته باشد.

</div>

---

<div align="center"><sub>

Testnet. Not financial advice. · تست‌نت. این توصیه مالی نیست.

</sub></div>
