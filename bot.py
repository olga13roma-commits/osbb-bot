"""
ОСББ Софіївське 2009 — Telegram бот для збору показників лічильників води
Автор: Claude Sonnet 4.6
"""

import os
import logging
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

# ──────────────────────────────────────────────
# Логування
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Стани розмови
# ──────────────────────────────────────────────
ENTER_CODE, ENTER_PREV, ENTER_CURR, CONFIRM = range(4)

# ──────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheets_client():
    """Підключення до Google Sheets через сервісний акаунт."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
    else:
        # Для локального запуску — файл поряд з bot.py
        with open("credentials.json", "r", encoding="utf-8") as f:
            creds_dict = json.load(f)

    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet():
    """Відкрити таблицю за ID з налаштувань."""
    client = get_sheets_client()
    sheet_id = os.environ.get("SPREADSHEET_ID", "1P1qxqLGnC8dTf9TBeUqkwHDZEiax_eDxgE_vMqKjbJQ")
    return client.open_by_key(sheet_id)


# ──────────────────────────────────────────────
# Допоміжні функції
# ──────────────────────────────────────────────

def get_settings(spreadsheet) -> dict:
    """Зчитати налаштування з листа «Налаштування»."""
    ws = spreadsheet.worksheet("Налаштування")
    rows = ws.get_all_values()
    settings = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            settings[row[0].strip()] = row[1].strip()
    return settings


def get_apartment_by_code(spreadsheet, code: str) -> dict | None:
    """
    Знайти квартиру за кодом доступу (КВ-001 і т.д.).
    Повертає словник з даними або None.
    """
    code = code.strip().upper()
    ws = spreadsheet.worksheet("Квартири")
    rows = ws.get_all_records()  # список dict з заголовками
    for row in rows:
        if str(row.get("Код доступу", "")).strip().upper() == code:
            return row
    return None


def already_submitted_this_month(spreadsheet, apt_number: str, month: str) -> bool:
    """Перевірити, чи квартира вже подала показники за цей місяць."""
    ws = spreadsheet.worksheet("Показники")
    rows = ws.get_all_records()
    for row in rows:
        if str(row.get("№кв", "")).strip() == str(apt_number).strip() \
                and str(row.get("Місяць", "")).strip() == month:
            return True
    return False


def save_reading(spreadsheet, data: dict):
    """Записати рядок у лист «Показники»."""
    ws = spreadsheet.worksheet("Показники")
    ws.append_row([
        data["month"],
        data["apt_number"],
        data["owner"],
        data["prev"],
        data["curr"],
        data["diff"],
        data["water_tariff"],
        data["water_sum"],
        data["kvarplata"],
        data["tpv"],
        data["electricity"],   # порожньо — вносить адмін
        data["to_pay"],
        data["prev_debt"],
        data["total"],
        data["date"],
        data["telegram_id"],
    ], value_input_option="USER_ENTERED")


def update_telegram_id(spreadsheet, code: str, telegram_id: int):
    """Запам'ятати Telegram ID мешканця у листі «Квартири» (колонка H)."""
    ws = spreadsheet.worksheet("Квартири")
    cell = ws.find(code)
    if cell:
        # Колонка «Telegram ID» — 8-а (H)
        ws.update_cell(cell.row, 8, str(telegram_id))


def get_apartments_without_reading(spreadsheet, month: str) -> list[dict]:
    """Список активних квартир, які ще не подали показники за місяць."""
    apts_ws = spreadsheet.worksheet("Квартири")
    readings_ws = spreadsheet.worksheet("Показники")

    all_apts = apts_ws.get_all_records()
    all_readings = readings_ws.get_all_records()

    submitted = {
        str(r["№кв"]).strip()
        for r in all_readings
        if str(r.get("Місяць", "")).strip() == month
    }

    missing = []
    for apt in all_apts:
        active = str(apt.get("Активна", "")).strip().upper()
        if active not in ("ТАК", "TRUE", "1", "YES"):
            continue
        apt_no = str(apt.get("№кв", "")).strip()
        if apt_no not in submitted:
            missing.append(apt)
    return missing


# ──────────────────────────────────────────────
# Команди
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю! Це бот ОСББ Софіївське 2009.\n\n"
        "Щоб передати показники лічильника холодної води — введіть /meter\n"
        "Якщо потрібна допомога — /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Як користуватись ботом:*\n\n"
        "1. Введіть /meter\n"
        "2. Введіть ваш код доступу (наприклад КВ-047)\n"
        "3. Введіть попередній показник лічильника\n"
        "4. Введіть поточний показник лічильника\n"
        "5. Отримайте рахунок 📄\n\n"
        "❓ Код доступу можна дізнатись у голови ОСББ.",
        parse_mode="Markdown"
    )


