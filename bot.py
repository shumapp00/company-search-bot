import os
import json
import csv
import re
import tempfile
from datetime import datetime
from typing import List, Dict

from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web
import asyncio

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# API-ключи для поисковиков РФ
YANDEX_MAPS_API_KEY = os.getenv("YANDEX_MAPS_API_KEY")
TWOGIS_API_KEY = os.getenv("TWOGIS_API_KEY", "rujrdl8776")

# ========== GOOGLE SHEETS ==========
google_key_content = os.getenv("GOOGLE_KEY_JSON")
if google_key_content:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(google_key_content)
        os.environ["GOOGLE_KEY_PATH"] = f.name

sheet = None
key_files = [
    os.getenv("GOOGLE_KEY_PATH"),
    "google_key.json",
    "google_key.json.json"
]

for f in key_files:
    if f and os.path.exists(f):
        try:
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = Credentials.from_service_account_file(f, scopes=scope)
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
            print("✓ Google OK")
            break
        except Exception as e:
            print(f"⚠️ Google error: {e}")

# ========== TELEGRAM ==========
session = AiohttpSession()
bot = Bot(token=TELEGRAM_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
user_data: Dict[int, Dict] = {}

# ========== СОСТОЯНИЯ БОТА (FSM) ==========
class Form(StatesGroup):
    waiting_for_city = State()
    waiting_for_niche = State()
    waiting_for_quantity = State()

# ========== КЛАВИАТУРЫ ==========
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="🔍 Начать парсинг")],
        [types.KeyboardButton(text="📊 Выгрузить в Google Таблицу")],
        [types.KeyboardButton(text="📋 Показать последний результат")]
    ],
    resize_keyboard=True
)

quantity_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="3 компании")],
        [types.KeyboardButton(text="5 компаний")],
        [types.KeyboardButton(text="10 компаний")]
    ],
    resize_keyboard=True
)

