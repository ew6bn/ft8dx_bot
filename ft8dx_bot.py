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

# ========== SETTINGS ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN environment variable not set!")
    sys.exit(1)

CHAT_ID = "@ft8dx"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

PARSING_HOUR = 6
PARSING_MINUTE = 55
MAX_NEWS_PER_POST = 15
SLEEP_BETWEEN_MESSAGES = 2

CALENDAR_PARSING_DAY = 'sunday'
CALENDAR_PARSING_HOUR = 1
CALENDAR_PARSING_MINUTE = 0

SGA_HOUR = 22
SGA_MINUTE = 10

# WWV schedule - every 6 hours at 00:10, 06:10, 12:10, 18:10 UTC
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
        if response.status_code == 200:
            return True
        else:
            logger.error(f"Telegram error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram exception: {e}")
        return False


def send_telegram_photo(photo_data, caption):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {"photo": ("image.jpg", photo_data, "image/jpeg")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        response = requests.post(url, files=files, data=data, timeout=30)
        if response.status_code == 200:
            return True
        else:
            logger.error(f"Photo error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Photo exception: {e}")
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
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(image_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'image' in response.headers.get('content-type', ''):
            return response.content
    except Exception as e:
        logger.error(f"Image download error: {e}")
    return None


# ========== DX-World NEWS ==========
def parse_dx_world():
    url = "https://www.dx-world.net/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
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


# ========== 425 DX NEWS CALENDAR ==========
def fetch_calendar():
    url = "https://www.425dxn.org/index.php?op=wcal"
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
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
    
    for row in table.find_all('tr')[1:]:
        cells = row.find_all('td')
        if len(cells) >= 2:
            operation_html = str(cells[1])
            
            if '<br' in operation_html:
                first_line = operation_html.split('<br')[0]
            else:
                first_line = operation_html.split('\n')[0]
            
            first_line = re.sub(r'<[^>]+>', '', first_line).strip()
            
            if ':' in first_line:
                first_line = first_line.split(':', 1)[0]
            
            if re.search(r'[A-Z]{1,2}[0-9][A-Z0-9]{1,5}-[A-Z]{1,2}[0-9][A-Z0-9]{1,5}', first_line):
                continue
            
            first_line = first_line.replace(' and ', ', ')
            first_line = first_line.replace('(', ',').replace(')', '')
            
            for part in first_line.split(','):
                part = part.strip()
                if part and re.match(r'^[A-Z]{1,2}[0-9][A-Z0-9]{1,5}$', part):
                    callsigns.add(part)
    
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
    
    logger.info(f"Saved {len(callsigns)} callsigns to {CALLSIGNS_FILE}")
    tracked_callsigns = set(callsigns)
    
    if callsigns:
        callsigns_str = ", ".join(callsigns)
        msg = f"<b>📋 425 DX News Calendar</b>\n\n<b>Total:</b> {len(callsigns)} callsigns\n\n<b>Tracking:</b>\n<code>{callsigns_str}</code>"
        send_telegram(msg)


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
                logger.info(f"Loaded {len(callsigns)} callsigns for tracking")
        except Exception as e:
            logger.error(f"Load error: {e}")


# ========== SGA REPORT ==========
def fetch_sga_report():
    url = "https://services.swpc.noaa.gov/text/sgarf.txt"
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"SGA report error: {e}")
        return None


def send_sga_report():
    """Send full SGA report to Telegram"""
    logger.info("Fetching SGA Report...")
    
    report = fetch_sga_report()
    if not report:
        send_telegram("<b>⚠️ Solar Report</b>\n\nFailed to fetch SGA report from NOAA")
        return
    
    # Clean report: remove extra spaces
    clean_report = re.sub(r' +', ' ', report)
    
    # Send full report as preformatted text
    msg = f"<b>🌞 Solar Geophysical Activity Report</b>\n\n<pre>{clean_report}</pre>"
    
    # Telegram has message length limit (4096 chars)
    if len(msg) > 4096:
        header = "<b>🌞 Solar Geophysical Activity Report (part 1)</b>\n\n"
        first_part = clean_report[:3500]
        send_telegram(header + f"<pre>{first_part}</pre>")
        
        second_part = clean_report[3500:]
        if second_part:
            send_telegram("<b>🌞 Solar Geophysical Activity Report (part 2)</b>\n\n" + f"<pre>{second_part}</pre>")
    else:
        send_telegram(msg)
    
    logger.info("SGA Report sent")


# ========== WWV REPORT (every 6 hours) ==========
def fetch_wwv_report():
    """Fetch WWV geophysical alert message"""
    url = "https://services.swpc.noaa.gov/text/wwv.txt"
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"WWV report error: {e}")
        return None


def parse_wwv_report(report_text):
    """Parse WWV report and extract key data"""
    if not report_text:
        return None
    
    result = {}
    
    # Extract Solar flux (SFI)
    sfi_match = re.search(r'Solar flux\s+(\d+)', report_text)
    if sfi_match:
        result['sfi'] = sfi_match.group(1)
    
    # Extract A-index
    a_match = re.search(r'A-index\s+(\d+)', report_text)
    if a_match:
        result['a_index'] = a_match.group(1)
    
    # Extract K-index
    k_match = re.search(r'K-index at \d+ UTC on \d+ [A-Za-z]+ was (\d+\.?\d*)', report_text)
    if k_match:
        result['k_index'] = k_match.group(1)
    
    # Extract observation/prediction message
    obs_match = re.search(r'No space weather storms were observed for the past 24 hours\.', report_text)
    if obs_match:
        result['observation'] = "No space weather storms observed for the past 24 hours."
    else:
        # Try to find any observation message
        obs_alt = re.search(r'([A-Z][^.]+\.[^.]+\.[^.]+?(?:storms|quiet|active)[^.]*\.)', report_text, re.IGNORECASE)
        if obs_alt:
            result['observation'] = obs_alt.group(1).strip()
    
    pred_match = re.search(r'No space weather storms are predicted for the next 24 hours\.', report_text)
    if pred_match:
        result['prediction'] = "No space weather storms predicted for the next 24 hours."
    else:
        # Try to find any prediction message
        pred_alt = re.search(r'are predicted for the next 24 hours[^.]*\.', report_text, re.IGNORECASE)
        if pred_alt:
            result['prediction'] = pred_alt.group(0).strip()
    
    return result


