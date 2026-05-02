#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot для парсинга новостей с DX-World.net
- Канал: @ft8dx
- Каждая новость отдельным сообщением с картинкой и текстом (только с главной)
- ПРИ ЗАПУСКЕ БОТА: отправляет ВСЕ новости с сайта
- ПО РАСПИСАНИЮ (06:55 UTC): отправляет ТОЛЬКО НОВЫЕ новости
- БЕЗ итоговых сообщений
- БЕЗ времени в подписи
- С дедупликацией (оставляем версию с МЕНЬШИМ текстом - краткую)
"""

import os
import sys
import requests
from bs4 import BeautifulSoup
import schedule
import time
import logging
from datetime import datetime, timezone
import re
import html
import json
from urllib.parse import urljoin

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN environment variable not set!")
    sys.exit(1)

CHAT_ID = "@ft8dx"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
PARSING_HOUR = 6
PARSING_MINUTE = 55
MAX_NEWS_PER_POST = 15
SLEEP_BETWEEN_MESSAGES = 2

SENT_NEWS_FILE = "sent_news.json"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def clean_text(text):
    """Очищает текст от HTML тегов и лишних пробелов"""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_image_url(article, base_url="https://www.dx-world.net"):
    """Извлекает URL изображения из статьи на главной странице"""
    image_url = None
    
    img_tag = article.find("img")
    if img_tag and img_tag.get("src"):
        image_url = img_tag["src"]
    
    if not image_url:
        img_div = article.find("div", class_=re.compile(r"(image|photo|thumbnail|featured)", re.I))
        if img_div:
            img_tag = img_div.find("img")
            if img_tag and img_tag.get("src"):
                image_url = img_tag["src"]
    
    if image_url:
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("/"):
            image_url = urljoin(base_url, image_url)
    
    return image_url

def download_image(image_url):
    """Скачивает изображение и возвращает bytes"""
    if not image_url:
        return None
    
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(image_url, headers=headers, timeout=10)
        if response.status_code == 200:
            content_type = response.headers.get('content-type', '')
            if 'image' in content_type:
                return response.content
    except Exception as e:
        logger.error(f"Ошибка скачивания изображения: {e}")
    
    return None

# ========== РАБОТА С ХРАНИЛИЩЕМ ==========
def load_sent_news():
    """Загружает ID отправленных новостей из файла"""
    if os.path.exists(SENT_NEWS_FILE):
        try:
            with open(SENT_NEWS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('sent_ids', []))
        except Exception as e:
            logger.error(f"Ошибка загрузки sent_news.json: {e}")
    return set()

def save_sent_news(sent_ids):
    """Сохраняет ID отправленных новостей в файл"""
    try:
        with open(SENT_NEWS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'sent_ids': list(sent_ids), 
                'last_update': datetime.now(timezone.utc).isoformat()
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"Сохранено {len(sent_ids)} ID новостей")
    except Exception as e:
        logger.error(f"Ошибка сохранения sent_news.json: {e}")

def get_news_id(news_item):
    """Генерирует уникальный ID для новости на основе ссылки"""
    return news_item.get('link', '')

# ========== ПАРСИНГ НОВОСТЕЙ (С ДЕДУПЛИКАЦИЕЙ - ОСТАВЛЯЕМ КРАТКУЮ ВЕРСИЮ) ==========
def parse_dx_world():
    """
    Парсит главную страницу dx-world.net
    Берёт заголовок, текст и картинку ТОЛЬКО с главной
    При дубликатах оставляем версию с МЕНЬШИМ текстом (краткую)
    """
    url = "https://www.dx-world.net/"
    
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    
    news_dict = {}  # Ключ = ссылка, значение = новость
    
    try:
        logger.info(f"Начинаю парсинг {url}")
        session = requests.Session()
        session.headers.update(headers)
        response = session.get(url, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Поиск статей
        articles = soup.find_all("article")
        if not articles:
            articles = soup.find_all("div", class_=re.compile(r"(post|entry|news-item)", re.I))
        
        if not articles:
            logger.warning("Не удалось найти статьи на странице")
            return []
        
        logger.info(f"Найдено {len(articles)} элементов для обработки")
        
        for article in articles:
            try:
                # === ЗАГОЛОВОК И ССЫЛКА ===
                title = None
                link = None
                
                # Ищем в h2 или h3 с ссылкой
                for header_tag in ['h2', 'h3', 'h4']:
                    header = article.find(header_tag)
                    if header:
                        link_tag = header.find("a", href=True)
                        if link_tag:
                            title = link_tag.get_text(strip=True)
                            link = link_tag["href"]
                            break
                
                # Если не нашли, ищем любую ссылку
                if not title:
                    link_tag = article.find("a", href=True)
                    if link_tag and link_tag.get_text(strip=True):
                        title = link_tag.get_text(strip=True)
                        link = link_tag["href"]
                
                if not title or not link:
                    continue
                
                # Очищаем заголовок
                title = clean_text(title)
                
                # Полный URL ссылки
                if link.startswith("/"):
                    link = urljoin("https://www.dx-world.net", link)
                
                # === ТЕКСТ НОВОСТИ ===
                news_text = ""
                
                # Ищем параграфы внутри статьи
                paragraphs = article.find_all("p")
                if paragraphs:
                    text_parts = []
                    for p in paragraphs:
                        p_text = clean_text(p.get_text())
                        if p_text and len(p_text) > 20:
                            text_parts.append(p_text)
                    news_text = "\n\n".join(text_parts)
                
                # Если нет параграфов, ищем div с excerpt/summary
                if not news_text:
                    desc_tag = article.find("div", class_=re.compile(r"(excerpt|summary|entry-summary)", re.I))
                    if desc_tag:
                        news_text = clean_text(desc_tag.get_text(strip=True))
                
                # Если всё ещё нет, ищем любой div с текстом
                if not news_text:
                    text_div = article.find("div", class_=re.compile(r"(content|text|body)", re.I))
                    if text_div:
                        news_text = clean_text(text_div.get_text(strip=True))
                
                # === ДЕДУПЛИКАЦИЯ: оставляем версию с МЕНЬШИМ текстом (краткую) ===
                if link in news_dict:
                    existing_text = news_dict[link].get("text", "")
                    # Если текущий текст КОРОЧЕ существующего, заменяем
                    if len(news_text) < len(existing_text):
                        logger.debug(f"Замена на краткую версию для: {title[:40]}... ({len(existing_text)} -> {len(news_text)} символов)")
                    else:
                        logger.debug(f"Пропускаем длинную версию для: {title[:40]}... ({len(news_text)} > {len(existing_text)})")
                        continue
                
                # === ИЗОБРАЖЕНИЕ ===
                image_url = extract_image_url(article)
                image_data = download_image(image_url) if image_url else None
                
                # Сохраняем в словарь
                news_dict[link] = {
                    "title": title,
                    "link": link,
                    "text": news_text,
                    "image_url": image_url,
                    "image_data": image_data,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                
                logger.info(f"Найдена новость: {title[:50]}... (текст: {len(news_text)} символов)")
                
            except Exception as e:
                logger.error(f"Ошибка при обработке статьи: {e}")
                continue
        
        # Преобразуем словарь в список и ограничиваем количество
        news_list = list(news_dict.values())[:MAX_NEWS_PER_POST]
        logger.info(f"Парсинг завершён. Найдено уникальных новостей: {len(news_list)}")
        return news_list
        
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return []

# ========== ФОРМАТИРОВАНИЕ СООБЩЕНИЯ ==========
def format_news_caption(news_item):
    """Форматирует новость для отправки (без времени в подписи)"""
    title = news_item["title"]
    link = news_item["link"]
    news_text = news_item.get("text", "")
    
    # Экранируем HTML специальные символы
    title = html.escape(title)
    news_text = html.escape(news_text)
    
    # Формируем сообщение
    caption = f"<b>📡 {title}</b>\n\n"
    
    # Добавляем текст новости
    if news_text:
        caption += f"{news_text}\n\n"
    
    # Добавляем ссылку
    caption += f"🔗 <a href='{link}'>Читать далее на сайте</a>\n"
    caption += f"━━━━━━━━━━\n"
    caption += f"🤖 <b>FT8DX News Bot</b>"
    
    return caption

# ========== ОТПРАВКА В TELEGRAM ==========
def send_photo_message(chat_id, photo_data, caption, bot_token):
    """Отправляет сообщение с фотографией"""
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    
    if photo_data:
        files = {"photo": ("image.jpg", photo_data, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        try:
            response = requests.post(url, files=files, data=data, timeout=30)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Ошибка отправки с фото: {e}")
            return False
    else:
        return send_text_message(chat_id, caption, bot_token)

def send_text_message(chat_id, text, bot_token):
    """Отправляет текстовое сообщение"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=30)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Ошибка отправки текста: {e}")
        return False