# ========== ПОИСКОВЫЙ ДВИЖОК С DUCKDUCKGO ==========
class SearchEngineRF:
    """Поиск компаний через Яндекс.Карты, 2GIS и DuckDuckGo"""

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Приводит телефон к формату +7XXXXXXXXXX"""
        digits = re.sub(r'\D', '', phone)
        if digits.startswith('8') and len(digits) == 11:
            digits = '7' + digits[1:]
        if digits.startswith('7') and len(digits) == 11:
            return f"+{digits}"
        return phone

    @staticmethod
    def _extract_phone_from_text(text: str) -> str:
        """Ищет телефон в любом тексте"""
        if not text:
            return ""
        patterns = [
            r'\+7[\s\(\)-]*\d{3}[\s\(\)-]*\d{3}[\s\(\)-]*\d{2}[\s\(\)-]*\d{2}',
            r'8[\s\(\)-]*\d{3}[\s\(\)-]*\d{3}[\s\(\)-]*\d{2}[\s\(\)-]*\d{2}',
            r'7[\s\(\)-]*\d{3}[\s\(\)-]*\d{3}[\s\(\)-]*\d{2}[\s\(\)-]*\d{2}'
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return SearchEngineRF._normalize_phone(match.group(0))
        return ""

    @staticmethod
    def _extract_website_from_text(text: str) -> str:
        """Ищет сайт в тексте"""
        if not text:
            return ""
        # Ищем https://domain.com
        match = re.search(r'https?://([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', text)
        if match:
            return match.group(1)
        # Ищем просто domain.ru
        match2 = re.search(r'([a-zA-Z0-9-]+\.(ru|com|net|org|info|biz|pro))', text, re.IGNORECASE)
        if match2:
            return match2.group(1)
        return ""

    @staticmethod
    def search_duckduckgo(city: str, niche: str, quantity: int) -> List[Dict]:
        """
        Поиск через DuckDuckGo - находит телефоны и сайты
        """
        query = f"{niche} {city} телефон"
        
        try:
            from duckduckgo_search import DDGS
            
            companies = []
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=quantity * 3)
                
                for r in results:
                    title = r.get("title", "")
                    body = r.get("body", "")
                    href = r.get("href", "")
                    
                    # Пропускаем агрегаторы
                    skip_domains = ["zoon.ru", "yell.ru", "2gis.ru", "yandex.ru", 
                                   "google.com", "tripadvisor", "instagram", "facebook",
                                   "vk.com", "ok.ru", "youtube.com"]
                    
                    if any(d in href.lower() for d in skip_domains):
                        continue
                    
                    # Ищем телефон в заголовке и описании
                    full_text = title + " " + body
                    phone = SearchEngineRF._extract_phone_from_text(full_text)
                    
                    # Ищем сайт
                    website = SearchEngineRF._extract_website_from_text(href)
                    if not website:
                        website = SearchEngineRF._extract_website_from_text(full_text)
                    
                    # Если нашли телефон или сайт - добавляем
                    if phone or website:
                        # Очищаем название
                        name = title.split("—")[0].split("-")[0].split("|")[0].strip()
                        if len(name) > 80:
                            name = name[:80]
                        
                        companies.append({
                            "business_name": name,
                            "website": website,
                            "social": "",
                            "phone": phone,
                            "address": ""
                        })
                        
                        if len(companies) >= quantity:
                            break
            
            print(f"✓ DuckDuckGo: найдено {len(companies)} с телефонами/сайтами")
            return companies
            
        except Exception as e:
            print(f"⚠️ DuckDuckGo error: {e}")
            return []

    @staticmethod
    def search_yandex_maps(city: str, niche: str, quantity: int) -> List[Dict]:
        """Поиск через Яндекс.Карты API (базовые данные)"""
        if not YANDEX_MAPS_API_KEY:
            return []

        try:
            geo_url = "https://geocode-maps.yandex.ru/1.x/"
            geo_params = {
                "apikey": YANDEX_MAPS_API_KEY,
                "geocode": city,
                "format": "json",
                "lang": "ru_RU"
            }
            geo_resp = requests.get(geo_url, params=geo_params, timeout=10)
            geo_data = geo_resp.json()

            features = geo_data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
            if not features:
                return []

            coords = features[0].get("GeoObject", {}).get("Point", {}).get("pos", "0 0").split()
            if len(coords) != 2:
                return []

            lon, lat = coords

            search_url = "https://search-maps.yandex.ru/v1/"
            search_params = {
                "apikey": YANDEX_MAPS_API_KEY,
                "text": niche,
                "ll": f"{lon},{lat}",
                "spn": "0.5,0.5",
                "lang": "ru_RU",
                "results": quantity,
                "type": "biz"
            }

            resp = requests.get(search_url, params=search_params, timeout=15)
            data = resp.json()

            companies = []
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                company = props.get("CompanyMetaData", {})

                name = company.get("name", "")
                if not name:
                    continue

                phones = company.get("Phones", [])
                phone = phones[0].get("formatted", "") if phones else ""
                url = company.get("url", "")
                address = props.get("description", "") or company.get("address", "")

                companies.append({
                    "business_name": name,
                    "website": url,
                    "social": "",
                    "phone": SearchEngineRF._normalize_phone(phone),
                    "address": address
                })

                if len(companies) >= quantity:
                    break

            print(f"✓ Yandex Maps: найдено {len(companies)}")
            return companies

        except Exception as e:
            print(f"⚠️ Yandex Maps error: {e}")
            return []

    @staticmethod
    def search_2gis(city: str, niche: str, quantity: int) -> List[Dict]:
        """Поиск через 2GIS API"""
        try:
            city_url = "https://catalog.api.2gis.com/2.0/region/search"
            city_resp = requests.get(
                city_url,
                params={"q": city, "key": TWOGIS_API_KEY, "locale": "ru_RU"},
                timeout=10
            )
            city_data = city_resp.json()

            items = city_data.get("result", {}).get("items", [])
            if not items:
                return []

            city_id = items[0].get("id")

            search_url = "https://catalog.api.2gis.com/3.0/items"
            params = {
                "q": niche,
                "region_id": city_id,
                "key": TWOGIS_API_KEY,
                "locale": "ru_RU",
                "type": "branch",
                "page_size": quantity + 5,
                "fields": "items.contact_groups,items.address"
            }

            resp = requests.get(search_url, params=params, timeout=15)
            data = resp.json()

            companies = []
            for item in data.get("result", {}).get("items", []):
                name = item.get("name", "")
                if not name:
                    continue

                phone = ""
                website = ""

                contacts = item.get("contact_groups", [{}])[0].get("contacts", [])
                for c in contacts:
                    ctype = c.get("type", "")
                    val = c.get("value", "")
                    if ctype == "phone" and not phone:
                        phone = SearchEngineRF._normalize_phone(val)
                    elif ctype == "website" and not website:
                        website = val

                address = item.get("address", {}).get("full", "") or item.get("address_name", "")

                companies.append({
                    "business_name": name,
                    "website": website,
                    "social": "",
                    "phone": phone,
                    "address": address
                })

                if len(companies) >= quantity:
                    break

            print(f"✓ 2GIS: найдено {len(companies)}")
            return companies

        except Exception as e:
            print(f"⚠️ 2GIS error: {e}")
            return []

    @classmethod
    def search_all(cls, city: str, niche: str, quantity: int) -> List[Dict]:
        """
        Комбинированный поиск:
        1. DuckDuckGo - телефоны и сайты
        2. Yandex Maps - название и адрес
        3. 2GIS - запасной
        """
        results = []
        seen_names = set()

        # 1. DuckDuckGo - ищем телефоны и сайты
        print(f"🔍 DuckDuckGo: ищу {niche} в {city}...")
        duck_results = cls.search_duckduckgo(city, niche, quantity)
        
        for duck in duck_results:
            name_lower = duck["business_name"].lower()
            if name_lower not in seen_names:
                seen_names.add(name_lower)
                results.append(duck)

        # 2. Yandex Maps - добавляем адреса
        if len(results) < quantity:
            print(f"🔍 Yandex Maps: ищу дополнительно...")
            yandex_maps = cls.search_yandex_maps(city, niche, quantity)
            
            for ym in yandex_maps:
                name_lower = ym["business_name"].lower()
                
                # Ищем, есть ли уже такая компания
                found = False
                for existing in results:
                    if name_lower in existing["business_name"].lower() or \
                       existing["business_name"].lower() in name_lower:
                        # Дополняем адресом
                        if not existing.get("address") and ym.get("address"):
                            existing["address"] = ym["address"]
                        found = True
                        break
                
                if not found and len(results) < quantity:
                    seen_names.add(name_lower)
                    results.append(ym)

        # 3. 2GIS - если всё ещё мало
        if len(results) < quantity:
            print(f"🔍 2GIS: ищу запасной вариант...")
            need_more = quantity - len(results)
            twogis = cls.search_2gis(city, niche, need_more)
            
            for tg in twogis:
                name_lower = tg["business_name"].lower()
                if name_lower not in seen_names:
                    seen_names.add(name_lower)
                    results.append(tg)

        print(f"✓ Итого: {len(results)} компаний")
        return results[:quantity]

# ========== ФУНКЦИИ СОХРАНЕНИЯ ==========
def save_to_google(city: str, niche: str, companies: List[Dict]) -> bool:
    """Сохраняет в Google Таблицу"""
    global sheet
    if sheet is None:
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        for c in companies:
            sheet.append_row([
                city,
                niche,
                c.get("business_name", ""),
                "",
                c.get("website", ""),
                c.get("social", ""),
                c.get("phone", ""),
                c.get("address", "")[:100],
                now
            ])
        return True
    except Exception as e:
        print(f"⚠️ Google save error: {e}")
        return False

def save_to_csv(city: str, niche: str, companies: List[Dict]) -> str:
    """Сохраняет в CSV файл"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    filename = f"result_{city}_{niche}.csv".replace(" ", "_").replace("/", "_")

    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Город", "Ниша", "Название", "Тип", "Сайт", "Соцсети", "Телефон", "Адрес", "Дата"])

        for c in companies:
            writer.writerow([
                city,
                niche,
                c.get("business_name", ""),
                "",
                c.get("website", ""),
                c.get("social", ""),
                c.get("phone", ""),
                c.get("address", ""),
                now
            ])

    return filename

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    """Приветствие и главное меню"""
    await state.clear()
    await message.answer(
        "👋 <b>Бот поиска компаний</b>\n\n"
        "🔍 Я ищу реальные компании через:\n"
        "   • DuckDuckGo (телефоны, сайты)\n"
        "   • Яндекс.Карты (адреса)\n"
        "   • 2GIS (запасной)\n\n"
        "📊 Собираю: телефоны, сайты, адреса\n\n"
        "<b>Выберите действие:</b>",
        reply_markup=main_kb,
        parse_mode="HTML"
    )