def send_wwv_report():
    """Fetch and send WWV report to Telegram"""
    logger.info("Fetching WWV Report...")
    
    report = fetch_wwv_report()
    if not report:
        send_telegram("<b>⚠️ WWV Alert</b>\n\nFailed to fetch WWV report from NOAA")
        return
    
    parsed = parse_wwv_report(report)
    if not parsed:
        send_telegram("<b>⚠️ WWV Alert</b>\n\nFailed to parse WWV report")
        return
    
    # Build compact message
    msg_parts = []
    
    if parsed.get('sfi') or parsed.get('a_index') or parsed.get('k_index'):
        sfi = parsed.get('sfi', '?')
        a_idx = parsed.get('a_index', '?')
        k_idx = parsed.get('k_index', '?')
        msg_parts.append(f"<b>⚡ WWV:</b> SFI={sfi} A={a_idx} K={k_idx}")
    
    if parsed.get('observation'):
        msg_parts.append(parsed['observation'])
    
    if parsed.get('prediction'):
        msg_parts.append(parsed['prediction'])
    
    if not msg_parts:
        msg_parts.append("No data available")
    
    message = "\n".join(msg_parts)
    send_telegram(message)
    logger.info("WWV Report sent")


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
                                # Extract target callsign (after frequency)
                                match = re.search(r'DX de \S+:\s+[\d.]+\s+([A-Z0-9/]+)', raw)
                                if match:
                                    target = match.group(1).upper()
                                    
                                    # Check exact match
                                    if target in tracked_callsigns:
                                        logger.info(f"MATCH: {target}")
                                        clean_raw = re.sub(r'\s+', ' ', raw)
                                        msg = f"<b>🎯 DX SPOT!</b>\n\n<code>{clean_raw}</code>"
                                        send_telegram(msg)
                                    else:
                                        # Check without suffix
                                        base = target.split('/')[0]
                                        if base in tracked_callsigns:
                                            logger.info(f"MATCH (base): {base}")
                                            clean_raw = re.sub(r'\s+', ' ', raw)
                                            msg = f"<b>🎯 DX SPOT!</b>\n\n<code>{clean_raw}</code>"
                                            send_telegram(msg)
                                        
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
    
    # DX-World news
    schedule.every().day.at(f"{PARSING_HOUR:02d}:{PARSING_MINUTE:02d}").do(send_new_news)
    
    # SGA report daily at 22:10 UTC
    schedule.every().day.at(f"{SGA_HOUR:02d}:{SGA_MINUTE:02d}").do(send_sga_report)
    
    # WWV report every 6 hours
    for wwv in WWV_SCHEDULES:
        schedule.every().day.at(f"{wwv['hour']:02d}:{wwv['minute']:02d}").do(send_wwv_report)
    
    # 425 Calendar on Sunday
    getattr(schedule.every(), CALENDAR_PARSING_DAY).at(f"{CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d}").do(update_callsigns)
    
    logger.info("=" * 50)
    logger.info(f"Scheduler started at UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"DX-World news: daily at {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC")
    logger.info(f"SGA Report: daily at {SGA_HOUR:02d}:{SGA_MINUTE:02d} UTC")
    wwv_times = ", ".join([f"{w['hour']:02d}:{w['minute']:02d} UTC" for w in WWV_SCHEDULES])
    logger.info(f"WWV Report: every 6 hours at {wwv_times}")
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
    return send_telegram("<b>🤖 DX-World News Bot</b>\n\n✅ Bot started\n⏰ DX-World: 06:55 UTC\n📅 425 Calendar: Sunday 01:00 UTC\n🌞 SGA Report: 22:10 UTC\n⚡ WWV Report: every 6 hours (00:10, 06:10, 12:10, 18:10 UTC)\n🔍 DX Cluster active")


# ========== ENTRY POINT ==========
if __name__ == "__main__":
    print("=" * 60)
    print("DX-World Telegram Bot v10.4")
    print("=" * 60)
    print(f"Channel: {CHAT_ID}")
    print(f"DX-World: {PARSING_HOUR:02d}:{PARSING_MINUTE:02d} UTC")
    print(f"SGA Report: {SGA_HOUR:02d}:{SGA_MINUTE:02d} UTC")
    print(f"WWV Report: every 6 hours at 00:10, 06:10, 12:10, 18:10 UTC")
    print(f"425 Calendar: Sunday {CALENDAR_PARSING_HOUR:02d}:{CALENDAR_PARSING_MINUTE:02d} UTC")
    print(f"Telnet: {TELNET_HOST}:{TELNET_PORT}")
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
    
    # Send SGA report on startup
    print("\nSending SGA Report on startup...")
    send_sga_report()
    
    # Send WWV report on startup
    print("\nSending WWV Report on startup...")
    send_wwv_report()
    
    print("\nStarting Telnet monitor...")
    t = threading.Thread(target=telnet_monitor, daemon=True)
    t.start()
    
    print(f"\nBot started. Press Ctrl+C to stop.\n")
    
    try:
        run_scheduler()
    except KeyboardInterrupt:
        print("\nBot stopped.")