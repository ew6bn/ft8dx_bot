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

SGAS_HOUR = 5
SGAS_MINUTE = 10

WWV_SCHEDULES = [
    {"hour": 0, "minute": 10},
    {"hour": 6, "minute": 10},
    {"hour": 12, "minute": 10},
    {"hour": 18, "minute": 10}
]

# Telnet servers (primary and fallbacks)
TELNET_SERVERS = [
    {"host": "ea4ure.com", "port": 7300, "name": "Primary (EA4URE)"},
    {"host": "ve7cc.net", "port": 23, "name": "Fallback 1 (VE7CC)"},
    {"host": "s50clx.si", "port": 41112, "name": "Fallback 2 (S50CLX)"},
]

TELNET_USER = "EW6BN-2"

SENT_NEWS_FILE = "sent_news.json"
CALLSIGNS_FILE = "callsigns_425dxn.txt"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

tracked_callsigns = set()


# ========== SPOT DUPLICATE FILTER ==========
class SpotFilter:
    def __init__(self, timeout_seconds=15 * 60, freq_tolerance=1.0, delete_after_seconds=60 * 60):
        self.timeout = timeout_seconds
        self.freq_tolerance = freq_tolerance
        self.delete_after = delete_after_seconds
        self.cache = {}
    
    def is_duplicate(self, callsign, frequency):
        key = callsign.upper()
        current_time = time.time()
        freq = float(frequency)
        
        expired = [k for k, v in self.cache.items() if current_time - v["timestamp"] > self.timeout]
        for k in expired:
            del self.cache[k]
        
        if key in self.cache:
            cached_freq = self.cache[key]["frequency"]
            if abs(freq - cached_freq) <= self.freq_tolerance:
                return True
        
        return False
    
    def add_spot(self, callsign, frequency, chat_id, message_id):
        key = callsign.upper()
        self.cache[key] = {
            "timestamp": time.time(),
            "frequency": float(frequency),
            "chat_id": chat_id,
            "message_id": message_id
        }
    
    def schedule_deletion(self, chat_id, message_id, delay_seconds):
        def delete():
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
                payload = {"chat_id": chat_id, "message_id": message_id}
                requests.post(url, json=payload, timeout=10)
            except Exception:
                pass
        
        timer = threading.Timer(delay_seconds, delete)
        timer.daemon = True
        timer.start()