# ──────────────────────────────────────────────
# Розмова: введення показників
# ──────────────────────────────────────────────

async def pokaznyky_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Введіть ваш *код доступу* (наприклад: КВ-047):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ENTER_CODE


async def enter_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    telegram_id = update.effective_user.id

    try:
        ss = open_spreadsheet()
        settings = get_settings(ss)
        apt = get_apartment_by_code(ss, code)
    except Exception as e:
        logger.error(f"Помилка підключення до Sheets: {e}")
        await update.message.reply_text(
            "⚠️ Не вдалося підключитись до бази даних. Спробуйте пізніше."
        )
        return ConversationHandler.END

    if apt is None:
        await update.message.reply_text(
            "❌ Код не знайдено. Перевірте код і спробуйте ще раз.\n"
            "Або натисніть /скасувати"
        )
        return ENTER_CODE

    active = str(apt.get("Активна", "")).strip().upper()
    if active not in ("ТАК", "TRUE", "1", "YES"):
        await update.message.reply_text(
            "❌ Цей код неактивний. Зверніться до голови ОСББ."
        )
        return ConversationHandler.END

    month = settings.get("Місяць поточний", datetime.now().strftime("%Y-%m"))

    if already_submitted_this_month(ss, str(apt.get("№кв", "")), month):
        await update.message.reply_text(
            f"✅ Показники за *{month}* для кв. {apt.get('№кв')} вже було подано.\n\n"
            "Якщо це помилка — зверніться до голови ОСББ.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Зберігаємо дані в context
    context.user_data["code"] = code
    context.user_data["apt"] = apt
    context.user_data["settings"] = settings
    context.user_data["ss_id"] = ss.id if hasattr(ss, 'id') else None
    context.user_data["month"] = month

    # Оновити Telegram ID
    try:
        update_telegram_id(ss, code, telegram_id)
    except Exception:
        pass

    owner = apt.get("Власник", "")
    apt_no = apt.get("№кв", "")
    debt = float(apt.get("Борг на 01.10.2026", 0) or 0)

    msg = f"✅ Квартира *{apt_no}* — {owner}\n\n"
    if debt > 0:
        msg += f"⚠️ Борг за попередні місяці: *{debt:.2f} грн*\n\n"
    elif debt < 0:
        msg += f"💚 Переплата: *{abs(debt):.2f} грн* (буде зараховано)\n\n"

    msg += "Введіть *попередній* показник лічильника холодної води (з квитанції минулого місяця):"

    await update.message.reply_text(msg, parse_mode="Markdown")
    return ENTER_PREV


async def enter_prev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        prev = float(text)
        if prev < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Невірний формат. Введіть число, наприклад: 123.45"
        )
        return ENTER_PREV

    context.user_data["prev"] = prev
    await update.message.reply_text(
        "Введіть *поточний* показник лічильника (що зараз показує лічильник):",
        parse_mode="Markdown"
    )
    return ENTER_CURR


