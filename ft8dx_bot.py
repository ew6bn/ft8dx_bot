#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot for DX-World.net news and DX Cluster monitoring
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
import socket
import threading
import random

# ========== SETTINGS ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN environment variable not set!")
    sys.exit(1)

CHAT_ID = "@ft8dx"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

PARSING_HOUR = 6
PARSING_MINUTE = 55
MAX_NEWS_PER_POST = 15
SLEEP_BETWEEN_MESSAGES = 2

CALENDAR_PARSING_DAY = 'sunday'
CALENDAR_PARSING_HOUR = 1
CALENDAR_PARSING_MINUTE = 0

SGA_HOUR = 22
SGA_MINUTE = 10

WWV_SCHEDULES = [
    {"hour": 0, "minute": 10},
    {"hour": 6, "minute": 10},
    {"hour": 12, "minute": 10},
    {"hour": 18, "minute": 10}
]

TELNET_HOST = "dxc.pi4cc.nl"
TELNET_PORT = 8000
TELNET_USER = "EW6BN-2"

SENT_NEWS_FILE = "sent_news.json"
CALLSIGNS_FILE = "callsigns_425dxn.txt"

# Cache for sent spots to avoid duplicates
sent_spots_cache = {}
SPOT_CACHE_TIMEOUT = 15 * 60  # 15 minutes in seconds

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

tracked_callsigns = set()


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