# ========== ОТПРАВКА НОВОСТЕЙ (РАЗНЫЕ РЕЖИМЫ) ==========
def send_all_news():
    """Отправляет ВСЕ новости с сайта (при запуске бота) - БЕЗ итогового сообщения"""
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК БОТА - отправляем ВСЕ новости с сайта")
    logger.info("=" * 50)
    
    # Парсим новости
    news_list = parse_dx_world()
    
    if not news_list:
        logger.warning("Новостей не найдено")
        return
    
    logger.info(f"Найдено уникальных новостей на сайте: {len(news_list)}")
    
    # Отправляем каждую новость
    success_count = 0
    all_sent_ids = set()
    
    for i, news_item in enumerate(news_list, 1):
        try:
            logger.info(f"Отправка {i}/{len(news_list)}: {news_item['title'][:40]}...")
            
            caption = format_news_caption(news_item)
            
            if news_item.get('image_data'):
                success = send_photo_message(CHAT_ID, news_item['image_data'], caption, BOT_TOKEN)
            else:
                success = send_text_message(CHAT_ID, caption, BOT_TOKEN)
            
            if success:
                success_count += 1
                all_sent_ids.add(get_news_id(news_item))
                logger.info(f"✅ Отправлено {i}/{len(news_list)}")
            else:
                logger.error(f"❌ Ошибка отправки {i}/{len(news_list)}")
            
            if i < len(news_list):
                time.sleep(SLEEP_BETWEEN_MESSAGES)
                
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    # Сохраняем ВСЕ ID отправленных новостей
    if success_count > 0:
        save_sent_news(all_sent_ids)
        logger.info(f"✅ Отправлено {success_count} из {len(news_list)} новостей")