@dp.message(F.text == "🔍 Начать парсинг")
async def start_parsing(message: types.Message, state: FSMContext):
    """Начало поиска - запрос города"""
    await state.set_state(Form.waiting_for_city)
    await message.answer(
        "🏙️ Введите <b>город</b>:\n"
        "<i>Например: Москва, Санкт-Петербург, Казань</i>",
        parse_mode="HTML"
    )

@dp.message(Form.waiting_for_city)
async def get_city(message: types.Message, state: FSMContext):
    """Сохраняем город, запрашиваем нишу"""
    await state.update_data(city=message.text.strip())
    await state.set_state(Form.waiting_for_niche)
    await message.answer(
        "📍 Введите <b>нишу</b>:\n"
        "<i>Например: кафе, стоматология, автосервис, мебель</i>",
        parse_mode="HTML"
    )

@dp.message(Form.waiting_for_niche)
async def get_niche(message: types.Message, state: FSMContext):
    """Сохраняем нишу, запрашиваем количество"""
    await state.update_data(niche=message.text.strip())
    await state.set_state(Form.waiting_for_quantity)
    await message.answer(
        "📊 Выберите <b>сколько компаний</b> найти:",
        reply_markup=quantity_kb,
        parse_mode="HTML"
    )

@dp.message(Form.waiting_for_quantity)
async def get_quantity(message: types.Message, state: FSMContext):
    """Основная логика поиска"""
    try:
        quantity = int(message.text.strip().split()[0])
        if quantity not in [3, 5, 10]:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Пожалуйста, выберите количество <b>кнопками</b> ниже:",
            reply_markup=quantity_kb,
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    city = data["city"]
    niche = data["niche"]
    await state.clear()

    msg = await message.answer(
        f"🔎 <b>Ищу компании...</b>\n"
        f"🏙️ Город: {city}\n"
        f"📌 Ниша: {niche}\n"
        f"📊 Количество: {quantity}\n"
        f"⏳ Это займёт 20-30 секунд",
        parse_mode="HTML"
    )

    try:
        companies = SearchEngineRF.search_all(city, niche, quantity)

        if not companies:
            await msg.edit_text(
                "❌ <b>Ничего не найдено</b>\n\n"
                "💡 Попробуйте:\n"
                "• Проверить название города (Москва, а не мск)\n"
                "• Упростить нишу (кафе вместо 'кофейня с круассанами')\n"
                "• Попробовать позже (DuckDuckGo может быть недоступен)",
                parse_mode="HTML"
            )
            return

        user_data[message.from_user.id] = {
            "city": city,
            "niche": niche,
            "companies": companies
        }

        csv_file = save_to_csv(city, niche, companies)

        result_text = (
            f"✅ <b>Найдено {len(companies)} компаний</b>\n"
            f"💾 Сохранено: <code>{csv_file}</code>\n\n"
            f"📋 <b>Результаты:</b>\n"
            f"{'─' * 30}\n\n"
        )

        for i, c in enumerate(companies, 1):
            result_text += f"<b>{i}. {c.get('business_name', '—')}</b>\n"
            if c.get("phone"):
                result_text += f"   📞 {c['phone']}\n"
            if c.get("website"):
                result_text += f"   🌐 {c['website']}\n"
            if c.get("address"):
                result_text += f"   📍 {c['address'][:80]}\n"
            result_text += "\n"

        await msg.delete()

        if len(result_text) > 4000:
            parts = []
            current = ""
            for line in result_text.split('\n'):
                if len(current) + len(line) + 1 > 4000:
                    parts.append(current)
                    current = line + '\n'
                else:
                    current += line + '\n'
            if current:
                parts.append(current)

            for i, part in enumerate(parts):
                await message.answer(
                    part,
                    parse_mode="HTML",
                    reply_markup=main_kb if i == len(parts) - 1 else None
                )
        else:
            await message.answer(result_text, parse_mode="HTML", reply_markup=main_kb)

    except Exception as e:
        print(f"Ошибка: {e}")
        await msg.edit_text(
            f"❌ <b>Ошибка при поиске:</b>\n"
            f"<code>{str(e)[:400]}</code>",
            parse_mode="HTML"
        )