async def enter_curr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        curr = float(text)
        if curr < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Невірний формат. Введіть число, наприклад: 125.80"
        )
        return ENTER_CURR

    prev = context.user_data["prev"]
    if curr < prev:
        await update.message.reply_text(
            f"❌ Поточний показник ({curr}) не може бути меншим за попередній ({prev}).\n"
            "Перевірте і введіть ще раз:"
        )
        return ENTER_CURR

    context.user_data["curr"] = curr

    # ── Розрахунок ──
    apt = context.user_data["apt"]
    settings = context.user_data["settings"]

    diff = round(curr - prev, 3)

    try:
        water_tariff = float(settings.get("Тариф вода", 63.56))
    except Exception:
        water_tariff = 63.56

    try:
        kvarplata_tariff = float(settings.get("Тариф квартплата", 3.5))
    except Exception:
        kvarplata_tariff = 3.5

    try:
        tpv_default = float(settings.get("ТПВ", 45))
    except Exception:
        tpv_default = 45.0

    area = float(apt.get("Площа м²", 0) or apt.get("Площа", 0) or 0)
    tpv_apt = apt.get("ТПВ", tpv_default)
    try:
        tpv = float(tpv_apt) if tpv_apt != "" else tpv_default
    except Exception:
        tpv = tpv_default

    # Тариф квартплати може бути індивідуальним (колонка «Тариф квартплата»)
    tariff_kv = apt.get("Тариф квартплата", kvarplata_tariff)
    try:
        tariff_kv = float(tariff_kv) if tariff_kv != "" else kvarplata_tariff
    except Exception:
        tariff_kv = kvarplata_tariff

    water_sum = round(diff * water_tariff, 2)
    kvarplata = round(area * tariff_kv, 2)

    # Борг (позитивне = має заплатити, негативне = переплата)
    debt_raw = apt.get("Борг на 01.10.2026", 0)
    try:
        prev_debt = float(debt_raw) if debt_raw != "" else 0.0
    except Exception:
        prev_debt = 0.0

    to_pay = round(water_sum + kvarplata + tpv, 2)
    total = round(to_pay + prev_debt, 2)

    context.user_data["calc"] = {
        "diff": diff,
        "water_tariff": water_tariff,
        "water_sum": water_sum,
        "kvarplata": kvarplata,
        "tpv": tpv,
        "prev_debt": prev_debt,
        "to_pay": to_pay,
        "total": total,
    }

    # ── Підсумок для підтвердження ──
    apt_no = apt.get("№кв", "")
    month = context.user_data["month"]

    lines = [
        f"📄 *Рахунок за {month} — кв. {apt_no}*\n",
        f"Показники: {prev} → {curr} = *{diff} м³*",
        f"Вода: {diff} × {water_tariff} = *{water_sum:.2f} грн*",
        f"Квартплата ({area} м²): *{kvarplata:.2f} грн*",
        f"ТПВ: *{tpv:.2f} грн*",
    ]

    if prev_debt > 0:
        lines.append(f"Борг з попередніх місяців: *+{prev_debt:.2f} грн*")
    elif prev_debt < 0:
        lines.append(f"Переплата (зараховується): *{prev_debt:.2f} грн*")

    lines.append(f"\n💳 *До сплати: {total:.2f} грн*")

    lines.append("\n✅ Підтверджуєте введення?")

    keyboard = [["✅ Так, підтверджую", "❌ Скасувати"]]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "Скасувати" in text or "❌" in text:
        await update.message.reply_text(
            "Введення скасовано. Якщо потрібно — починайте знову: /meter",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    if "Так" not in text and "✅" not in text:
        await update.message.reply_text("Будь ласка, натисніть одну з кнопок.")
        return CONFIRM

    # ── Записати у Sheets ──
    apt = context.user_data["apt"]
    calc = context.user_data["calc"]
    month = context.user_data["month"]

    try:
        ss = open_spreadsheet()
        save_reading(ss, {
            "month": month,
            "apt_number": str(apt.get("№кв", "")),
            "owner": apt.get("Власник", ""),
            "prev": context.user_data["prev"],
            "curr": context.user_data["curr"],
            "diff": calc["diff"],
            "water_tariff": calc["water_tariff"],
            "water_sum": calc["water_sum"],
            "kvarplata": calc["kvarplata"],
            "tpv": calc["tpv"],
            "electricity": "",
            "to_pay": calc["to_pay"],
            "prev_debt": calc["prev_debt"],
            "total": calc["total"],
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "telegram_id": str(update.effective_user.id),
        })
    except Exception as e:
        logger.error(f"Помилка запису у Sheets: {e}")
        await update.message.reply_text(
            "⚠️ Показники прийнято, але виникла помилка при записі. "
            "Зверніться до голови ОСББ.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    apt_no = apt.get("№кв", "")
    total = calc["total"]

    deadline = context.user_data["settings"].get("Крайній термін", "25")
    _, month_num = month.split("-") if "-" in month else ("", "")
    month_names = {
        "01": "січня", "02": "лютого", "03": "березня", "04": "квітня",
        "05": "травня", "06": "червня", "07": "липня", "08": "серпня",
        "09": "вересня", "10": "жовтня", "11": "листопада", "12": "грудня"
    }
    month_name = month_names.get(month_num, month_num)

    await update.message.reply_text(
        f"✅ *Показники прийнято!*\n\n"
        f"Квартира {apt_no}, {month}\n"
        f"💳 *До сплати: {total:.2f} грн*\n\n"
        f"Оплатіть до {deadline} {month_name}.\n"
        f"Дякуємо! 🏠",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введення скасовано.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# Адмін-команди
# ──────────────────────────────────────────────

def is_admin(update: Update, settings: dict) -> bool:
    admin_id = settings.get("ID адміністратора", "")
    try:
        return str(update.effective_user.id) == str(int(admin_id))
    except Exception:
        return False


async def zvit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /звіт — хто подав / хто не подав показники за поточний місяць.
    Тільки для адміна.
    """
    try:
        ss = open_spreadsheet()
        settings = get_settings(ss)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка підключення: {e}")
        return

    if not is_admin(update, settings):
        await update.message.reply_text("🔒 Ця команда доступна лише адміністратору.")
        return

    month = settings.get("Місяць поточний", datetime.now().strftime("%Y-%m"))

    readings_ws = ss.worksheet("Показники")
    apts_ws = ss.worksheet("Квартири")

    all_readings = readings_ws.get_all_records()
    all_apts = apts_ws.get_all_records()

    submitted = {
        str(r["№кв"]).strip()
        for r in all_readings
        if str(r.get("Місяць", "")).strip() == month
    }

    active_apts = [
        a for a in all_apts
        if str(a.get("Активна", "")).strip().upper() in ("ТАК", "TRUE", "1", "YES")
    ]
    total_active = len(active_apts)
    submitted_count = len([a for a in active_apts if str(a.get("№кв", "")).strip() in submitted])
    missing_count = total_active - submitted_count

    msg = (
        f"📊 *Звіт за {month}*\n\n"
        f"✅ Подали показники: {submitted_count} з {total_active}\n"
        f"❌ Ще не подали: {missing_count}\n\n"
    )

    if missing_count > 0 and missing_count <= 30:
        missing_apts = [
            a for a in active_apts
            if str(a.get("№кв", "")).strip() not in submitted
        ]
        numbers = ", ".join(str(a.get("№кв", "")) for a in missing_apts[:30])
        msg += f"Квартири без показників:\n{numbers}"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def nagaduvannya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /нагадування — надіслати нагадування тим, хто не подав.
    Тільки для адміна.
    """
    try:
        ss = open_spreadsheet()
        settings = get_settings(ss)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка підключення: {e}")
        return

    if not is_admin(update, settings):
        await update.message.reply_text("🔒 Ця команда доступна лише адміністратору.")
        return

    month = settings.get("Місяць поточний", datetime.now().strftime("%Y-%m"))
    deadline = settings.get("Крайній термін", "25")

    missing = get_apartments_without_reading(ss, month)

    sent = 0
    no_id = 0

    for apt in missing:
        tg_id = str(apt.get("Telegram ID", "")).strip()
        if not tg_id or tg_id in ("", "0"):
            no_id += 1
            continue
        try:
            apt_no = apt.get("№кв", "")
            await context.bot.send_message(
                chat_id=int(tg_id),
                text=(
                    f"📢 *Нагадування ОСББ Софіївське 2009*\n\n"
                    f"Квартира {apt_no} ще не подала показники лічильника води за *{month}*.\n\n"
                    f"⏰ Крайній термін: *{deadline} числа*.\n\n"
                    "Будь ласка, введіть показники: /meter"
                ),
                parse_mode="Markdown"
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати нагадування кв. {apt.get('№кв')}: {e}")
            no_id += 1

    await update.message.reply_text(
        f"📨 Нагадування надіслано: *{sent}*\n"
        f"❓ Без Telegram ID (не можу надіслати): *{no_id}*",
        parse_mode="Markdown"
    )


async def set_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /місяць YYYY-MM — встановити поточний місяць у налаштуваннях.
    Тільки для адміна.
    """
    try:
        ss = open_spreadsheet()
        settings = get_settings(ss)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка підключення: {e}")
        return

    if not is_admin(update, settings):
        await update.message.reply_text("🔒 Ця команда доступна лише адміністратору.")
        return

    args = context.args
    if not args:
        current = settings.get("Місяць поточний", "не встановлено")
        await update.message.reply_text(
            f"Поточний місяць: *{current}*\n\n"
            "Щоб змінити: /month 2026-11",
            parse_mode="Markdown"
        )
        return

    new_month = args[0].strip()
    try:
        datetime.strptime(new_month, "%Y-%m")
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Приклад: /month 2026-11")
        return

    ws = ss.worksheet("Налаштування")
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Місяць поточний":
            ws.update_cell(i + 1, 2, new_month)
            await update.message.reply_text(
                f"✅ Місяць оновлено: *{new_month}*",
                parse_mode="Markdown"
            )
            return

    await update.message.reply_text("⚠️ Рядок «Місяць поточний» не знайдено у налаштуваннях.")


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        # Для локального тесту — читаємо з credentials.json або .env
        try:
            with open("credentials.json", "r", encoding="utf-8") as f:
                creds_data = json.load(f)
            # Токен не в JSON сервісного акаунту — потрібна змінна середовища
        except Exception:
            pass
        raise RuntimeError(
            "TELEGRAM_TOKEN не встановлено. "
            "Додайте змінну середовища TELEGRAM_TOKEN."
        )

    app = Application.builder().token(token).build()

    # Розмова: введення показників
    conv = ConversationHandler(
        entry_points=[CommandHandler("meter", pokaznyky_start)],
        states={
            ENTER_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_code)],
            ENTER_PREV: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_prev)],
            ENTER_CURR: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_curr)],
            CONFIRM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv)

    # Адмін-команди
    app.add_handler(CommandHandler("report", zvit))
    app.add_handler(CommandHandler("remind", nagaduvannya))
    app.add_handler(CommandHandler("month", set_month))

    logger.info("Бот запущено")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