def send_new_news():
    """Отправляет ТОЛЬКО НОВЫЕ новости (по расписанию) - БЕЗ итогового сообщения"""
    logger.info("=" * 50)
    logger.info("📅 РАСПИСАНИЕ - отправляем ТОЛЬКО НОВЫЕ новости")
    logger.info("=" * 50)
    
    # Загружаем уже отправленные ID
    sent_ids = load_sent_news()
    logger.info(f"Уже отправлено новостей в истории: {len(sent_ids)}")
    
    # Парсим новости
    news_list = parse_dx_world()
    
    if not news_list:
        logger.warning("Новостей не найдено")
        return
    
    logger.info(f"Найдено уникальных новостей на сайте: {len(news_list)}")
    
    # Фильтруем новые новости
    new_news = []
    for news in news_list:
        news_id = get_news_id(news)
        if news_id not in sent_ids:
            new_news.append(news)
    
    if not new_news:
        logger.info("Новых новостей нет")
        return
    
    logger.info(f"Новых новостей для отправки: {len(new_news)}")
    
    # Отправляем только новые
    success_count = 0
    new_sent_ids = set(sent_ids)
    
    for i, news_item in enumerate(new_news, 1):
        try:
            logger.info(f"Отправка {i}/{len(new_news)}: {news_item['title'][:40]}...")
            
            caption = format_news_caption(news_item)
            
            if news_item.get('image_data'):
                success = send_photo_message(CHAT_ID, news_item['image_data'], caption, BOT_TOKEN)
            else:
                success = send_text_message(CHAT_ID, caption, BOT_TOKEN)
            
            if success:
                success_count += 1
                new_sent_ids.add(get_news_id(news_item))
                logger.info(f"✅ Отправлено {i}/{len(new_news)}")
            else:
                logger.error(f"❌ Ошибка отправки {i}/{len(new_news)}")
            
            if i < len(new_news):
                time.sleep(SLEEP_BETWEEN_MESSAGES)
                
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    # Сохраняем обновлённый список
    if success_count > 0:
        save_sent_news(new_sent_ids)
        logger.info(f"✅ Отправлено {success_count} из {len(new_news)} новых новостей")