def send_telegram_photo(photo_data, caption):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {"photo": ("image.jpg", photo_data, "image/jpeg")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        response = requests.post(url, files=files, data=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Photo error: {e}")
        return False


def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_image_url(article, base_url="https://www.dx-world.net"):
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
    if not image_url:
        return None
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = requests.get(image_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'image' in response.headers.get('content-type', ''):
            return response.content
    except Exception as e:
        logger.error(f"Image error: {e}")
    return None


# ========== SPOT DUPLICATE FILTER ==========
def is_spot_duplicate(callsign, frequency):
    """
    Check if we've already sent this spot in the last 15 minutes
    Only called for callsigns that are in tracking list
    """
    global sent_spots_cache
    
    key = f"{callsign}|{frequency}"
    current_time = time.time()
    
    # Clean expired entries
    expired_keys = []
    for k, timestamp in sent_spots_cache.items():
        if current_time - timestamp > SPOT_CACHE_TIMEOUT:
            expired_keys.append(k)
    for k in expired_keys:
        del sent_spots_cache[k]
    
    # Check if key exists
    if key in sent_spots_cache:
        elapsed = int(current_time - sent_spots_cache[key])
        logger.info(f"🔄 Duplicate spot filtered: {callsign} on {frequency} (last sent {elapsed} sec ago)")
        return True
    
    # Store new spot
    sent_spots_cache[key] = current_time
    return False


# ========== DX-World NEWS ==========
def parse_dx_world():
    url = "https://www.dx-world.net/"
    
    current_user_agent = random.choice(USER_AGENTS)
    
    headers = {
        "User-Agent": current_user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "Referer": "https://www.google.com/",
    }
    
    news_dict = {}
    
    try:
        logger.info(f"Parsing {url}")
        session = requests.Session()
        session.headers.update(headers)
        response = session.get(url, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        articles = soup.find_all("article")
        if not articles:
            articles = soup.find_all("div", class_=re.compile(r"(post|entry|news-item)", re.I))
        
        if not articles:
            logger.warning("No articles found")
            return []
        
        logger.info(f"Found {len(articles)} articles")
        
        for article in articles[:MAX_NEWS_PER_POST]:
            try:
                title = None
                link = None
                
                for header_tag in ['h2', 'h3', 'h4']:
                    header = article.find(header_tag)
                    if header:
                        link_tag = header.find("a", href=True)
                        if link_tag:
                            title = link_tag.get_text(strip=True)
                            link = link_tag["href"]
                            break
                
                if not title:
                    link_tag = article.find("a", href=True)
                    if link_tag and link_tag.get_text(strip=True):
                        title = link_tag.get_text(strip=True)
                        link = link_tag["href"]
                
                if not title or not link:
                    continue
                
                title = clean_text(title)
                
                if link.startswith("/"):
                    link = urljoin("https://www.dx-world.net", link)
                
                news_text = ""
                paragraphs = article.find_all("p")
                if paragraphs:
                    text_parts = []
                    for p in paragraphs:
                        p_text = clean_text(p.get_text())
                        if p_text and len(p_text) > 20:
                            text_parts.append(p_text)
                    news_text = "\n\n".join(text_parts)
                
                if not news_text:
                    desc_tag = article.find("div", class_=re.compile(r"(excerpt|summary|entry-summary)", re.I))
                    if desc_tag:
                        news_text = clean_text(desc_tag.get_text(strip=True))
                
                if link in news_dict:
                    existing_text = news_dict[link].get("text", "")
                    if len(news_text) >= len(existing_text):
                        continue
                
                image_url = extract_image_url(article)
                image_data = download_image(image_url) if image_url else None
                
                news_dict[link] = {
                    "title": title,
                    "link": link,
                    "text": news_text,
                    "image_url": image_url,
                    "image_data": image_data
                }
                
                logger.info(f"Found: {title[:50]}...")
                
            except Exception as e:
                logger.error(f"Error: {e}")
                continue
        
        return list(news_dict.values())
        
    except Exception as e:
        logger.error(f"Parsing error: {e}")
        return []


def format_news_caption(news_item):
    title = html.escape(news_item["title"])
    link = news_item["link"]
    news_text = html.escape(news_item.get("text", ""))
    caption = f"<b>📡 {title}</b>\n\n"
    if news_text:
        caption += f"{news_text}\n\n"
    caption += f"🔗 <a href='{link}'>Read more</a>\n━━━━━━━━━━\n🤖 <b>FT8DX News Bot</b>"
    return caption


def load_sent_news():
    if os.path.exists(SENT_NEWS_FILE):
        try:
            with open(SENT_NEWS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('sent_ids', []))
        except:
            pass
    return set()


def save_sent_news(sent_ids):
    try:
        with open(SENT_NEWS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'sent_ids': list(sent_ids)}, f)
    except Exception as e:
        logger.error(f"Save error: {e}")


def get_news_id(news_item):
    return news_item.get('link', '')


def send_all_news():
    logger.info("Sending ALL news...")
    news_list = parse_dx_world()
    if not news_list:
        return
    
    sent_ids = set()
    for i, news in enumerate(news_list, 1):
        caption = format_news_caption(news)
        if news.get('image_data'):
            send_telegram_photo(news['image_data'], caption)
        else:
            send_telegram(caption)
        sent_ids.add(get_news_id(news))
        logger.info(f"Sent {i}/{len(news_list)}")
        time.sleep(SLEEP_BETWEEN_MESSAGES)
    
    if sent_ids:
        save_sent_news(sent_ids)


def send_new_news():
    logger.info("Sending NEW news...")
    sent_ids = load_sent_news()
    news_list = parse_dx_world()
    if not news_list:
        return
    
    new_news = [n for n in news_list if get_news_id(n) not in sent_ids]
    if not new_news:
        logger.info("No new news")
        return
    
    new_sent = set(sent_ids)
    for i, news in enumerate(new_news, 1):
        caption = format_news_caption(news)
        if news.get('image_data'):
            send_telegram_photo(news['image_data'], caption)
        else:
            send_telegram(caption)
        new_sent.add(get_news_id(news))
        logger.info(f"Sent new {i}/{len(new_news)}")
        time.sleep(SLEEP_BETWEEN_MESSAGES)
    
    save_sent_news(new_sent)


# ========== EUROPEAN PREFIXES TO EXCLUDE ==========
EUROPEAN_PREFIXES = {
    # Portugal
    'CQ', 'CR', 'CS', 'CT', 'CU', 'AG',
    # Spain
    'EA', 'EB', 'EC', 'ED', 'EE', 'EF', 'EG', 'EH', 'AM', 'AN', 'AO',
    # Ireland
    'EI', 'EJ',
    # Moldova
    'ER',
    # Estonia
    'ES',
    # Belarus
    'EU', 'EV', 'EW',
    # France
    'F', 'TM',
    # United Kingdom
    'G', 'M', '2E', '2I', '2J', '2M', '2W', 'GX', 'GB', 'GU', 'GJ', 'GW', 'GM', 'MM', 'MX', 'MU', 'MJ', 'MW',
    # Hungary
    'HA', 'HG',
    # Switzerland
    'HB', 'HE',
    # Italy
    'I', 'II', 'IZ', 'IK', 'IW', 'IG', 'IS', 'IT', 'IH', 'IN', 'IL', 'IA', 'IV',
    # Norway
    'LA', 'LB', 'LC', 'LD', 'LE', 'LF', 'LG', 'LH', 'LI', 'LJ', 'LK', 'LL', 'LM', 'LN',
    # Luxembourg
    'LX',
    # Lithuania
    'LY',
    # Bulgaria
    'LZ',
    # Austria
    'OE',
    # Finland
    'OH', 'OF', 'OG', 'OI',
    # Czech Republic
    'OK', 'OL',
    # Slovakia
    'OM',
    # Belgium
    'ON', 'OO', 'OP', 'OQ', 'OR', 'OS', 'OT', '5P',
    # Denmark
    'OU', 'OV', 'OW', 'OX', 'OY', 'OZ',
    # Netherlands
    'PA', 'PB', 'PC', 'PD', 'PE', 'PF', 'PG', 'PH', 'PI',
    # Russia
    'R', 'RA', 'RB', 'RC', 'RD', 'RE', 'RF', 'RG', 'RH', 'RI', 'RJ', 'RK', 
    'RL', 'RM', 'RN', 'RO', 'RP', 'RQ', 'RR', 'RS', 'RT', 'RU', 'RV', 'RW', 'RX', 'RY', 'RZ',
    'UA', 'UB', 'UC', 'UD', 'UE', 'UF', 'UG', 'UH', 'UI',
    # Sweden
    'SA', 'SB', 'SC', 'SD', 'SE', 'SF', 'SG', 'SH', 'SI', 'SJ', 'SK', 'SL', 'SM',
    # Poland
    'SN', 'SO', 'SP', 'SQ', 'SR', '3Z', 'HF',
    # Greece
    'SV', 'SW', 'SX', 'SY', 'SZ',
    # Turkey
    'TA', 'TB', 'TC',
    # Ukraine
    'UR', 'US', 'UT', 'UU', 'UV', 'UW', 'UX', 'UY', 'UZ',
    # Romania
    'YO', 'YP', 'YQ', 'YR',
    # Serbia
    'YT', 'YU', '4N', '4O',
    # Croatia
    '9A',
    # Malta
    '9H',
    # Germany
    'DA', 'DB', 'DC', 'DD', 'DE', 'DF', 'DG', 'DH', 'DI', 'DJ', 'DK', 'DL', 'DM', 'DN', 'DO', 'DP', 'DQ', 'DR', 'DS', 'DT', 'DU', 'DV', 'DW', 'DX', 'DY', 'DZ',
    # Monaco
    '3A',
    # Azerbaijan
    '4J', '4K',
    # Cyprus
    '5B', 'C4', 'P3',
    # Slovenia
    'S5',
    # San Marino
    'T7',
    # Iceland
    'TF',
    # Corsica
    'TK',
    # Albania
    'ZA',
    # North Macedonia
    'Z3',
}


def is_european_callsign(callsign):
    """Check if a callsign is European based on its prefix"""
    if not callsign:
        return True
    
    callsign_upper = callsign.upper()
    
    # Take prefix BEFORE slash if present
    base = callsign_upper.split('/')[0]
    
    # RI prefix is for Arctic/Antarctic stations - NOT European
    if base.startswith('RI'):
        return False
    
    # Extract the prefix (letters/digits before the first digit)
    prefix_match = re.match(r'^([A-Z0-9]+?)[0-9]', base)
    if prefix_match:
        prefix = prefix_match.group(1)
    else:
        prefix = base
    
    # Check if prefix matches any European prefix
    for euro_prefix in EUROPEAN_PREFIXES:
        if prefix == euro_prefix:
            return True
    
    # UK 2-letter prefixes (2E, 2I, 2J, 2M, 2W)
    if len(base) >= 2 and base[0] == '2' and base[1] in 'EIJMW':
        return True
    
    return False


# ========== 425 DX NEWS CALENDAR ==========
def fetch_calendar():
    url = "https://www.425dxn.org/index.php?op=wcal"
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return None


def extract_callsigns_from_calendar():
    html_content = fetch_calendar()
    if not html_content:
        return set()
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    table = None
    for tbl in soup.find_all('table'):
        if 'Period' in tbl.get_text() and 'Operation' in tbl.get_text():
            table = tbl
            break
    
    if not table:
        logger.error("Calendar table not found")
        return set()
    
    callsigns = set()
    
    # Words that are NOT callsigns
    exclude_words = {
        'QRV', 'QSL', 'CW', 'SSB', 'FT8', 'RTTY', 'LOTW', 'EQSL', 'OQRS',
        'VIA', 'BUREAU', 'DIRECT', 'CLUB', 'LOG', 'QRP', 'PWR', 'WATT',
        'ANT', 'DIPOLE', 'YAGI', 'VERTICAL', 'BEAM', 'HF', 'VHF', 'UHF',
        'SAT', 'DIGITAL', 'PHONE', 'MODE', 'BAND', 'METER', 'WATTS',
        'CQ', 'DX', 'IOTA', 'WWFF', 'SOTA', 'POTA', 'QTH', 'UTC',
        'DE', 'TU', 'TNX', '73', '88', 'TEST', 'CONTEST', 'F/H', 'MSHV',
        'TILL', 'MAY', 'JUNE', 'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER',
        'NOVEMBER', 'DECEMBER', 'FEBRUARY', 'MARCH', 'APRIL'
    }
    
    for row in table.find_all('tr')[1:]:
        cells = row.find_all('td')
        if len(cells) >= 2:
            operation_html = str(cells[1])
            
            # Extract first line - look for bold text first
            bold_text = re.search(r'<b>(.*?)</b>', operation_html)
            if bold_text:
                first_line = bold_text.group(1)
            else:
                if '<br' in operation_html:
                    first_line = operation_html.split('<br')[0]
                else:
                    first_line = operation_html.split('\n')[0]
            
            first_line = re.sub(r'<[^>]+>', '', first_line).strip()
            
            if ':' not in first_line:
                continue
            
            before_colon = first_line.split(':', 1)[0]
            
            # Skip ranges (CALL1-CALL2)
            if '-' in before_colon and re.search(r'[A-Z0-9]{2,}-[A-Z0-9]{2,}', before_colon):
                continue
            
            # Clean up separators
            before_colon = before_colon.replace(' and ', ', ')
            before_colon = before_colon.replace('(', ',').replace(')', '')
            
            # Split by comma and by space
            parts = []
            for p in before_colon.split(','):
                for subp in p.split():
                    if subp.strip():
                        parts.append(subp.strip())
            
            for part in parts:
                if not part:
                    continue
                
                # Skip obvious non-callsigns
                if part.upper() in exclude_words:
                    continue
                
                # Remove trailing punctuation
                part_clean = part.rstrip('.,;:!?')
                
                # Must contain a digit
                if not re.search(r'[0-9]', part_clean):
                    continue
                
                # Length check
                if not (3 <= len(part_clean) <= 15):
                    continue
                
                if not is_european_callsign(part_clean):
                    callsigns.add(part_clean)
    
    return callsigns


def update_callsigns():
    global tracked_callsigns
    
    logger.info("PARSING 425 DX NEWS CALENDAR")
    
    callsigns = extract_callsigns_from_calendar()
    callsigns = sorted(list(callsigns))
    
    with open(CALLSIGNS_FILE, 'w', encoding='utf-8') as f:
        f.write(f"# 425 DX News Calendar Callsigns (Non-European only)\n")
        f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total: {len(callsigns)}\n\n")
        for cs in callsigns:
            f.write(f"{cs}\n")
    
    logger.info(f"Saved {len(callsigns)} non-European callsigns")
    tracked_callsigns = set(callsigns)
    
    if callsigns:
        callsigns_str = ", ".join(callsigns)
        send_telegram(f"<b>📋 425 DX News Calendar</b>\n\nTotal: {len(callsigns)} callsigns\n\n<code>{callsigns_str}</code>")


def load_callsigns():
    global tracked_callsigns
    if os.path.exists(CALLSIGNS_FILE):
        try:
            with open(CALLSIGNS_FILE, 'r', encoding='utf-8') as f:
                callsigns = set()
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        callsigns.add(line.upper())
                tracked_callsigns = callsigns
                logger.info(f"Loaded {len(callsigns)} callsigns")
        except Exception as e:
            logger.error(f"Load error: {e}")


# ========== SGA REPORT ==========
def fetch_sga_report():
    url = "https://services.swpc.noaa.gov/text/sgarf.txt"
    try:
        response = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"SGA error: {e}")
        return None


def send_sga_report():
    logger.info("Fetching SGA Report...")
    
    report = fetch_sga_report()
    if not report:
        send_telegram("<b>⚠️ Solar Report</b>\n\nFailed to fetch")
        return
    
    clean_report = re.sub(r' +', ' ', report)
    msg = f"<b>🌞 Solar Geophysical Activity Report</b>\n\n<pre>{clean_report}</pre>"
    
    if len(msg) > 4096:
        first_part = clean_report[:3500]
        send_telegram(f"<b>🌞 Solar Report (part 1)</b>\n\n<pre>{first_part}</pre>")
        if len(clean_report) > 3500:
            send_telegram(f"<b>🌞 Solar Report (part 2)</b>\n\n<pre>{clean_report[3500:]}</pre>")
    else:
        send_telegram(msg)


# ========== WWV REPORT ==========
def fetch_wwv_report():
    url = "https://services.swpc.noaa.gov/text/wwv.txt"
    try:
        response = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"WWV error: {e}")
        return None


def parse_wwv_report(report_text):
    if not report_text:
        return None
    
    result = {}
    
    sfi_match = re.search(r'Solar flux\s+(\d+)', report_text)
    if sfi_match:
        result['sfi'] = sfi_match.group(1)
    
    a_match = re.search(r'A-index\s+(\d+)', report_text)
    if a_match:
        result['a_index'] = a_match.group(1)
    
    k_match = re.search(r'K-index at \d+ UTC on \d+ [A-Za-z]+ was (\d+\.?\d*)', report_text)
    if k_match:
        result['k_index'] = k_match.group(1)
    
    obs_match = re.search(r'No space weather storms were observed for the past 24 hours\.', report_text)
    if obs_match:
        result['observation'] = "No storms observed past 24h"
    
    pred_match = re.search(r'No space weather storms are predicted for the next 24 hours\.', report_text)
    if pred_match:
        result['prediction'] = "No storms predicted next 24h"
    
    return result


def send_wwv_report():
    logger.info("Fetching WWV Report...")
    
    report = fetch_wwv_report()
    if not report:
        send_telegram("<b>⚠️ WWV Alert</b>\n\nFailed to fetch")
        return
    
    parsed = parse_wwv_report(report)
    if not parsed:
        send_telegram("<b>⚠️ WWV Alert</b>\n\nFailed to parse")
        return
    
    msg_parts = []
    
    if parsed.get('sfi') or parsed.get('a_index') or parsed.get('k_index'):
        msg_parts.append(f"<b>⚡ WWV:</b> SFI={parsed.get('sfi', '?')} A={parsed.get('a_index', '?')} K={parsed.get('k_index', '?')}")
    
    if parsed.get('observation'):
        msg_parts.append(parsed['observation'])
    
    if parsed.get('prediction'):
        msg_parts.append(parsed['prediction'])
    
    if not msg_parts:
        msg_parts.append("No data available")
    
    send_telegram("\n".join(msg_parts))


# ========== TELNET CLIENT ==========
def telnet_monitor():
    global tracked_callsigns
    
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((TELNET_HOST, TELNET_PORT))
            logger.info(f"Connected to {TELNET_HOST}:{TELNET_PORT}")
            
            sock.recv(4096)
            sock.send(f"{TELNET_USER}\r\n".encode('ascii'))
            logger.info(f"Logged in as {TELNET_USER}")
            time.sleep(2)
            sock.send(b"set/echo off\r\n")
            sock.send(b"set/dx/filter Band=HF\r\n")
            
            buffer = ""
            
            while True:
                try:
                    sock.settimeout(1)
                    data = sock.recv(65536).decode('ascii', errors='ignore')
                    if data:
                        buffer += data
                        lines = buffer.split('\r\n')
                        buffer = lines[-1]
                        
                        for line in lines[:-1]:
                            raw = line.strip()
                            if raw and raw.startswith('DX de'):
                                # Extract frequency and callsign
                                match = re.search(r'DX de \S+:\s+([\d.]+)\s+([A-Z0-9/]+)', raw)
                                if match:
                                    frequency = match.group(1)
                                    target = match.group(2).upper()
                                    base = target.split('/')[0]
                                    
                                    # Check if callsign is in tracking list
                                    if target in tracked_callsigns or base in tracked_callsigns:
                                        # Only check duplicate for matching spots
                                        if is_spot_duplicate(target, frequency):
                                            continue
                                        
                                        logger.info(f"✅ MATCH: {target}")
                                        clean_raw = re.sub(r'\s+', ' ', raw)
                                        send_telegram(f"<b>🎯 DX SPOT!</b>\n\n<code>{clean_raw}</code>")
                                    # Ignore other spots completely (no logging)
                                        
                except socket.timeout:
                    pass
                except Exception as e:
                    logger.error(f"Read error: {e}")
                    break
                time.sleep(2)
                
        except Exception as e:
            logger.error(f"Telnet error: {e}")
            logger.info("Reconnecting in 30 seconds...")
            time.sleep(30)


# ========== SCHEDULER ==========
def run_scheduler():
    schedule.clear()
    
    schedule.every().day.at(f"{PARSING_HOUR:02d}:{PARSING_MINUTE:02d}").do(send_new_news)
    schedule.every().day.at(f"{SGA_HOUR:02d}:{SGA_MINUTE:02d}").do(send_sga_report)
    
    for wwv in WWV_SCHEDULES:
        schedule.every().day.at(f"{wwv['hour']:02d}:{wwv['minute']:02d}").do(send_wwv_report)
    
    getattr(schedule.every(), CALENDAR_PARSING_DAY).at(f"{CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d}").do(update_callsigns)
    
    logger.info("=" * 50)
    logger.info(f"Scheduler started at UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"DX-World news: daily at {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC")
    logger.info(f"SGA Report: daily at {SGA_HOUR:02d}:{SGA_MINUTE:02d} UTC")
    logger.info(f"WWV Report: every 6 hours")
    logger.info(f"425 Calendar: Sunday at {CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d} UTC")
    logger.info("=" * 50)
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)


def test_bot():
    return send_telegram("<b>🤖 DX-World News Bot</b>\n\n✅ Bot started\n⏰ DX-World: 06:55 UTC\n📅 425 Calendar: Sunday 01:00 UTC\n🌞 SGA Report: 22:10 UTC\n⚡ WWV Report: every 6 hours\n🔍 DX Cluster active (15 min duplicate filter)")


# ========== ENTRY POINT ==========
if __name__ == "__main__":
    print("=" * 60)
    print("DX-World Telegram Bot v12.2")
    print("=" * 60)
    print(f"Channel: {CHAT_ID}")
    print(f"DX-World: {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC")
    print(f"SGA Report: {SGA_HOUR:02d}:{SGA_MINUTE:02d} UTC")
    print(f"WWV Report: every 6 hours")
    print(f"425 Calendar: Sunday {CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d} UTC")
    print(f"Telnet: {TELNET_HOST}:{TELNET_PORT}")
    print(f"Duplicate filter: 15 minutes (same callsign + frequency)")
    print("=" * 60)
    
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set!")
        sys.exit(1)
    
    test_bot()
    print("Bot permissions confirmed")
    
    print("\nParsing 425 DX News Calendar...")
    update_callsigns()
    
    print("\nLoading callsigns for tracking...")
    load_callsigns()
    
    print("\nSending DX-World news...")
    send_all_news()
    
    print("\nSending SGA Report...")
    send_sga_report()
    
    print("\nSending WWV Report...")
    send_wwv_report()
    
    print("\nStarting Telnet monitor with duplicate filter...")
    t = threading.Thread(target=telnet_monitor, daemon=True)
    t.start()
    
    print(f"\nBot started. Press Ctrl+C to stop.\n")
    
    try:
        run_scheduler()
    except KeyboardInterrupt:
        print("\nBot stopped.")