@dp.message(F.text == "📊 Выгрузить в Google Таблицу")
async def export_google(message: types.Message):
    """Выгрузка в Google Sheets"""
    uid = message.from_user.id

    if uid not in user_data:
        await message.answer(
            "⚠️ <b>Нет данных для выгрузки</b>\n"
            "Сначала нажмите '🔍 Начать парсинг'",
            reply_markup=main_kb,
            parse_mode="HTML"
        )
        return

    d = user_data[uid]
    loading = await message.answer("📊 Выгружаю в Google Таблицу...")

    if save_to_google(d["city"], d["niche"], d["companies"]):
        await loading.edit_text("✅ <b>Готово!</b> Данные в таблице.")
    else:
        await loading.edit_text(
            "❌ <b>Google Таблица недоступна</b>\n"
            "💾 Данные сохранены в CSV-файл",
            reply_markup=main_kb
        )

@dp.message(F.text == "📋 Показать последний результат")
async def show_result(message: types.Message):
    """Показать последний поиск"""
    uid = message.from_user.id

    if uid not in user_data:
        await message.answer(
            "⚠️ <b>Нет данных</b>\n"
            "Сначала нажмите '🔍 Начать парсинг'",
            reply_markup=main_kb,
            parse_mode="HTML"
        )
        return

    d = user_data[uid]
    companies = d["companies"]

    result = (
        f"📋 <b>Последний результат</b>\n"
        f"🏙️ {d['city']} | 📌 {d['niche']}\n"
        f"{'─' * 30}\n\n"
    )

    for i, c in enumerate(companies, 1):
        result += f"<b>{i}. {c.get('business_name', '—')}</b>\n"
        if c.get("phone"):
            result += f"   📞 {c['phone']}\n"
        if c.get("website"):
            result += f"   🌐 {c['website']}\n"
        if c.get("address"):
            result += f"   📍 {c['address'][:100]}\n"
        result += "\n"

    await message.answer(result, parse_mode="HTML", reply_markup=main_kb)

# ========== ВЕБ-СЕРВЕР (для Render) ==========
async def ping(request):
    return web.Response(text="✅ Бот работает!")

async def health_check(request):
    status = {
        "status": "ok",
        "time": datetime.now().strftime("%H:%M:%S"),
        "telegram": "ok" if TELEGRAM_TOKEN else "no_token",
        "yandex_maps": "ok" if YANDEX_MAPS_API_KEY else "no_key",
        "twogis": "ok",
        "google_sheets": "ok" if sheet else "not_connected"
    }
    return web.json_response(status)

async def run_web():
    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)

    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port}")

# ========== ЗАПУСК ==========
async def main():
    asyncio.create_task(run_web())

    print("\n" + "=" * 50)
    print("🤖 БОТ ЗАПУЩЕН!")
    print("=" * 50)
    print(f"🔑 Telegram: {'✅' if TELEGRAM_TOKEN else '❌ НЕТ ТОКЕНА'}")
    print(f"🔑 Yandex Maps: {'✅' if YANDEX_MAPS_API_KEY else '⚠️ нет'}")
    print(f"🔑 2GIS: ✅")
    print(f"📊 Google Sheets: {'✅' if sheet else '⚠️ не подключен'}")
    print("=" * 50 + "\n")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