# ========== ЗАПУСК ПЛАНИРОВЩИКА ==========
def run_scheduler():
    """Запускает планировщик с ежедневной отправкой (только новые)"""
    schedule.clear()
    schedule.every().day.at(f"{PARSING_HOUR:02d}:{PARSING_MINUTE:02d}").do(send_new_news)
    
    logger.info("=" * 50)
    logger.info(f"⏰ Планировщик запущен")
    logger.info(f"📅 Ежедневная отправка в {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC")
    logger.info(f"📡 Режим: только НОВЫЕ новости")
    logger.info(f"📡 Канал: {CHAT_ID}")
    logger.info("=" * 50)
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Получен сигнал остановки...")
            break
        except Exception as e:
            logger.error(f"Ошибка в планировщике: {e}")
            time.sleep(60)

# ========== ТЕСТОВАЯ ОТПРАВКА ==========
def test_bot():
    """Тестовая отправка для проверки прав бота"""
    logger.info("Проверка прав бота...")
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": "<b>🤖 DX-World News Bot</b>\n\n✅ Бот запущен и работает\n⏰ Расписание: 06:55 UTC",
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Тестовое сообщение отправлено успешно")
            return True
        else:
            logger.error(f"❌ Тестовая отправка не удалась: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Тестовая отправка не удалась: {e}")
        return False

# ========== ТОЧКА ВХОДА ==========
if __name__ == "__main__":
    print("=" * 60)
    print("DX-World Telegram Bot v7.5")
    print("=" * 60)
    print(f"📡 Канал: {CHAT_ID}")
    print(f"⏰ Расписание: {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC (только новые)")
    print(f"🚀 При запуске: ВСЕ новости с сайта")
    print(f"📝 Режим: краткие новости (как на главной)")
    print("=" * 60)
    
    if not BOT_TOKEN:
        print("❌ ОШИБКА: BOT_TOKEN не найден в переменных окружения!")
        print("\n⚠️  Установите переменную окружения BOT_TOKEN")
        print("Пример: set BOT_TOKEN=ваш_токен_бота")
        sys.exit(1)
    
    logger.info("DX-World Telegram Bot запущен")
    
    print("\n🔍 Проверка прав бота...")
    if not test_bot():
        print(f"❌ ОШИБКА: Бот не может отправить сообщение в {CHAT_ID}")
        print("Проверьте:")
        print("1. Правильность токена бота")
        print("2. Бот добавлен в канал как администратор")
        print("3. CHAT_ID указан верно (@ft8dx)")
        sys.exit(1)
    
    print("✅ Права бота подтверждены")
    
    # Отправляем ВСЕ новости при запуске
    print("\n🚀 Отправляем ВСЕ новости с сайта...")
    send_all_news()
    
    print(f"\n✅ Бот запущен")
    print(f"⏰ Следующая отправка по расписанию: {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC (только новые)")
    print("Нажмите Ctrl+C для остановки.\n")
    
    try:
        run_scheduler()
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
        print("\n✅ Бот остановлен.")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)