spot_filter = SpotFilter(15 * 60, 1.0, 60 * 60)


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def send_telegram_with_reply(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            result = response.json()
            return result.get("result", {}).get("message_id")
        return None
    except Exception:
        return None


def send_telegram_photo(photo_data, caption):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {"photo": ("image.jpg", photo_data, "image/jpeg")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        response = requests.post(url, files=files, data=data, timeout=30)
        return response.status_code == 200
    except Exception:
        return False


def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_image_url(article, base_url="https://www.dx-world.net"):
    img_tag = article.find("img")
    if img_tag and img_tag.get("src"):
        image_url = img_tag["src"]
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("/"):
            image_url = urljoin(base_url, image_url)
        return image_url
    return None


def download_image(image_url):
    if not image_url:
        return None
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = requests.get(image_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'image' in response.headers.get('content-type', ''):
            return response.content
    except Exception:
        return None


# ========== DX-World NEWS ==========
def parse_dx_world():
    url = "https://www.dx-world.net/"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
    }
    
    news_dict = {}
    
    try:
        logger.info(f"Parsing {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        articles = soup.find_all("article")
        if not articles:
            articles = soup.find_all("div", class_=re.compile(r"(post|entry)", re.I))
        
        if not articles:
            return []
        
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
                    text_parts = [clean_text(p.get_text()) for p in paragraphs if len(clean_text(p.get_text())) > 20]
                    news_text = "\n\n".join(text_parts)
                
                if not news_text:
                    desc_tag = article.find("div", class_=re.compile(r"(excerpt|summary)", re.I))
                    if desc_tag:
                        news_text = clean_text(desc_tag.get_text(strip=True))
                
                if link in news_dict:
                    if len(news_text) >= len(news_dict[link].get("text", "")):
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
                
            except Exception:
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
    except Exception:
        pass


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
        return
    
    new_sent = set(sent_ids)
    for i, news in enumerate(new_news, 1):
        caption = format_news_caption(news)
        if news.get('image_data'):
            send_telegram_photo(news['image_data'], caption)
        else:
            send_telegram(caption)
        new_sent.add(get_news_id(news))
        time.sleep(SLEEP_BETWEEN_MESSAGES)
    
    save_sent_news(new_sent)


# ========== EUROPEAN PREFIXES ==========
EUROPEAN_PREFIXES = {
    'CQ', 'CR', 'CS', 'CT', 'CU', 'AG',
    'EA', 'EB', 'EC', 'ED', 'EE', 'EF', 'EG', 'EH', 'AM', 'AN', 'AO',
    'EI', 'EJ', 'ER', 'ES', 'EU', 'EV', 'EW', 'F', 'TM',
    'G', 'M', '2E', '2I', '2J', '2M', '2W', 'GX', 'GB', 'GU', 'GJ', 'GW', 'GM', 'MM', 'MX', 'MU', 'MJ', 'MW',
    'HA', 'HG', 'HB', 'HE',
    'I', 'II', 'IZ', 'IK', 'IW', 'IG', 'IS', 'IT', 'IH', 'IN', 'IL', 'IA', 'IV',
    'LA', 'LB', 'LC', 'LD', 'LE', 'LF', 'LG', 'LH', 'LI', 'LJ', 'LK', 'LL', 'LM', 'LN',
    'LX', 'LY', 'LZ', 'OE', 'OH', 'OF', 'OG', 'OI', 'OK', 'OL', 'OM',
    'ON', 'OO', 'OP', 'OQ', 'OR', 'OS', 'OT', '5P',
    'OU', 'OV', 'OW', 'OX', 'OY', 'OZ',
    'PA', 'PB', 'PC', 'PD', 'PE', 'PF', 'PG', 'PH', 'PI',
    'R', 'RA', 'RB', 'RC', 'RD', 'RE', 'RF', 'RG', 'RH', 'RI', 'RJ', 'RK',
    'RL', 'RM', 'RN', 'RO', 'RP', 'RQ', 'RR', 'RS', 'RT', 'RU', 'RV', 'RW', 'RX', 'RY', 'RZ',
    'UA', 'UB', 'UC', 'UD', 'UE', 'UF', 'UG', 'UH', 'UI',
    'SA', 'SB', 'SC', 'SD', 'SE', 'SF', 'SG', 'SH', 'SI', 'SJ', 'SK', 'SL', 'SM',
    'SN', 'SO', 'SP', 'SQ', 'SR', '3Z', 'HF',
    'SV', 'SW', 'SX', 'SY', 'SZ', 'TA', 'TB', 'TC',
    'UR', 'US', 'UT', 'UU', 'UV', 'UW', 'UX', 'UY', 'UZ',
    'YO', 'YP', 'YQ', 'YR', 'YT', 'YU', '4N', '4O', '9A', '9H',
    'DA', 'DB', 'DC', 'DD', 'DE', 'DF', 'DG', 'DH', 'DI', 'DJ', 'DK', 'DL', 'DM', 'DN', 'DO', 'DP', 'DQ', 'DR', 'DS', 'DT', 'DU', 'DV', 'DW', 'DX', 'DY', 'DZ',
    '3A', '4J', '4K', '5B', 'C4', 'P3', 'S5', 'T7', 'TF', 'TK', 'ZA', 'Z3',
}


def is_european_callsign(callsign):
    if not callsign:
        return True
    
    base = callsign.upper().split('/')[0]
    
    if base.startswith('RI'):
        return False
    
    match = re.match(r'^([A-Z0-9]+?)[0-9]', base)
    prefix = match.group(1) if match else base
    
    for euro in EUROPEAN_PREFIXES:
        if prefix == euro:
            return True
    
    if len(base) >= 2 and base[0] == '2' and base[1] in 'EIJMW':
        return True
    
    return False


# ========== 425 DX NEWS CALENDAR ==========
def fetch_calendar():
    url = "https://www.425dxn.org/index.php?op=wcal"
    try:
        response = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return None


def extract_callsigns_from_calendar():
    html = fetch_calendar()
    if not html:
        return set()
    
    soup = BeautifulSoup(html, 'html.parser')
    
    table = None
    for tbl in soup.find_all('table'):
        if 'Period' in tbl.get_text() and 'Operation' in tbl.get_text():
            table = tbl
            break
    
    if not table:
        return set()
    
    callsigns = set()
    exclude_words = {'QRV', 'QSL', 'CW', 'SSB', 'FT8', 'RTTY', 'LOTW', 'EQSL', 'OQRS',
                     'VIA', 'BUREAU', 'DIRECT', 'CLUB', 'LOG', 'QRP', 'PWR', 'WATT',
                     'ANT', 'DIPOLE', 'YAGI', 'BEAM', 'HF', 'VHF', 'UHF', 'SAT',
                     'DIGITAL', 'PHONE', 'MODE', 'BAND', 'CQ', 'DX', 'IOTA',
                     'TILL', 'MAY', 'JUNE', 'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER',
                     'NOVEMBER', 'DECEMBER', 'FEBRUARY', 'MARCH', 'APRIL'}
    
    for row in table.find_all('tr')[1:]:
        cells = row.find_all('td')
        if len(cells) >= 2:
            op_html = str(cells[1])
            bold = re.search(r'<b>(.*?)</b>', op_html)
            if bold:
                first_line = bold.group(1)
            else:
                first_line = op_html.split('<br')[0] if '<br' in op_html else op_html.split('\n')[0]
            
            first_line = re.sub(r'<[^>]+>', '', first_line).strip()
            if ':' not in first_line:
                continue
            
            before_colon = first_line.split(':', 1)[0]
            
            if '-' in before_colon and re.search(r'[A-Z0-9]{2,}-[A-Z0-9]{2,}', before_colon):
                continue
            
            before_colon = before_colon.replace(' and ', ', ').replace('(', ',').replace(')', '')
            
            parts = []
            for p in before_colon.split(','):
                for sub in p.split():
                    if sub.strip():
                        parts.append(sub.strip())
            
            for part in parts:
                if part.upper() in exclude_words:
                    continue
                part_clean = part.rstrip('.,;:!?')
                if re.search(r'[0-9]', part_clean) and 3 <= len(part_clean) <= 15:
                    if not is_european_callsign(part_clean):
                        callsigns.add(part_clean)
    
    return callsigns


def update_callsigns():
    global tracked_callsigns
    
    logger.info("PARSING 425 DX NEWS CALENDAR")
    
    callsigns = extract_callsigns_from_calendar()
    callsigns = sorted(list(callsigns))
    
    with open(CALLSIGNS_FILE, 'w', encoding='utf-8') as f:
        f.write(f"# 425 DX News Calendar Callsigns\n")
        f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total: {len(callsigns)}\n\n")
        for cs in callsigns:
            f.write(f"{cs}\n")
    
    tracked_callsigns = set(callsigns)
    
    if callsigns:
        send_telegram(f"<b>📋 425 DX News Calendar</b>\n\nTotal: {len(callsigns)} callsigns\n\n<code>{', '.join(callsigns)}</code>")


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
        except Exception:
            pass


# ========== SGAS REPORT ==========
def send_sgas_report():
    logger.info("Fetching SGAS Report...")
    
    try:
        url = "https://services.swpc.noaa.gov/text/sgas.txt"
        response = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        report = response.text
    except Exception as e:
        logger.error(f"SGAS error: {e}")
        send_telegram("<b>⚠️ Solar Report</b>\n\nFailed to fetch")
        return
    
    clean_report = re.sub(r' +', ' ', report)
    msg = f"<b>🌞 Solar and Geophysical Activity Summary</b>\n\n<pre>{clean_report}</pre>"
    
    if len(msg) > 4096:
        send_telegram(f"<b>🌞 SGAS Report (part 1)</b>\n\n<pre>{clean_report[:3500]}</pre>")
        send_telegram(f"<b>🌞 SGAS Report (part 2)</b>\n\n<pre>{clean_report[3500:]}</pre>")
    else:
        send_telegram(msg)


# ========== WWV REPORT ==========
def send_wwv_report():
    logger.info("Fetching WWV Report...")
    
    try:
        url = "https://services.swpc.noaa.gov/text/wwv.txt"
        response = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        response.raise_for_status()
        report = response.text
    except Exception as e:
        logger.error(f"WWV error: {e}")
        send_telegram("<b>⚠️ WWV Alert</b>\n\nFailed to fetch")
        return
    
    lines = report.strip().split('\n')
    
    sfi = re.search(r'Solar flux\s+(\d+)', report)
    a_idx = re.search(r'A-index\s+(\d+)', report)
    k_idx = re.search(r'K-index at \d+ UTC on \d+ [A-Za-z]+ was (\d+\.?\d*)', report)
    
    msg = f"<b>⚡ WWV:</b> SFI={sfi.group(1) if sfi else '?'} A={a_idx.group(1) if a_idx else '?'} K={k_idx.group(1) if k_idx else '?'}"
    
    k_index_line_index = -1
    for i, line in enumerate(lines):
        if 'K-index' in line:
            k_index_line_index = i
            break
    
    if k_index_line_index >= 0:
        remaining_lines = lines[k_index_line_index + 1:]
        if remaining_lines:
            remaining_text = '\n'.join(remaining_lines).strip()
            if remaining_text:
                msg += f"\n\n{remaining_text}"
    
    send_telegram(msg)


# ========== TELNET CLIENT ==========
def telnet_monitor():
    global tracked_callsigns
    reconnect_delay = 5
    server_index = 0
    
    while True:
        server = TELNET_SERVERS[server_index]
        host = server["host"]
        port = server["port"]
        name = server["name"]
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            
            # TCP keepalive
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except:
                pass
            
            logger.info(f"Connecting to {name}: {host}:{port}")
            sock.connect((host, port))
            logger.info(f"Connected to {name}: {host}:{port}")
            reconnect_delay = 5
            server_index = 0
            
            sock.recv(4096)
            sock.send(f"{TELNET_USER}\r\n".encode('ascii'))
            time.sleep(2)
            sock.send(b"set/echo off\r\n")
            sock.send(b"set/dx/filter Band=HF\r\n")
            
            buffer = ""
            last_activity = time.time()
            
            while True:
                try:
                    sock.settimeout(30)
                    data = sock.recv(65536).decode('ascii', errors='ignore')
                    if data:
                        last_activity = time.time()
                        buffer += data
                        lines = buffer.split('\r\n')
                        buffer = lines[-1]
                        
                        for line in lines[:-1]:
                            raw = line.strip()
                            if raw and raw.startswith('DX de'):
                                match = re.search(r'DX de \S+:\s+([\d.]+)\s+([A-Z0-9/]+)', raw)
                                if match:
                                    freq = match.group(1)
                                    target = match.group(2).upper()
                                    base = target.split('/')[0]
                                    
                                    if target in tracked_callsigns or base in tracked_callsigns:
                                        if spot_filter.is_duplicate(target, freq):
                                            continue
                                        
                                        logger.info(f"MATCH: {target} on {freq}")
                                        clean_raw = re.sub(r'\s+', ' ', raw)
                                        
                                        msg_text = f"<b>🎯 DX SPOT!</b>\n\n<code>{clean_raw}</code>"
                                        message_id = send_telegram_with_reply(msg_text)
                                        if message_id:
                                            spot_filter.add_spot(target, freq, CHAT_ID, message_id)
                                            spot_filter.schedule_deletion(CHAT_ID, message_id, 60 * 60)
                                        
                except socket.timeout:
                    if time.time() - last_activity > 60:
                        logger.warning(f"No data for 60 seconds from {name}, switching server...")
                        break
                    continue
                except Exception as e:
                    logger.error(f"Read error from {name}: {e}")
                    break
                time.sleep(2)
                
        except Exception as e:
            logger.error(f"Connection error to {name} ({host}:{port}): {e}")
            server_index = (server_index + 1) % len(TELNET_SERVERS)
            next_server = TELNET_SERVERS[server_index]["name"]
            logger.info(f"Switching to {next_server} in {reconnect_delay} seconds...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ========== SCHEDULER ==========
def run_scheduler():
    schedule.clear()
    
    schedule.every().day.at(f"{PARSING_HOUR:02d}:{PARSING_MINUTE:02d}").do(send_new_news)
    schedule.every().day.at(f"{SGAS_HOUR:02d}:{SGAS_MINUTE:02d}").do(send_sgas_report)
    
    for wwv in WWV_SCHEDULES:
        schedule.every().day.at(f"{wwv['hour']:02d}:{wwv['minute']:02d}").do(send_wwv_report)
    
    getattr(schedule.every(), CALENDAR_PARSING_DAY).at(f"{CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d}").do(update_callsigns)
    
    logger.info(f"Scheduler started at UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(60)


def test_bot():
    servers = ", ".join([f"{s['name']} ({s['host']}:{s['port']})" for s in TELNET_SERVERS])
    return send_telegram(f"<b>🤖 DX-World News Bot</b>\n\n✅ Bot started\n⏰ DX-World: 06:55 UTC\n📅 425 Calendar: Sunday 01:00 UTC\n🌞 SGAS Report: 05:10 UTC\n⚡ WWV Report: every 6 hours\n🔍 Telnet servers: {servers}\n🔄 Auto-reconnect + failover enabled")


# ========== ENTRY POINT ==========
if __name__ == "__main__":
    print("=" * 60)
    print("DX-World Telegram Bot v13.1")
    print("=" * 60)
    print(f"Channel: {CHAT_ID}")
    print(f"SGAS Report: {SGAS_HOUR:02d}:{SGAS_MINUTE:02d} UTC")
    print(f"WWV Report: every 6 hours")
    print(f"425 Calendar: Sunday {CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d} UTC")
    print(f"Telnet servers:")
    for s in TELNET_SERVERS:
        print(f"  - {s['name']}: {s['host']}:{s['port']}")
    print("=" * 60)
    
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set!")
        sys.exit(1)
    
    test_bot()
    
    print("\nLoading...")
    update_callsigns()
    load_callsigns()
    send_all_news()
    send_sgas_report()
    send_wwv_report()
    
    print("\nStarting Telnet monitor...")
    threading.Thread(target=telnet_monitor, daemon=True).start()
    
    print("\nBot started. Press Ctrl+C to stop.\n")
    
    try:
        run_scheduler()
    except KeyboardInterrupt:
        print("\nBot stopped.")