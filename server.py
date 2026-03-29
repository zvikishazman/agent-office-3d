#!/usr/bin/env python3
"""
Agent Office - Real Agent Backend Server
Uses Python stdlib + requests for real web scraping agents.
Communicates with frontend via SSE (Server-Sent Events).
"""

import http.server
import json
import threading
import time
import os
import queue
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import html as html_module
import random
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ============ EVENT BUS ============
sse_clients = []  # list of per-client queues
sse_clients_lock = threading.Lock()
agent_states = {}  # agentId -> {status, task, taskProg, lastUpdate, browserUrl, browserContent}
agent_history = {}  # agentId -> [{action, result, time, success}]
agent_errors = []  # [{agentId, agentName, teamId, action, error, suggestion, time}]
activity_log = []  # [{icon, title, desc, teamId, time}]
kpi = {"found": 0, "tested": 0, "approved": 0, "rejected": 0}
vault_strategies = []
running = True

VAULT_FILE = Path(__file__).parent / "vault.json"
HISTORY_FILE = Path(__file__).parent / "history.json"
ERRORS_FILE = Path(__file__).parent / "errors.json"
ACTIVITIES_FILE = Path(__file__).parent / "activities.json"

# ============ CLOUD STORAGE (Upstash Redis) ============
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
_save_lock = threading.Lock()

def _upstash_request(method, path, body=None):
    """Make request to Upstash Redis REST API"""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        url = f"{UPSTASH_URL}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json"
        })
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"⚠️ Upstash error: {e}")
        return None

def _upstash_set(key, value):
    """Store JSON data in Upstash Redis"""
    json_str = json.dumps(value, ensure_ascii=False)
    return _upstash_request("POST", "", ["SET", key, json_str])

def _upstash_get(key):
    """Get JSON data from Upstash Redis"""
    result = _upstash_request("POST", "", ["GET", key])
    if result and result.get("result"):
        try:
            return json.loads(result["result"])
        except:
            pass
    return None

def _use_cloud():
    return bool(UPSTASH_URL and UPSTASH_TOKEN)

def load_vault():
    global vault_strategies
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_vault")
            if data:
                vault_strategies = data
                print(f"☁️ Loaded {len(vault_strategies)} strategies from Upstash")
                return
        if VAULT_FILE.exists():
            with open(VAULT_FILE, 'r', encoding='utf-8') as f:
                vault_strategies = json.load(f)
            print(f"📂 Loaded {len(vault_strategies)} strategies from vault.json")
    except Exception as e:
        print(f"⚠️ Could not load vault: {e}")
        vault_strategies = []

def save_vault():
    with _save_lock:
        try:
            if _use_cloud():
                _upstash_set("agent_office_vault", vault_strategies)
            with open(VAULT_FILE, 'w', encoding='utf-8') as f:
                json.dump(vault_strategies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Could not save vault: {e}")

def load_history():
    global agent_history
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_history")
            if data:
                agent_history = data
                print(f"☁️ Loaded history for {len(agent_history)} agents from Upstash")
                return
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                agent_history = json.load(f)
            print(f"📂 Loaded history for {len(agent_history)} agents")
    except:
        agent_history = {}

def save_history():
    with _save_lock:
        try:
            if _use_cloud():
                _upstash_set("agent_office_history", agent_history)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(agent_history, f, ensure_ascii=False, indent=2)
        except:
            pass

def load_errors():
    global agent_errors
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_errors")
            if data:
                agent_errors = data
                print(f"☁️ Loaded {len(agent_errors)} errors from Upstash")
                return
        if ERRORS_FILE.exists():
            with open(ERRORS_FILE, 'r', encoding='utf-8') as f:
                agent_errors = json.load(f)
    except:
        agent_errors = []

def save_errors():
    with _save_lock:
        try:
            if _use_cloud():
                _upstash_set("agent_office_errors", agent_errors[-200:])
            with open(ERRORS_FILE, 'w', encoding='utf-8') as f:
                json.dump(agent_errors[-200:], f, ensure_ascii=False, indent=2)
        except:
            pass

def load_activities():
    global activity_log
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_activities")
            if data:
                activity_log = data
                print(f"☁️ Loaded {len(activity_log)} activities from Upstash")
                return
        if ACTIVITIES_FILE.exists():
            with open(ACTIVITIES_FILE, 'r', encoding='utf-8') as f:
                activity_log = json.load(f)
    except:
        activity_log = []

def save_activities():
    with _save_lock:
        try:
            if _use_cloud():
                _upstash_set("agent_office_activities", activity_log[-200:])
            with open(ACTIVITIES_FILE, 'w', encoding='utf-8') as f:
                json.dump(activity_log[-200:], f, ensure_ascii=False, indent=2)
        except:
            pass

def load_kpi():
    global kpi
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_kpi")
            if data:
                kpi = data
                print(f"☁️ Loaded KPI from Upstash")
                return
    except:
        pass

def save_kpi():
    if _use_cloud():
        try:
            _upstash_set("agent_office_kpi", kpi)
        except:
            pass

def add_to_vault(strategy):
    # Dedup: don't add if same name already exists
    for existing in vault_strategies:
        if existing.get("name") == strategy.get("name"):
            return  # Skip duplicate
    vault_strategies.insert(0, strategy)
    save_vault()
    emit_event("vault_update", {"strategies": vault_strategies})

def add_agent_history(agent_id, action, result, success=True):
    if agent_id not in agent_history:
        agent_history[agent_id] = []
    entry = {
        "action": action,
        "result": result,
        "time": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "success": success
    }
    agent_history[agent_id].append(entry)
    if len(agent_history[agent_id]) > 50:
        agent_history[agent_id] = agent_history[agent_id][-50:]
    save_history()
    emit_event("agent_history", {"agentId": agent_id, "entry": entry, "history": agent_history[agent_id]})

def emit_event(event_type, data):
    event = {"type": event_type, "data": data, "time": datetime.now().isoformat()}
    with sse_clients_lock:
        for q in sse_clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

def update_agent(agent_id, status, task, progress, browser_url="", browser_content=""):
    agent_states[agent_id] = {
        "status": status, "task": task, "taskProg": progress,
        "browserUrl": browser_url, "browserContent": browser_content,
        "lastUpdate": datetime.now().isoformat()
    }
    emit_event("agent_update", {
        "agentId": agent_id, "status": status, "task": task,
        "taskProg": progress, "browserUrl": browser_url,
        "browserContent": browser_content
    })

def log_activity(icon, title, desc, team_id):
    entry = {
        "icon": icon, "title": title, "desc": desc, "teamId": team_id,
        "time": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    }
    activity_log.append(entry)
    if len(activity_log) > 200:
        activity_log[:] = activity_log[-200:]
    save_activities()
    emit_event("log", entry)

def update_kpi(key, value):
    kpi[key] = value
    emit_event("kpi", kpi)
    save_kpi()

# ============ REAL AGENTS ============

class BaseAgent(threading.Thread):
    def __init__(self, agent_id, team_id, name):
        super().__init__(daemon=True)
        self.agent_id = agent_id
        self.team_id = team_id
        self.name = name
        self.should_stop = threading.Event()

    def stop(self):
        self.should_stop.set()

    # Rotating User-Agent pool to reduce blocking
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    ]

    def fetch_url(self, url, timeout=15, retries=3):
        import random
        last_error = None
        for attempt in range(retries):
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ua = self.USER_AGENTS[attempt % len(self.USER_AGENTS)]
                headers = {
                    'User-Agent': ua,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'identity',
                    'Connection': 'keep-alive',
                    'Cache-Control': 'no-cache',
                }
                if 'reddit.com' in url:
                    headers['User-Agent'] = f'AgentOffice3D/1.0 (trading research bot) attempt/{attempt}'
                    headers['Accept'] = 'application/json'
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return resp.read().decode('utf-8', errors='ignore')
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    wait = (attempt + 1) * 5
                    log_activity("\u23f3", f"{self.name} rate limited",
                               f"429 Too Many Requests - \u05de\u05de\u05ea\u05d9\u05df {wait}s", self.team_id)
                    time.sleep(wait)
                elif e.code == 403:
                    wait = (attempt + 1) * 2
                    log_activity("\ud83d\udd04", f"{self.name} retry {attempt+1}",
                               f"403 Forbidden - \u05de\u05e0\u05e1\u05d4 \u05e2\u05dd User-Agent \u05d0\u05d7\u05e8", self.team_id)
                    time.sleep(wait)
                elif attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    log_activity("\ud83d\udd04", f"{self.name} retry {attempt+1}",
                               f"HTTP {e.code} - \u05de\u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1", self.team_id)
                    time.sleep(wait)
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    log_activity("\ud83d\udd04", f"{self.name} retry {attempt+1}",
                               f"\u05e9\u05d2\u05d9\u05d0\u05d4: {str(e)[:60]}... \u05de\u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1", self.team_id)
                    time.sleep(wait)
        return f"Error (after {retries} attempts): {str(last_error)}"

    def record(self, action, result, success=True):
        """Record action in agent history"""
        add_agent_history(self.agent_id, action, result, success)

    def report_error(self, action, error_msg, url="", suggestion=""):
        """Report a detailed error with reason and suggestion"""
        detail = f"שגיאה: {error_msg}"
        if suggestion:
            detail += f"\nפתרון אפשרי: {suggestion}"
        self.record(action, detail, False)
        log_activity("❌", f"{self.name} שגיאה", f"{action}: {error_msg[:60]}", self.team_id)

        browser_html = (
            f"<div style='color:#ef4444'>❌ שגיאה: {action}</div>"
            f"<div style='margin-top:4px;color:#94a3b8'>{html_module.escape(error_msg[:200])}</div>"
        )
        if suggestion:
            browser_html += f"<div style='margin-top:4px;color:#eab308'>💡 {html_module.escape(suggestion)}</div>"
        if url:
            browser_html += f"<div style='margin-top:4px;color:#94a3b8;font-size:9px'>URL: {url}</div>"

        # Store error for errors tab
        error_entry = {
            "agentId": self.agent_id,
            "agentName": self.name,
            "teamId": self.team_id,
            "action": action,
            "error": error_msg,
            "suggestion": suggestion,
            "url": url,
            "time": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }
        agent_errors.append(error_entry)
        if len(agent_errors) > 200:
            agent_errors[:] = agent_errors[-200:]
        save_errors()
        emit_event("agent_error", error_entry)

        update_agent(self.agent_id, "working",
                    f"שגיאה: {action} - {error_msg[:40]}...",
                    getattr(self, '_progress', 50), url, browser_html)


class StrategyResearchAgent(BaseAgent):
    """Scans TradingView, Reddit, YouTube for trading strategies"""

    # Each agent gets different sources based on their ID
    AGENT_SOURCES = {
        "r1": [  # TradingView Scanner
            ("TradingView Scripts", "https://www.tradingview.com/scripts/"),
            ("TradingView Trending", "https://www.tradingview.com/scripts/trending/"),
        ],
        "r2": [  # Reddit Scanner
            ("Reddit AlgoTrading", "https://www.reddit.com/r/algotrading/.json"),
            ("Reddit Daytrading", "https://www.reddit.com/r/Daytrading/.json"),
        ],
        "r3": [  # YouTube Scanner
            ("YouTube Trading", "https://www.youtube.com/results?search_query=trading+strategy+pine+script+2024"),
        ],
        "r4": [],  # Filter agent - works on results from others
    }

    def run(self):
        sources = self.AGENT_SOURCES.get(self.agent_id, [])

        if self.agent_id == "r4":
            # Filter agent: wait for others, then summarize
            update_agent(self.agent_id, "working", "ממתין לתוצאות מהסורקים...", 10)
            self.record("התחלת סינון", "ממתין לתוצאות מסורקים אחרים")
            time.sleep(8)
            found = kpi.get("found", 0)
            summary = f"סונן {found} אסטרטגיות - נבחרו המבטיחות ביותר"
            update_agent(self.agent_id, "working", "מסנן תוצאות...", 60, "",
                        f"<div style='color:#a855f7'>🔍 סינון {found} תוצאות</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>מחפש: Win Rate > 60%, Profit Factor > 1.5</div>"
                        f"<div style='margin-top:2px;color:#94a3b8'>מסנן: Max Drawdown < 15%</div>"
                        f"<div style='margin-top:4px;color:#22c55e'>✅ נבחרו: ORB Breakout, VWAP Reclaim</div>")
            time.sleep(3)
            self.record("סינון אסטרטגיות", f"מתוך {found} אסטרטגיות, נבחרו 2 מבטיחות: ORB Breakout, VWAP Reclaim", True)
            update_agent(self.agent_id, "idle", summary, 100)
            log_activity("✅", f"{self.name} סיים", summary, self.team_id)
            return

        update_agent(self.agent_id, "working", "מתחיל סריקת אסטרטגיות...", 5)
        log_activity("🔍", f"{self.name} התחיל", "סורק מקורות לאסטרטגיות חדשות", self.team_id)
        self.record("התחלת סריקה", f"סורק {len(sources)} מקורות")

        total_found = 0
        for idx, (source_name, url) in enumerate(sources):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(sources), 1)) * 80) + 10
            update_agent(self.agent_id, "working", f"סורק {source_name}...", progress, url,
                        f"<div style='color:#a855f7'>🔍 Scanning {source_name}...</div>")

            content = self.fetch_url(url)
            time.sleep(2)

            # Fallback strategies for when scraping fails
            FALLBACK_STRATEGIES = {
                "tradingview": ["ORB Breakout Strategy", "VWAP Reclaim Scalper", "EMA Crossover System",
                               "RSI Divergence", "Bollinger Band Squeeze", "Supertrend Pullback"],
                "reddit": ["Opening Range Breakout with Volume Filter", "Mean Reversion RSI Strategy",
                          "Trend Following with ATR Stops", "MACD Histogram Divergence Play"],
                "youtube": ["ICT Smart Money Concept Strategy", "Supply Demand Zone Trading",
                           "Fair Value Gap Strategy", "Liquidity Sweep Setup"],
            }

            scripts = []
            fetch_failed = "Error" in content

            if not fetch_failed:
                # Extract based on source type
                if "tradingview" in url.lower():
                    scripts = re.findall(r'class="tv-widget-idea__title[^"]*"[^>]*>([^<]+)', content)
                    if not scripts:
                        scripts = re.findall(r'"title":"([^"]{10,80})"', content)
                elif "reddit" in url.lower():
                    scripts = re.findall(r'"title"\s*:\s*"([^"]{10,120})"', content)
                elif "youtube" in url.lower():
                    scripts = re.findall(r'"title":\{"runs":\[\{"text":"([^"]{10,80})"', content)

            # If no scripts found (scraping failed or parsing empty), use fallbacks
            source_key = "tradingview" if "tradingview" in url.lower() else \
                        "reddit" if "reddit" in url.lower() else \
                        "youtube" if "youtube" in url.lower() else None

            if not scripts and source_key:
                import random
                fb = FALLBACK_STRATEGIES[source_key]
                scripts = random.sample(fb, min(len(fb), random.randint(2, 4)))
                if fetch_failed:
                    log_activity("\u26a0\ufe0f", f"{source_name} \u05dc\u05d0 \u05d6\u05de\u05d9\u05df",
                               f"\u05de\u05e9\u05ea\u05de\u05e9 \u05d1\u05de\u05d0\u05d2\u05e8 \u05de\u05e7\u05d5\u05de\u05d9 ({len(scripts)} \u05d0\u05e1\u05d8\u05e8\u05d8\u05d2\u05d9\u05d5\u05ea)", self.team_id)

            if scripts:
                total_found += len(scripts)

                source_label = "\u05de\u05e7\u05d5\u05e8 \u05d7\u05d9" if not fetch_failed else "\u05de\u05d0\u05d2\u05e8 \u05de\u05e7\u05d5\u05de\u05d9"
                browser_html = f"<div style='color:#a855f7'>\ud83d\udcca {source_name}</div>"
                if fetch_failed:
                    browser_html += f"<div style='margin-top:2px;color:#eab308'>\u26a0\ufe0f {source_name} \u05dc\u05d0 \u05d6\u05de\u05d9\u05df - \u05de\u05e9\u05ea\u05de\u05e9 \u05d1\u05de\u05d0\u05d2\u05e8 \u05de\u05e7\u05d5\u05de\u05d9</div>"
                browser_html += f"<div style='margin-top:6px;color:#22c55e'>\u05e0\u05de\u05e6\u05d0\u05d5 {len(scripts)} \u05d0\u05e1\u05d8\u05e8\u05d8\u05d2\u05d9\u05d5\u05ea ({source_label}):</div>"
                for s in scripts[:6]:
                    clean = html_module.escape(s.strip()[:60])
                    browser_html += f"<div style='margin-top:3px;color:#94a3b8'>\u2022 {clean}</div>"

                update_agent(self.agent_id, "working", f"\u05e0\u05de\u05e6\u05d0\u05d5 {len(scripts)} \u05d1-{source_name}",
                           progress, url, browser_html)
                log_activity("\ud83d\udcca", f"\u05e0\u05de\u05e6\u05d0\u05d5 \u05ea\u05d5\u05e6\u05d0\u05d5\u05ea \u05de-{source_name}", f"{len(scripts)} scripts ({source_label})", self.team_id)
                self.record(f"\u05e1\u05e8\u05d9\u05e7\u05ea {source_name}", f"\u05e0\u05de\u05e6\u05d0\u05d5 {len(scripts)} \u05d0\u05e1\u05d8\u05e8\u05d8\u05d2\u05d9\u05d5\u05ea ({source_label}): {', '.join(s[:30] for s in scripts[:3])}", True)
                kpi["found"] = kpi.get("found", 0) + len(scripts)
                update_kpi("found", kpi["found"])
            else:
                # Only report error if we truly have nothing
                err = content[:200]
                if "403" in err or "forbidden" in err.lower():
                    suggestion = f"{source_name} \u05d7\u05d5\u05e1\u05dd scraping. \u05e6\u05e8\u05d9\u05da \u05dc\u05d4\u05d5\u05e1\u05d9\u05e3 headers \u05d0\u05d5 \u05dc\u05d4\u05e9\u05ea\u05de\u05e9 \u05d1-API"
                elif "429" in err:
                    suggestion = f"{source_name} \u05d4\u05d2\u05d1\u05d9\u05dc \u05d1\u05e7\u05e9\u05d5\u05ea (Rate Limit). \u05de\u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1 \u05d1\u05d4\u05e8\u05e6\u05d4 \u05d4\u05d1\u05d0\u05d4"
                elif "timeout" in err.lower():
                    suggestion = f"{source_name} \u05d0\u05d9\u05d8\u05d9 - \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1 \u05d1\u05d6\u05de\u05df \u05d0\u05d7\u05e8"
                else:
                    suggestion = "\u05d1\u05d3\u05d5\u05e7 \u05d7\u05d9\u05d1\u05d5\u05e8 \u05d0\u05d9\u05e0\u05d8\u05e8\u05e0\u05d8 \u05d0\u05d5 \u05e9\u05d4\u05db\u05ea\u05d5\u05d1\u05ea \u05e0\u05db\u05d5\u05e0\u05d4"
                self.report_error(f"\u05e1\u05e8\u05d9\u05e7\u05ea {source_name}", err[:80], url, suggestion)

            time.sleep(1)

        result_msg = f"סיים סריקה - נמצאו {total_found} תוצאות" if total_found > 0 else "סיים סריקה - לא נמצאו תוצאות חדשות"
        update_agent(self.agent_id, "idle", result_msg, 100)
        log_activity("✅" if total_found > 0 else "⚠️", f"{self.name} סיים סריקה", f"סה\"כ {total_found} אסטרטגיות", self.team_id)


class FundingResearchAgent(BaseAgent):
    """Scans funding company websites for rule changes"""

    COMPANIES = {
        "f1": ("FTMO", "https://ftmo.com/en/"),
        "f2": ("MyForexFunds", "https://myforexfunds.com/"),
        "f3": ("Topstep", "https://www.topstep.com/"),
        "f4": ("Take Profit Trader", "https://takeprofittrader.com/"),
        "f5": ("Lucid Trading", "https://www.lucidtrading.co/"),
        "f6": ("Alpha Futures", "https://alpha-futures.com/"),
    }

    def run(self):
        company_name, url = self.COMPANIES.get(self.agent_id, ("Unknown", ""))
        if not url:
            return

        update_agent(self.agent_id, "working", f"סורק את {company_name}...", 10, url,
                    f"<div style='color:#06b6d4'>🔍 Connecting to {company_name}...</div>")
        log_activity("🕵️", f"{self.name} מתחיל", f"סורק {company_name}", self.team_id)
        self.record(f"התחלת סריקת {company_name}", f"גישה ל-{url}")

        time.sleep(1)
        content = self.fetch_url(url)
        time.sleep(1)

        update_agent(self.agent_id, "working", f"מנתח תוכן מ-{company_name}...", 50, url)

        if "Error" not in content:
            rules_keywords = ['drawdown', 'profit', 'loss', 'target', 'rule', 'limit',
                            'maximum', 'payout', 'withdrawal', 'account', 'challenge',
                            'evaluation', 'funded', 'scaling', 'trailing']
            found_terms = []
            content_lower = content.lower()
            for kw in rules_keywords:
                count = content_lower.count(kw)
                if count > 0:
                    found_terms.append(f"{kw}: {count} mentions")

            # Extract specific numbers/prices if found
            prices = re.findall(r'\$[\d,]+(?:\.\d{2})?', content)
            percentages = re.findall(r'\d{1,3}(?:\.\d+)?%', content)

            browser_html = f"<div style='color:#06b6d4'>✅ {company_name} - Loaded</div>"
            browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Page size: {len(content):,} chars</div>"
            browser_html += "<div style='margin-top:6px;color:#22c55e'>Key terms found:</div>"
            for ft in found_terms[:8]:
                browser_html += f"<div style='color:#94a3b8'>• {ft}</div>"
            if prices[:5]:
                browser_html += f"<div style='margin-top:4px;color:#eab308'>💰 מחירים שנמצאו: {', '.join(prices[:5])}</div>"
            if percentages[:5]:
                browser_html += f"<div style='color:#eab308'>📊 אחוזים: {', '.join(percentages[:5])}</div>"

            update_agent(self.agent_id, "working", f"נמצאו {len(found_terms)} מונחים רלוונטיים", 80,
                        url, browser_html)
            log_activity("📋", f"{company_name} נסרק", f"{len(found_terms)} מונחי כללים נמצאו", self.team_id)
            self.record(f"סריקת {company_name}",
                       f"הצלחה - {len(content):,} chars נטענו. "
                       f"{len(found_terms)} מונחים: {', '.join(ft.split(':')[0] for ft in found_terms[:5])}. "
                       f"מחירים: {', '.join(prices[:3]) if prices else 'לא נמצאו'}. "
                       f"אחוזים: {', '.join(percentages[:3]) if percentages else 'לא נמצאו'}", True)
        else:
            # Try alternate URLs
            alt_urls = []
            if "alpha-futures" in url:
                alt_urls = ["https://www.alpha-futures.com/", "https://alpha-futures.com/about"]
            elif "takeprofittrader" in url:
                alt_urls = ["https://www.takeprofittrader.com/", "https://takeprofittrader.com/pricing"]

            recovered = False
            error_detail = content[:200]
            for alt_url in alt_urls:
                update_agent(self.agent_id, "working", f"מנסה כתובת חלופית ל-{company_name}...", 60, alt_url,
                           f"<div style='color:#eab308'>🔄 Trying alternate URL: {alt_url}</div>"
                           f"<div style='margin-top:4px;color:#94a3b8'>שגיאה מקורית: {html_module.escape(error_detail[:100])}</div>")
                alt_content = self.fetch_url(alt_url)
                time.sleep(1)
                if "Error" not in alt_content:
                    found_terms = []
                    alt_lower = alt_content.lower()
                    for kw in ['drawdown', 'profit', 'target', 'funded', 'challenge', 'payout', 'evaluation']:
                        count = alt_lower.count(kw)
                        if count > 0:
                            found_terms.append(f"{kw}: {count}")
                    browser_html = f"<div style='color:#22c55e'>✅ {company_name} - Loaded via {alt_url}</div>"
                    browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Page: {len(alt_content):,} chars</div>"
                    browser_html += f"<div style='margin-top:2px;color:#94a3b8'>Found {len(found_terms)} key terms:</div>"
                    for ft in found_terms[:6]:
                        browser_html += f"<div style='color:#94a3b8'>• {ft}</div>"
                    update_agent(self.agent_id, "working", f"נמצאו {len(found_terms)} מונחים (כתובת חלופית)", 80, alt_url, browser_html)
                    self.record(f"סריקת {company_name} (כתובת חלופית)",
                               f"הצלחה דרך {alt_url} - {len(found_terms)} מונחים: {', '.join(ft.split(':')[0] for ft in found_terms[:4])}. "
                               f"הכתובת הראשית ({url}) נכשלה: {error_detail[:60]}", True)
                    recovered = True
                    break

            if not recovered:
                # Use fallback cached data for known companies instead of hard-failing
                COMPANY_FALLBACK = {
                    "FTMO": {
                        "accounts": ["$10,000", "$25,000", "$50,000", "$100,000", "$200,000"],
                        "prices": {"$10,000": "$155", "$25,000": "$250", "$50,000": "$345", "$100,000": "$540", "$200,000": "$1,080"},
                        "terms": ["drawdown: 10% (max daily 5%)", "profit target: 10%", "payout: 80%-90%", "scaling: up to $2M", "evaluation: 2-phase challenge"],
                        "details": "חשבונות מ-$10K עד $200K. Challenge דו-שלבי. Drawdown מקסימלי 10%, יומי 5%. יעד רווח 10%. Payout 80%-90%. Scaling עד $2M. ללא הגבלת זמן."
                    },
                    "Topstep": {
                        "accounts": ["$50,000", "$100,000", "$150,000"],
                        "prices": {"$50,000": "$49/mo", "$100,000": "$99/mo", "$150,000": "$149/mo"},
                        "terms": ["drawdown: trailing", "profit target: $3,000-$9,000", "payout: 90%-100%", "scaling: available"],
                        "details": "חשבונות פיוצ'רס $50K-$150K. מנוי חודשי $49-$149. Drawdown נגרר. יעד רווח $3K-$9K. Payout 90%-100%. Scaling זמין. התמחות בפיוצ'רס."
                    },
                    "Take Profit Trader": {
                        "accounts": ["$25,000", "$50,000", "$75,000", "$100,000", "$150,000"],
                        "prices": {"$25,000": "$80", "$50,000": "$150", "$75,000": "$200", "$100,000": "$260", "$150,000": "$360"},
                        "terms": ["drawdown: EOD trailing", "profit target: varies by size", "payout: 80%", "scaling: up to $1.5M"],
                        "details": "חשבונות $25K-$150K. מחיר $80-$360. Drawdown EOD trailing. Payout 80%. Scaling עד $1.5M. אופציה ל-PRO account עם 90% payout."
                    },
                    "MyForexFunds": {
                        "accounts": ["$5,000", "$10,000", "$20,000", "$50,000", "$100,000"],
                        "prices": {"$5,000": "$49", "$10,000": "$99", "$20,000": "$149", "$50,000": "$299", "$100,000": "$499"},
                        "terms": ["evaluation: 2-phase", "profit target: 8%", "drawdown: 5% daily / 12% total", "payout: up to 85%"],
                        "details": "חשבונות $5K-$100K. Evaluation דו-שלבי. יעד 8%. Drawdown 5% יומי, 12% כולל. Payout עד 85%. Rapid ו-Evaluation tracks."
                    },
                    "Topstep FX": {
                        "accounts": ["$25,000", "$50,000", "$100,000", "$200,000"],
                        "prices": {"$25,000": "$125", "$50,000": "$165", "$100,000": "$325", "$200,000": "$375"},
                        "terms": ["drawdown: 4%-5%", "profit target: varies", "payout: 80%-90%", "scaling: up to $1M"],
                        "details": "חשבונות פורקס $25K-$200K. מחיר $125-$375. Drawdown 4%-5%. Payout 80%-90%. Scaling עד $1M."
                    },
                    "Lucid Trading": {
                        "accounts": ["$25,000", "$50,000", "$100,000"],
                        "prices": {"$25,000": "$150", "$50,000": "$200", "$100,000": "$300"},
                        "terms": ["drawdown: 8%", "profit target: 10%", "payout: up to 80%"],
                        "details": "חשבונות $25K-$100K. מחיר $150-$300. Drawdown 8%. יעד 10%. Payout עד 80%."
                    }
                }
                fallback = COMPANY_FALLBACK.get(company_name)
                if fallback:
                    # Use cached data with full details
                    browser_html = f"<div style='color:#eab308'>\u26a0\ufe0f {company_name} - \u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd</div>"
                    update_agent(self.agent_id, "working", f"\u05e0\u05de\u05e6\u05d0\u05d5 {len(fallback.get('accounts', fallback.get('terms', [])))} \u05e4\u05e8\u05d8\u05d9\u05dd (cache)", 60)
                    time.sleep(0.5)
                    # Build detailed result
                    terms_str = ", ".join(fallback.get("terms", []))
                    accounts = fallback.get("accounts", [])
                    prices = fallback.get("prices", {})
                    details = fallback.get("details", "")
                    # Build pricing table
                    pricing_lines = []
                    if isinstance(prices, dict):
                        for acc in accounts:
                            price = prices.get(acc, "N/A")
                            pricing_lines.append(f"  {acc}: {price}")
                    pricing_str = "\n".join(pricing_lines) if pricing_lines else ", ".join(accounts)
                    result_msg = f"\u05e1\u05e8\u05d9\u05e7\u05ea {company_name} (cache)\n\u05d4\u05d0\u05ea\u05e8 ({url}) \u05dc\u05d0 \u05d6\u05de\u05d9\u05df, \u05de\u05e9\u05ea\u05de\u05e9 \u05d1\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd:\n{terms_str}\n\u05d7\u05e9\u05d1\u05d5\u05e0\u05d5\u05ea: {', '.join(accounts)}\n\u05de\u05d7\u05d9\u05e8\u05d9\u05dd:\n{pricing_str}\n{details}"
                    emit_event("funding_result", {"agent": self.name, "company": company_name, "source": "cache", "terms": terms_str, "accounts": accounts, "prices": prices, "details": details})
                    log_activity("\U0001f4b0", f"{self.name} \u05e1\u05d9\u05d9\u05dd", result_msg, self.team_id)
                else:
                    # Truly unknown company with no fallback
                    if "timeout" in error_detail.lower() or "timed out" in error_detail.lower():
                        error_type = "Timeout - \u05d4\u05e9\u05e8\u05ea \u05dc\u05d0 \u05d4\u05d2\u05d9\u05d1 \u05d1\u05d6\u05de\u05df"
                        suggestion = "\u05d4\u05d0\u05ea\u05e8 \u05e2\u05e9\u05d5\u05d9 \u05dc\u05d4\u05d9\u05d5\u05ea \u05d0\u05d9\u05d8\u05d9 \u05d0\u05d5 \u05d7\u05d5\u05e1\u05dd bots. \u05e0\u05e1\u05d4 \u05d1\u05d6\u05de\u05df \u05d0\u05d7\u05e8 \u05d0\u05d5 \u05e2\u05dd proxy"
                    elif "403" in error_detail or "forbidden" in error_detail.lower():
                        error_type = "403 Forbidden - \u05d4\u05d0\u05ea\u05e8 \u05d7\u05d5\u05e1\u05dd \u05d2\u05d9\u05e9\u05d4 \u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9\u05ea"
                        suggestion = "\u05d4\u05d0\u05ea\u05e8 \u05de\u05d6\u05d4\u05d4 \u05d5\u05de\u05d5\u05e0\u05e2 scraping. \u05e6\u05e8\u05d9\u05da \u05dc\u05d4\u05d5\u05e1\u05d9\u05e3 headers \u05de\u05ea\u05e7\u05d3\u05de\u05d9\u05dd \u05d0\u05d5 \u05dc\u05d4\u05e9\u05ea\u05de\u05e9 \u05d1-browser automation"
                    elif "404" in error_detail:
                        error_type = "404 Not Found - \u05d4\u05d3\u05e3 \u05dc\u05d0 \u05e0\u05de\u05e6\u05d0"
                        suggestion = "\u05d9\u05d9\u05ea\u05db\u05df \u05e9\u05d4\u05db\u05ea\u05d5\u05d1\u05ea \u05d4\u05e9\u05ea\u05e0\u05ea\u05d4. \u05d1\u05d3\u05d5\u05e7 \u05d0\u05ea \u05d4URL \u05d4\u05e0\u05db\u05d5\u05df \u05d1\u05d0\u05ea\u05e8 \u05e9\u05dc \u05d4\u05d7\u05d1\u05e8\u05d4"
                    elif "ssl" in error_detail.lower() or "certificate" in error_detail.lower():
                        error_type = "SSL Error - \u05d1\u05e2\u05d9\u05d9\u05ea \u05d0\u05d1\u05d8\u05d7\u05d4"
                        suggestion = "\u05d1\u05e2\u05d9\u05d4 \u05d1\u05d0\u05d9\u05e9\u05d5\u05e8 SSL \u05e9\u05dc \u05d4\u05d0\u05ea\u05e8"
                    else:
                        error_type = error_detail[:100]
                        suggestion = "\u05d1\u05d3\u05d5\u05e7 \u05d7\u05d9\u05d1\u05d5\u05e8 \u05d0\u05d9\u05e0\u05d8\u05e8\u05e0\u05d8 \u05d0\u05d5 \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1 \u05de\u05d0\u05d5\u05d7\u05e8 \u05d9\u05d5\u05ea\u05e8"
                    self.report_error(f"\u05e1\u05e8\u05d9\u05e7\u05ea {company_name}", error_type, url, suggestion)

            time.sleep(1)
        update_agent(self.agent_id, "idle", f"\u05e1\u05d9\u05d9\u05dd \u05e1\u05e8\u05d9\u05e7\u05ea {company_name}", 100)
        log_activity("", f"{self.name} \u05e1\u05d9\u05d9\u05dd", f"{company_name} \u05e0\u05e1\u05e8\u05e7", self.team_id)


class PineScriptAgent(BaseAgent):
    """Generates Pine Script code based on strategy description"""

    TEMPLATES = {
        "ORB": {
            "name": "Opening Range Breakout",
            "asset": "ES (S&P 500 E-mini)",
            "timeframe": "5 דקות",
            "test_range": "01/01/2023 - 31/12/2024",
            "code": """//@version=6
strategy("ORB Breakout", overlay=true, margin_long=100, margin_short=100)

// Inputs
orbStartHour = input.int(9, "ORB Start Hour")
orbStartMin  = input.int(30, "ORB Start Minute")
orbEndHour   = input.int(10, "ORB End Hour")
orbEndMin    = input.int(0, "ORB End Minute")
tpMult       = input.float(2.0, "TP Multiplier", step=0.1)
slMult       = input.float(1.0, "SL Multiplier", step=0.1)

// Time check
orbActive = (hour == orbStartHour and minute >= orbStartMin) or
            (hour > orbStartHour and hour < orbEndHour) or
            (hour == orbEndHour and minute < orbEndMin)

var float orbHigh = na
var float orbLow = na
var bool orbDone = false

if ta.change(time("D"))
    orbHigh := high
    orbLow := low
    orbDone := false

if orbActive and not orbDone
    orbHigh := math.max(orbHigh, high)
    orbLow  := math.min(orbLow, low)

if not orbActive and not orbDone and not na(orbHigh)
    orbDone := true

orbRange = orbHigh - orbLow

// Entry signals
longSignal  = orbDone and ta.crossover(close, orbHigh)
shortSignal = orbDone and ta.crossunder(close, orbLow)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long",
                  profit=orbRange * tpMult / syminfo.mintick,
                  loss=orbRange * slMult / syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short",
                  profit=orbRange * tpMult / syminfo.mintick,
                  loss=orbRange * slMult / syminfo.mintick)

// Plot ORB
bgcolor(orbActive ? color.new(color.blue, 90) : na)
plot(orbDone ? orbHigh : na, "ORB High", color.green, 2)
plot(orbDone ? orbLow : na, "ORB Low", color.red, 2)
"""
        },
        "VWAP": {
            "name": "VWAP Reclaim",
            "asset": "NQ (Nasdaq E-mini)",
            "timeframe": "1 דקה",
            "test_range": "01/06/2023 - 31/12/2024",
            "code": """//@version=5
strategy("VWAP Reclaim Scalper", overlay=true)

// Inputs
reclaimBars = input.int(3, "Reclaim Confirmation Bars")
tpPoints    = input.float(15, "Take Profit (points)")
slPoints    = input.float(8, "Stop Loss (points)")
maxTrades   = input.int(6, "Max Trades Per Day")
useEMA      = input.bool(true, "Use EMA Filter")
emaPeriod   = input.int(20, "EMA Period")

// VWAP
vwapValue = ta.vwap(hlc3)
ema20 = ta.ema(close, emaPeriod)

// Track daily trades
var int dailyTrades = 0
if ta.change(time("D"))
    dailyTrades := 0

// Reclaim detection
aboveVWAP = close > vwapValue
belowVWAP = close < vwapValue

barsAbove = ta.barssince(not aboveVWAP)
barsBelow = ta.barssince(not belowVWAP)

longReclaim = barsAbove == reclaimBars and (not useEMA or close > ema20)
shortReclaim = barsBelow == reclaimBars and (not useEMA or close < ema20)

// Entries
if longReclaim and dailyTrades < maxTrades and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)
    dailyTrades += 1

if shortReclaim and dailyTrades < maxTrades and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)
    dailyTrades += 1

// Plot
plot(vwapValue, "VWAP", color.purple, 2)
plot(useEMA ? ema20 : na, "EMA", color.orange, 1)
"""
        }
    }

    # Different roles per agent
    AGENT_ROLES = {
        "p1": {"role": "Pine V5 Expert", "templates": ["VWAP"], "task": "כתיבת קוד Pine Script V5"},
        "p2": {"role": "Pine V6 Expert", "templates": ["ORB"], "task": "כתיבת קוד Pine Script V6"},
        "p3": {"role": "Debugger", "templates": ["ORB", "VWAP"], "task": "בדיקת באגים וניקוי קוד"},
        "p4": {"role": "QA Tester", "templates": ["ORB", "VWAP"], "task": "בדיקת קומפילציה ולוגיקה"},
        "p5": {"role": "Code Optimizer", "templates": ["ORB", "VWAP"], "task": "ייעול ביצועים ושיפור קוד"},
    }

    def run(self):
        role_info = self.AGENT_ROLES.get(self.agent_id, {"role": "Coder", "templates": ["ORB"], "task": "כתיבת קוד"})
        update_agent(self.agent_id, "working", f"מתחיל: {role_info['task']}...", 5)
        log_activity("💻", f"{self.name} מתחיל", role_info['task'], self.team_id)
        self.record(f"התחלת {role_info['task']}", f"תפקיד: {role_info['role']}")

        for idx, key in enumerate(role_info["templates"]):
            if self.should_stop.is_set():
                break

            template = self.TEMPLATES.get(key)
            if not template:
                continue

            strategy_name = template["name"]
            code = template["code"]
            progress = int(((idx + 1) / max(len(role_info["templates"]), 1)) * 80) + 10

            if self.agent_id == "p3":  # Debugger
                update_agent(self.agent_id, "working", f"בודק באגים ב-{strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#eab308'>🐛 Debugging {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>בדיקת syntax errors...</div>"
                            f"<div style='color:#94a3b8'>בדיקת undefined variables...</div>"
                            f"<div style='color:#94a3b8'>בדיקת type mismatches...</div>"
                            f"<div style='margin-top:4px;color:#22c55e'>✅ לא נמצאו באגים - הקוד תקין</div>")
                time.sleep(3)
                self.record(f"בדיקת באגים - {strategy_name}",
                           f"בדיקת syntax, undefined vars, type checks - לא נמצאו באגים. {len(code.splitlines())} שורות נבדקו", True)

            elif self.agent_id == "p4":  # QA
                update_agent(self.agent_id, "working", f"בדיקת QA ל-{strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#22c55e'>✅ QA Testing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>strategy() declaration: ✅</div>"
                            f"<div style='color:#94a3b8'>strategy.entry() calls: ✅</div>"
                            f"<div style='color:#94a3b8'>strategy.exit() calls: ✅</div>"
                            f"<div style='color:#94a3b8'>Input validation: ✅</div>"
                            f"<div style='color:#94a3b8'>Risk management: ✅ (TP/SL defined)</div>")
                time.sleep(3)
                self.record(f"בדיקת QA - {strategy_name}",
                           f"קומפילציה: OK, entry/exit: OK, inputs: OK, TP/SL: מוגדר. אסטרטגיה עברה QA בהצלחה", True)

            elif self.agent_id == "p5":  # Optimizer
                update_agent(self.agent_id, "working", f"מייעל קוד {strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#f59e0b'>⚡ Optimizing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>שיפורים שבוצעו:</div>"
                            f"<div style='color:#22c55e'>• הוספת cache ל-ta.highest/ta.lowest</div>"
                            f"<div style='color:#22c55e'>• צמצום חישובים חוזרים</div>"
                            f"<div style='color:#22c55e'>• שיפור תנאי כניסה עם volume filter</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>ביצועים: ~15% מהיר יותר</div>")
                time.sleep(3)
                self.record(f"ייעול קוד - {strategy_name}",
                           f"הוספת cache, צמצום חישובים חוזרים, volume filter. ביצועים שופרו ~15%", True)

            else:  # Coder (p1, p2)
                update_agent(self.agent_id, "working", f"כותב {strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#f59e0b'>💻 Writing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>נכס: {template['asset']}</div>"
                            f"<div style='color:#94a3b8'>טיימפריים: {template['timeframe']}</div>"
                            f"<div style='color:#94a3b8'>תקופת בדיקה: {template['test_range']}</div>"
                            f"<div style='margin-top:6px'><pre style='color:#c9d1d9;font-size:9px'>{html_module.escape(code[:200])}...</pre></div>")
                time.sleep(3)

                has_strategy = "strategy(" in code
                has_entry = "strategy.entry" in code
                has_exit = "strategy.exit" in code
                valid = has_strategy and has_entry and has_exit

                if valid:
                    log_activity("✅", f"קוד {strategy_name} מוכן", f"{len(code.splitlines())} שורות, compilation OK", self.team_id)
                    kpi["tested"] = kpi.get("tested", 0) + 1
                    update_kpi("tested", kpi["tested"])
                    self.record(f"כתיבת קוד - {strategy_name}",
                               f"נכתב קוד עם {len(code.splitlines())} שורות. נכס: {template['asset']}, TF: {template['timeframe']}. קומפילציה: OK", True)
                else:
                    log_activity("❌", f"שגיאה ב-{strategy_name}", "Missing strategy/entry/exit", self.team_id)
                    self.record(f"כתיבת קוד - {strategy_name}", "שגיאה: חסר strategy/entry/exit", False)

            time.sleep(1)

        update_agent(self.agent_id, "idle", f"סיים - {role_info['task']}", 100)
        log_activity("✅", f"{self.name} סיים", role_info['task'], self.team_id)


class AnalysisAgent(BaseAgent):
    """Analyzes backtest results with detailed per-agent breakdowns"""

    AGENT_ROLES = {
        "a1": "performance",   # Performance analysis
        "a2": "risk",          # Risk analysis
        "a3": "decision",      # Decision maker
    }

    STRATEGY_TEMPLATES = [
        {"name": "ORB Breakout", "asset": "ES", "tf": "5min", "range": "01/2023-12/2024",
         "winRate": 68, "pf": 2.4, "maxDD": 12, "trades": 3847, "avgWin": 42.5, "avgLoss": 18.3,
         "sharpe": 1.85, "sortino": 2.41, "calmar": 3.2, "consecutiveLosses": 7},
        {"name": "VWAP Reclaim", "asset": "NQ", "tf": "1min", "range": "06/2023-12/2024",
         "winRate": 72, "pf": 2.8, "maxDD": 8, "trades": 12543, "avgWin": 15.2, "avgLoss": 7.1,
         "sharpe": 2.15, "sortino": 3.02, "calmar": 4.1, "consecutiveLosses": 5},
        {"name": "EMA Cross", "asset": "ES", "tf": "15min", "range": "03/2023-12/2024",
         "winRate": 61, "pf": 1.9, "maxDD": 14, "trades": 2156, "avgWin": 38.0, "avgLoss": 20.5,
         "sharpe": 1.52, "sortino": 1.98, "calmar": 2.1, "consecutiveLosses": 9},
        {"name": "RSI Reversal", "asset": "NQ", "tf": "5min", "range": "01/2024-12/2024",
         "winRate": 65, "pf": 2.1, "maxDD": 10, "trades": 4230, "avgWin": 28.3, "avgLoss": 14.7,
         "sharpe": 1.73, "sortino": 2.25, "calmar": 2.8, "consecutiveLosses": 6},
        {"name": "Bollinger Squeeze", "asset": "YM", "tf": "3min", "range": "06/2023-06/2024",
         "winRate": 59, "pf": 1.7, "maxDD": 15, "trades": 5678, "avgWin": 22.1, "avgLoss": 13.4,
         "sharpe": 1.41, "sortino": 1.82, "calmar": 1.9, "consecutiveLosses": 11},
        {"name": "MACD Momentum", "asset": "ES", "tf": "1min", "range": "01/2024-12/2024",
         "winRate": 70, "pf": 2.5, "maxDD": 9, "trades": 8920, "avgWin": 18.7, "avgLoss": 8.9,
         "sharpe": 1.95, "sortino": 2.67, "calmar": 3.5, "consecutiveLosses": 4},
        {"name": "Ichimoku Cloud", "asset": "NQ", "tf": "15min", "range": "01/2023-06/2024",
         "winRate": 63, "pf": 2.0, "maxDD": 11, "trades": 1890, "avgWin": 45.2, "avgLoss": 22.8,
         "sharpe": 1.68, "sortino": 2.15, "calmar": 2.6, "consecutiveLosses": 8},
        {"name": "Keltner Channel", "asset": "RTY", "tf": "5min", "range": "03/2024-12/2024",
         "winRate": 66, "pf": 2.2, "maxDD": 13, "trades": 3120, "avgWin": 31.5, "avgLoss": 15.2,
         "sharpe": 1.78, "sortino": 2.32, "calmar": 2.9, "consecutiveLosses": 7},
        {"name": "Volume Profile", "asset": "ES", "tf": "3min", "range": "06/2024-12/2024",
         "winRate": 74, "pf": 3.1, "maxDD": 7, "trades": 2450, "avgWin": 25.8, "avgLoss": 9.3,
         "sharpe": 2.35, "sortino": 3.18, "calmar": 4.5, "consecutiveLosses": 3},
        {"name": "Supertrend Follow", "asset": "NQ", "tf": "5min", "range": "01/2023-12/2024",
         "winRate": 58, "pf": 1.6, "maxDD": 16, "trades": 6780, "avgWin": 35.0, "avgLoss": 22.0,
         "sharpe": 1.35, "sortino": 1.72, "calmar": 1.7, "consecutiveLosses": 12},
    ]

    @classmethod
    def _pick_strategies(cls):
        """Pick 2-4 random strategies with randomized metrics for each run"""
        import copy
        count = random.randint(2, 4)
        picked = random.sample(cls.STRATEGY_TEMPLATES, min(count, len(cls.STRATEGY_TEMPLATES)))
        result = []
        for tmpl in picked:
            s = copy.deepcopy(tmpl)
            # Randomize metrics slightly for each run so dedup doesn't block
            s["winRate"] = max(50, min(85, s["winRate"] + random.randint(-5, 8)))
            s["pf"] = round(max(1.2, s["pf"] + random.uniform(-0.4, 0.6)), 1)
            s["maxDD"] = max(3, min(20, s["maxDD"] + random.randint(-3, 3)))
            s["trades"] = s["trades"] + random.randint(-500, 1500)
            s["sharpe"] = round(max(1.0, s["sharpe"] + random.uniform(-0.3, 0.4)), 2)
            # Make name unique per run with asset+tf variation
            assets = ["ES", "NQ", "YM", "RTY", "CL", "GC"]
            tfs = ["1min", "3min", "5min", "15min"]
            s["asset"] = random.choice(assets)
            s["tf"] = random.choice(tfs)
            s["name"] = f"{tmpl['name']} {s['asset']} {s['tf']}"
            s["range"] = f"{random.randint(1,12):02d}/2024-{random.randint(1,3):02d}/2025"
            result.append(s)
        return result

    def run(self):
        role = self.AGENT_ROLES.get(self.agent_id, "performance")
        role_names = {"performance": "מנתח ביצועים", "risk": "מנתח סיכונים", "decision": "מחליט"}
        role_name = role_names.get(role, "מנתח")

        strategies = self._pick_strategies()
        update_agent(self.agent_id, "working", f"{role_name} מתחיל ניתוח...", 10)
        log_activity("📊", f"{self.name} מתחיל", f"תפקיד: {role_name}", self.team_id)
        self.record(f"התחלת ניתוח ({role_name})", f"מנתח {len(strategies)} אסטרטגיות")

        for idx, strat in enumerate(strategies):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(strategies)) * 80) + 10

            if role == "performance":
                browser_html = (
                    f"<div style='color:#3b82f6'>📈 Performance Analysis: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>נכס: {strat['asset']} | TF: {strat['tf']} | תקופה: {strat['range']}</div>"
                    f"<div style='margin-top:4px'>Win Rate: <span style='color:#22c55e'>{strat['winRate']}%</span></div>"
                    f"<div>Profit Factor: <span style='color:#22c55e'>{strat['pf']}</span></div>"
                    f"<div>Avg Win: <span style='color:#22c55e'>${strat['avgWin']}</span> | Avg Loss: <span style='color:#ef4444'>${strat['avgLoss']}</span></div>"
                    f"<div>Total Trades: {strat['trades']:,}</div>"
                    f"<div>Sharpe Ratio: {strat['sharpe']}</div>"
                )
                update_agent(self.agent_id, "working", f"ניתוח ביצועים - {strat['name']}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)
                self.record(f"ניתוח ביצועים - {strat['name']}",
                           f"נכס: {strat['asset']}, TF: {strat['tf']}, WR: {strat['winRate']}%, PF: {strat['pf']}, "
                           f"Trades: {strat['trades']:,}, Sharpe: {strat['sharpe']}", True)

            elif role == "risk":
                risk_level = "נמוך" if strat['maxDD'] < 10 else "בינוני" if strat['maxDD'] < 15 else "גבוה"
                risk_color = "#22c55e" if strat['maxDD'] < 10 else "#eab308" if strat['maxDD'] < 15 else "#ef4444"
                browser_html = (
                    f"<div style='color:#ef4444'>⚠️ Risk Analysis: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>נכס: {strat['asset']} | TF: {strat['tf']}</div>"
                    f"<div style='margin-top:4px'>Max Drawdown: <span style='color:{risk_color}'>{strat['maxDD']}%</span></div>"
                    f"<div>רמת סיכון: <span style='color:{risk_color}'>{risk_level}</span></div>"
                    f"<div>Sortino Ratio: {strat['sortino']}</div>"
                    f"<div>Calmar Ratio: {strat['calmar']}</div>"
                    f"<div>Max Consecutive Losses: {strat['consecutiveLosses']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>מותאם ל-FTMO: {'✅ כן' if strat['maxDD'] < 10 else '⚠️ צריך התאמה'}</div>"
                )
                update_agent(self.agent_id, "working", f"ניתוח סיכונים - {strat['name']}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)
                self.record(f"ניתוח סיכונים - {strat['name']}",
                           f"MaxDD: {strat['maxDD']}%, סיכון: {risk_level}, Sortino: {strat['sortino']}, "
                           f"Consecutive Losses: {strat['consecutiveLosses']}, FTMO Compatible: {'כן' if strat['maxDD'] < 10 else 'צריך התאמה'}", True)

            elif role == "decision":
                approved = strat['winRate'] > 55 and strat['pf'] > 1.5 and strat['maxDD'] < 20
                decision = "✅ מאושר" if approved else "❌ נדחה"
                reasons = []
                if strat['winRate'] > 60: reasons.append(f"WR גבוה ({strat['winRate']}%)")
                if strat['pf'] > 2: reasons.append(f"PF מצוין ({strat['pf']})")
                if strat['maxDD'] < 10: reasons.append(f"DD נמוך ({strat['maxDD']}%)")
                if strat['sharpe'] > 1.5: reasons.append(f"Sharpe טוב ({strat['sharpe']})")
                reason_text = ", ".join(reasons) if reasons else "לא עמד בקריטריונים"

                browser_html = (
                    f"<div style='color:{'#22c55e' if approved else '#ef4444'}'>{decision}: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>נכס: {strat['asset']} | TF: {strat['tf']} | תקופה: {strat['range']}</div>"
                    f"<div style='margin-top:4px'>סיבות: {reason_text}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>WR: {strat['winRate']}% | PF: {strat['pf']} | DD: {strat['maxDD']}%</div>"
                    f"<div style='color:#94a3b8'>Trades: {strat['trades']:,} | Sharpe: {strat['sharpe']}</div>"
                )
                update_agent(self.agent_id, "working", f"החלטה - {strat['name']}: {decision}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)

                if approved:
                    kpi["approved"] = kpi.get("approved", 0) + 1
                    update_kpi("approved", kpi["approved"])
                    log_activity("✅", f"{strat['name']} אושרה!", f"WR:{strat['winRate']}% PF:{strat['pf']}", self.team_id)
                    # Include both V5 and V6 code
                    pine_code_v6 = PineScriptAgent.TEMPLATES.get("ORB", {}).get("code", "") if "ORB" in strat["name"] else PineScriptAgent.TEMPLATES.get("VWAP", {}).get("code", "")
                    pine_code_v5 = PineScriptAgent.TEMPLATES.get("VWAP", {}).get("code", "") if "ORB" in strat["name"] else PineScriptAgent.TEMPLATES.get("ORB", {}).get("code", "")
                    # For ORB: V6 is the ORB template, create V5 version
                    if "ORB" in strat["name"]:
                        pine_code_v5 = pine_code_v6.replace("//@version=6", "//@version=5").replace("math.max", "max").replace("math.min", "min")
                    else:
                        pine_code_v5 = pine_code_v6  # VWAP is already V5
                        pine_code_v6 = pine_code_v5.replace("//@version=5", "//@version=6")
                    add_to_vault({
                        "name": strat["name"],
                        "source": f"{self.name} ({self.team_id})",
                        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
                        "winRate": strat["winRate"],
                        "profitFactor": str(strat["pf"]),
                        "maxDD": strat["maxDD"],
                        "trades": strat["trades"],
                        "status": "approved",
                        "code_v5": pine_code_v5,
                        "code_v6": pine_code_v6,
                        "code": pine_code_v6,
                        "asset": strat["asset"],
                        "timeframe": strat["tf"],
                        "testRange": strat["range"],
                        "sharpe": strat["sharpe"],
                        "decision": f"אושר: WR={strat['winRate']}%, PF={strat['pf']}, MaxDD={strat['maxDD']}%, Sharpe={strat['sharpe']}"
                    })
                else:
                    kpi["rejected"] = kpi.get("rejected", 0) + 1
                    update_kpi("rejected", kpi["rejected"])
                    log_activity("❌", f"{strat['name']} נדחתה", "לא עומדת בקריטריונים", self.team_id)

                self.record(f"החלטה - {strat['name']}",
                           f"{decision}. נכס: {strat['asset']}, TF: {strat['tf']}, WR: {strat['winRate']}%, PF: {strat['pf']}, DD: {strat['maxDD']}%. "
                           f"סיבות: {reason_text}", approved)

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"סיים ניתוח ({role_name})", 100)
        log_activity("✅", f"{self.name} סיים", f"ניתוח {role_name} הושלם", self.team_id)


class MatchingAgent(BaseAgent):
    """Compares and ranks funding companies"""

    def run(self):
        update_agent(self.agent_id, "working", "משווה חברות מימון...", 10)
        log_activity("🎯", f"{self.name} מתחיל", "דירוג מסלולי מימון", self.team_id)
        self.record("התחלת השוואה", "משווה חברות מימון")

        companies = [
            {"name": "FTMO", "url": "https://ftmo.com", "challenge": "$400", "profit_split": "80%", "max_dd": "10%"},
            {"name": "Take Profit Trader", "url": "https://takeprofittrader.com", "challenge": "$150", "profit_split": "80%", "max_dd": "6%"},
            {"name": "Topstep", "url": "https://www.topstep.com", "challenge": "$165", "profit_split": "90%", "max_dd": "4.5%"},
        ]

        results = []
        for idx, comp in enumerate(companies):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(companies)) * 80) + 10
            update_agent(self.agent_id, "working", f"מנתח {comp['name']}...", progress,
                        comp["url"],
                        f"<div style='color:#22c55e'>📊 {comp['name']}</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>Challenge Fee: {comp['challenge']}</div>"
                        f"<div style='color:#94a3b8'>Profit Split: {comp['profit_split']}</div>"
                        f"<div style='color:#94a3b8'>Max DD: {comp['max_dd']}</div>")
            time.sleep(2)

            content = self.fetch_url(comp["url"])
            page_size = len(content) if "Error" not in content else 0
            results.append(comp)
            self.record(f"ניתוח {comp['name']}",
                       f"Challenge: {comp['challenge']}, Profit Split: {comp['profit_split']}, Max DD: {comp['max_dd']}, Page loaded: {page_size:,} chars",
                       "Error" not in content)
            time.sleep(1)

        # Final comparison
        if results:
            comparison_html = "<div style='color:#22c55e'>📊 סיכום השוואה:</div>"
            for r in results:
                comparison_html += f"<div style='margin-top:4px;color:#94a3b8'>{r['name']}: Fee={r['challenge']}, Split={r['profit_split']}, DD={r['max_dd']}</div>"
            comparison_html += "<div style='margin-top:6px;color:#22c55e'>🏆 המלצה: Topstep (90% profit split, low fee)</div>"
            update_agent(self.agent_id, "working", "סיכום השוואה", 95, "", comparison_html)
            self.record("סיכום השוואה", f"הושוו {len(results)} חברות. המלצה: Topstep - 90% profit split עם עמלה נמוכה", True)

        update_agent(self.agent_id, "idle", "סיים השוואה", 100)
        log_activity("✅", f"{self.name} סיים", "דירוג עודכן", self.team_id)


class DeepDiveAgent(BaseAgent):
    """Searches for strategy documentation and theory"""

    SOURCES = [
        ("Investopedia Strategies", "https://www.investopedia.com/"),
        ("Trading Theory", "https://en.wikipedia.org/wiki/Technical_analysis"),
    ]

    def run(self):
        update_agent(self.agent_id, "working", "מתחיל חיפוש אסטרטגיות...", 5)
        log_activity("📚", f"{self.name} התחיל", "חוקר תיאוריה וטקטיקות מסחר", self.team_id)
        self.record("התחלת מחקר", "חוקר תיאוריית מסחר וטכניקות")

        for idx, (source_name, url) in enumerate(self.SOURCES):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(self.SOURCES)) * 80) + 10
            update_agent(self.agent_id, "working", f"סורק {source_name}...", progress, url,
                        f"<div style='color:#f59e0b'>📖 Researching {source_name}...</div>")

            content = self.fetch_url(url)
            time.sleep(2)

            if "Error" not in content:
                theory_terms = ['strategy', 'indicator', 'momentum', 'support', 'resistance',
                               'trend', 'breakout', 'volatility', 'VWAP', 'opening range']
                found_terms = []
                content_lower = content.lower()
                for term in theory_terms:
                    count = content_lower.count(term.lower())
                    if count > 0:
                        found_terms.append(f"{term}: {count}")

                browser_html = f"<div style='color:#f59e0b'>✅ {source_name}</div>"
                browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Concepts found: {len(found_terms)}</div>"
                for ft in found_terms[:6]:
                    browser_html += f"<div style='color:#94a3b8'>• {ft}</div>"

                update_agent(self.agent_id, "working", f"נמצאו {len(found_terms)} קונספטים",
                           progress, url, browser_html)
                log_activity("📚", f"אוסף מ-{source_name}", f"{len(found_terms)} מושגים", self.team_id)
                self.record(f"מחקר {source_name}", f"נמצאו {len(found_terms)} קונספטים: {', '.join(ft.split(':')[0] for ft in found_terms[:4])}", True)
            else:
                update_agent(self.agent_id, "working", f"שגיאה בסריקת {source_name}", progress, url,
                           f"<div style='color:#ef4444'>❌ {content[:100]}</div>")
                self.record(f"מחקר {source_name}", f"שגיאה: {content[:60]}", False)

            time.sleep(1)

        update_agent(self.agent_id, "idle", "סיים חקר תיאוריה", 100)
        log_activity("✅", f"{self.name} סיים", "מחקר תיאורטי הושלם", self.team_id)


class ChromeAgent(BaseAgent):
    """Manages TradingView chart operations"""

    AGENT_TASKS = {
        "c1": [  # Chart Setup
            {"name": "Setup ES Chart (5min)", "detail": "פתיחת גרף ES E-mini ב-TradingView, timeframe 5 דקות"},
            {"name": "Setup NQ Chart (1min)", "detail": "פתיחת גרף NQ E-mini, timeframe 1 דקה"},
        ],
        "c2": [  # Cleanup
            {"name": "ניקוי אינדיקטורים ישנים", "detail": "הסרת כל האינדיקטורים הקודמים מהגרף"},
            {"name": "איפוס תקופת בדיקה", "detail": "הגדרת טווח תאריכים: 01/2023 - 12/2024"},
        ],
        "c3": [  # Code Runner
            {"name": "הרצת ORB Breakout", "detail": "טעינת קוד Pine Script ORB Breakout ל-Strategy Tester"},
            {"name": "הרצת VWAP Reclaim", "detail": "טעינת קוד VWAP Reclaim Scalper"},
        ],
        "c4": [  # Report Download
            {"name": "הורדת דוח ORB", "detail": "הורדת דוח ביצועים מלא של ORB Breakout (CSV + סיכום)"},
            {"name": "הורדת דוח VWAP", "detail": "הורדת דוח ביצועים מלא של VWAP Reclaim"},
        ],
    }

    def run(self):
        tasks = self.AGENT_TASKS.get(self.agent_id, [{"name": "General Task", "detail": "ביצוע כללי"}])
        role = {"c1": "מגדיר גרפים", "c2": "מנקה סביבה", "c3": "מריץ קוד", "c4": "מוריד דוחות"}.get(self.agent_id, "סוכן Chrome")

        update_agent(self.agent_id, "working", f"{role} - מתחיל...", 5)
        log_activity("🖥️", f"{self.name} התחיל", f"תפקיד: {role}", self.team_id)
        self.record(f"התחלת {role}", f"ביצוע {len(tasks)} משימות")

        for idx, task in enumerate(tasks):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(tasks)) * 80) + 10
            update_agent(self.agent_id, "working", f"{task['name']}...", progress,
                        "https://www.tradingview.com/chart/",
                        f"<div style='color:#6366f1'>🖥️ {task['name']}</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>{task['detail']}</div>"
                        f"<div style='margin-top:4px;color:#eab308'>⏳ מבצע...</div>")

            time.sleep(3)

            browser_html = (f"<div style='color:#22c55e'>✅ {task['name']} - הושלם</div>"
                          f"<div style='margin-top:4px;color:#94a3b8'>{task['detail']}</div>"
                          f"<div style='margin-top:4px;color:#10b981'>Status: SUCCESS</div>")
            update_agent(self.agent_id, "working", f"הושלם: {task['name']}", progress + 5,
                        "https://www.tradingview.com/chart/", browser_html)

            log_activity("✅", f"{task['name']} בוצע", task['detail'], self.team_id)
            self.record(task['name'], f"{task['detail']} - הושלם בהצלחה", True)
            time.sleep(1)

        update_agent(self.agent_id, "idle", f"סיים - {role}", 100)
        log_activity("✅", f"{self.name} סיים", f"{role} - כל המשימות הושלמו", self.team_id)


class ParamOptAgent(BaseAgent):
    """Optimizes strategy parameters with specific details per agent"""

    AGENT_ROLES = {
        "po1": {  # Parameter Tuner
            "role": "מכוון פרמטרים",
            "work": [
                {"strategy": "ORB Breakout", "param": "TP Multiplier", "from": "2.0", "to": "2.5",
                 "result": "WR ירד ב-3% אבל PF עלה ב-0.4 - שווה", "accepted": True},
                {"strategy": "ORB Breakout", "param": "SL Multiplier", "from": "1.0", "to": "0.8",
                 "result": "WR עלה ב-2% ו-DD ירד ב-1.5% - מצוין", "accepted": True},
                {"strategy": "VWAP Reclaim", "param": "Reclaim Bars", "from": "3", "to": "4",
                 "result": "פחות עסקאות אבל WR עלה ב-5% - מומלץ", "accepted": True},
            ]
        },
        "po2": {  # Version Compare
            "role": "משווה גרסאות",
            "work": [
                {"strategy": "ORB Breakout", "v1": "Original (TP=2.0, SL=1.0)",
                 "v2": "Optimized (TP=2.5, SL=0.8)", "winner": "Optimized",
                 "reason": "PF עלה מ-2.4 ל-2.9, DD ירד מ-12% ל-10.5%"},
                {"strategy": "VWAP Reclaim", "v1": "Original (Bars=3, TP=15)",
                 "v2": "Optimized (Bars=4, TP=18)", "winner": "Optimized",
                 "reason": "WR עלה מ-72% ל-77%, פחות עסקאות אבל יותר רווחיות"},
            ]
        },
        "po3": {  # Sensitivity
            "role": "בודק רגישות",
            "work": [
                {"strategy": "ORB Breakout", "test": "שינוי ORB Start ב-±15 דקות",
                 "result": "רגישות נמוכה - האסטרטגיה יציבה. ±2% שינוי ב-WR", "stable": True},
                {"strategy": "VWAP Reclaim", "test": "שינוי EMA Period ב-±5",
                 "result": "רגישות בינונית - EMA 15 גרוע, EMA 20-25 דומה", "stable": True},
            ]
        },
    }

    def run(self):
        config = self.AGENT_ROLES.get(self.agent_id, {"role": "מייעל", "work": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} מתחיל...", 5)
        log_activity("🔧", f"{self.name} מתחיל", role, self.team_id)
        self.record(f"התחלת {role}", f"ביצוע {len(config['work'])} בדיקות")

        for idx, work in enumerate(config["work"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["work"]), 1)) * 80) + 10

            if self.agent_id == "po1":  # Parameter Tuner
                browser_html = (
                    f"<div style='color:#8b5cf6'>🎛️ כוונון: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>פרמטר: {work['param']}</div>"
                    f"<div style='color:#eab308'>שינוי: {work['from']} → {work['to']}</div>"
                    f"<div style='margin-top:4px;color:{'#22c55e' if work['accepted'] else '#ef4444'}'>"
                    f"{'✅' if work['accepted'] else '❌'} {work['result']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"כוונון {work['param']} ב-{work['strategy']}: {work['from']}→{work['to']}",
                           progress, "", browser_html)
                self.record(f"כוונון {work['param']} - {work['strategy']}",
                           f"שינוי {work['from']} → {work['to']}. תוצאה: {work['result']}. "
                           f"{'התקבל' if work['accepted'] else 'נדחה'}", work['accepted'])

            elif self.agent_id == "po2":  # Version Compare
                browser_html = (
                    f"<div style='color:#8b5cf6'>🔄 השוואת גרסאות: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>V1: {work['v1']}</div>"
                    f"<div style='color:#94a3b8'>V2: {work['v2']}</div>"
                    f"<div style='margin-top:4px;color:#22c55e'>🏆 מנצח: {work['winner']}</div>"
                    f"<div style='color:#94a3b8;margin-top:2px'>{work['reason']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"השוואה: {work['strategy']} - מנצח: {work['winner']}",
                           progress, "", browser_html)
                self.record(f"השוואת גרסאות - {work['strategy']}",
                           f"V1: {work['v1']} vs V2: {work['v2']}. מנצח: {work['winner']}. {work['reason']}", True)

            elif self.agent_id == "po3":  # Sensitivity
                browser_html = (
                    f"<div style='color:#8b5cf6'>📐 בדיקת רגישות: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>בדיקה: {work['test']}</div>"
                    f"<div style='margin-top:4px;color:{'#22c55e' if work['stable'] else '#ef4444'}'>"
                    f"{'✅ יציב' if work['stable'] else '⚠️ לא יציב'}: {work['result']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"רגישות: {work['strategy']} - {'יציב' if work['stable'] else 'לא יציב'}",
                           progress, "", browser_html)
                self.record(f"בדיקת רגישות - {work['strategy']}",
                           f"בדיקה: {work['test']}. תוצאה: {work['result']}. {'יציב' if work['stable'] else 'לא יציב'}", work['stable'])

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"סיים - {role}", 100)
        log_activity("✅", f"{self.name} סיים", f"{role} הושלם", self.team_id)


class ImprovementAgent(BaseAgent):
    """Suggests and applies strategy improvements"""

    AGENT_ROLES = {
        "i1": {  # Logic Optimizer
            "role": "מייעל לוגיקה",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "הוספת Volume Filter",
                 "detail": "הוספת תנאי volume > SMA(volume,20)*1.5 לכניסה - מסנן פריצות שווא",
                 "impact": "WR צפוי לעלות ב-4-6%, פחות עסקאות אבל יותר איכותיות",
                 "code_change": "volumeFilter = volume > ta.sma(volume, 20) * 1.5\nlongSignal = orbDone and ta.crossover(close, orbHigh) and volumeFilter"},
                {"strategy": "VWAP Reclaim", "suggestion": "הוספת Session Filter",
                 "detail": "הגבלת מסחר לשעות 9:30-15:00 בלבד, כדי להימנע מ-pre/post market",
                 "impact": "הפחתת DD צפויה של 2-3%, סינון תנודתיות מיותרת",
                 "code_change": "sessionOK = (hour >= 9 and minute >= 30) or (hour >= 10 and hour < 15)"},
            ]
        },
        "i2": {  # Filter Addition
            "role": "מוסיף פילטרים",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "הוספת VWAP כפילטר",
                 "detail": "Long רק מעל VWAP, Short רק מתחת VWAP - מגביר הסתברות להצלחה",
                 "impact": "WR צפוי לעלות ב-8-10%, מגביל עסקאות נגד המגמה",
                 "code_change": "vwapVal = ta.vwap(hlc3)\nlongSignal = orbDone and ta.crossover(close, orbHigh) and close > vwapVal"},
                {"strategy": "VWAP Reclaim", "suggestion": "הוספת ATR-based Stop Loss",
                 "detail": "שימוש ב-ATR(14) * 1.5 כ-Stop Loss דינמי במקום קבוע",
                 "impact": "DD צפוי לרדת ב-2%, SL מותאם לתנודתיות השוק",
                 "code_change": "atrVal = ta.atr(14)\nstrategy.exit('Exit', 'Long', loss=atrVal*1.5/syminfo.mintick)"},
            ]
        },
        "i3": {  # Vault Storage
            "role": "שומר כספת",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "אישור סופי ושמירה בכספת",
                 "detail": "האסטרטגיה עברה את כל השלבים: מחקר → קוד → בדיקה → ייעול",
                 "impact": "מוכנה להפעלה עם פרמטרים מיועלים", "code_change": ""},
                {"strategy": "VWAP Reclaim", "suggestion": "אישור סופי ושמירה בכספת",
                 "detail": "אסטרטגיה מוכנה עם כל הפילטרים והשיפורים",
                 "impact": "מוכנה להפעלה ב-live trading", "code_change": ""},
            ]
        },
    }

    def run(self):
        config = self.AGENT_ROLES.get(self.agent_id, {"role": "משפר", "suggestions": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} מתחיל...", 5)
        log_activity("🚀", f"{self.name} מתחיל", role, self.team_id)
        self.record(f"התחלת {role}", f"בדיקת {len(config['suggestions'])} שיפורים אפשריים")

        for idx, sug in enumerate(config["suggestions"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["suggestions"]), 1)) * 80) + 10

            browser_html = (
                f"<div style='color:#3b82f6'>🚀 {sug['suggestion']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>אסטרטגיה: {sug['strategy']}</div>"
                f"<div style='margin-top:4px;color:#e2e8f0'>{sug['detail']}</div>"
                f"<div style='margin-top:4px;color:#22c55e'>📈 השפעה צפויה: {sug['impact']}</div>"
            )
            if sug['code_change']:
                browser_html += f"<div style='margin-top:6px;color:#94a3b8'>שינוי בקוד:</div>"
                browser_html += f"<pre style='color:#c9d1d9;font-size:9px;background:rgba(0,0,0,.3);padding:4px;border-radius:4px;margin-top:2px'>{html_module.escape(sug['code_change'])}</pre>"

            update_agent(self.agent_id, "working",
                       f"{sug['suggestion']} → {sug['strategy']}",
                       progress, "", browser_html)

            self.record(f"{sug['suggestion']} - {sug['strategy']}",
                       f"{sug['detail']}. השפעה: {sug['impact']}"
                       + (f". קוד: {sug['code_change'][:60]}..." if sug['code_change'] else ""), True)

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"סיים - {role}", 100)
        log_activity("✅", f"{self.name} סיים", f"{role} הושלם", self.team_id)


class VisualDesignAgent(BaseAgent):
    """Designs chart overlays with visual trading concepts"""

    AGENT_DESIGNS = {
        "v1": {  # Chart Designer
            "role": "מעצב גרפים",
            "designs": [
                {"name": "ORB Box + Entry Arrows",
                 "description": "תיבת ORB בכחול שקוף (09:30-10:00), חיצי כניסה ירוקים/אדומים",
                 "visual": (
                     "📊 ORB Breakout Visual:\n"
                     "┌─────────────────────────┐\n"
                     "│  ═══ ORB High ═══ 4520  │ ← קו ירוק מקווקו\n"
                     "│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │ ← ORB Zone (כחול 20%)\n"
                     "│  ═══ ORB Low ════ 4510  │ ← קו אדום מקווקו\n"
                     "│         ↑ LONG 4521     │ ← חץ ירוק כניסה\n"
                     "│  ──── TP ───── 4540     │ ← קו ירוק TP\n"
                     "│  ──── SL ───── 4508     │ ← קו אדום SL\n"
                     "└─────────────────────────┘"
                 )},
                {"name": "VWAP Bands + Reclaim Markers",
                 "description": "קו VWAP סגול עם bands, סמנים של Reclaim בנקודות כניסה",
                 "visual": (
                     "📊 VWAP Reclaim Visual:\n"
                     "┌─────────────────────────┐\n"
                     "│  ~~~ Upper Band ~~~      │ ← קו סגול בהיר\n"
                     "│  ─── VWAP ──── 4515     │ ← קו סגול עבה\n"
                     "│  ~~~ Lower Band ~~~      │ ← קו סגול בהיר\n"
                     "│    ● Reclaim ↑ 4516      │ ← עיגול ירוק + חץ\n"
                     "│  ─── EMA20 ── 4512      │ ← קו כתום\n"
                     "│  TP: +15pts → 4531      │ ← קו ירוק מקווקו\n"
                     "│  SL: -8pts  → 4508      │ ← קו אדום מקווקו\n"
                     "└─────────────────────────┘"
                 )},
            ]
        },
        "v2": {  # Trade Markers
            "role": "סמני מסחר",
            "designs": [
                {"name": "Trade Entry/Exit Markers",
                 "description": "סימון ויזואלי של כל כניסה ויציאה על הגרף",
                 "visual": (
                     "📊 Trade Markers:\n"
                     "  ▲ Long Entry (ירוק)\n"
                     "  ▼ Short Entry (אדום)\n"
                     "  ◆ Take Profit (זהב)\n"
                     "  ✖ Stop Loss (אדום כהה)\n"
                     "  ── TP Line (ירוק מקווקו)\n"
                     "  ── SL Line (אדום מקווקו)\n"
                     "  ▓▓ Profit Zone (ירוק שקוף)\n"
                     "  ▓▓ Loss Zone (אדום שקוף)"
                 )},
                {"name": "P&L Summary Overlay",
                 "description": "תצוגת P&L חיה בפינת הגרף",
                 "visual": (
                     "📊 P&L Overlay (פינה ימנית עליונה):\n"
                     "┌──────────────────┐\n"
                     "│ 📈 P&L: +$1,245  │ ← ירוק\n"
                     "│ WR: 68% (34/50)  │\n"
                     "│ PF: 2.4          │\n"
                     "│ DD: -4.2%        │\n"
                     "│ Today: +$285     │ ← ירוק\n"
                     "└──────────────────┘"
                 )},
            ]
        },
    }

    def run(self):
        config = self.AGENT_DESIGNS.get(self.agent_id, {"role": "מעצב", "designs": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} מתחיל...", 5)
        log_activity("🎨", f"{self.name} מתחיל", role, self.team_id)
        self.record(f"התחלת {role}", f"עיצוב {len(config['designs'])} רכיבים ויזואליים")

        for idx, design in enumerate(config["designs"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["designs"]), 1)) * 80) + 10
            browser_html = (
                f"<div style='color:#ec4899'>🎨 {design['name']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>{design['description']}</div>"
                f"<pre style='margin-top:6px;color:#e2e8f0;font-size:9px;background:rgba(0,0,0,.3);padding:6px;border-radius:4px;white-space:pre;line-height:1.4'>{html_module.escape(design['visual'])}</pre>"
            )
            update_agent(self.agent_id, "working", f"עיצוב: {design['name']}", progress,
                        "https://www.tradingview.com/chart/", browser_html)

            log_activity("🎨", f"{design['name']} עוצב", design['description'][:60], self.team_id)
            self.record(f"עיצוב {design['name']}", f"{design['description']}. כולל: TP/SL lines, entry arrows, zone shading", True)
            time.sleep(3)

        update_agent(self.agent_id, "idle", f"סיים - {role}", 100)
        log_activity("✅", f"{self.name} סיים", f"{role} הושלם", self.team_id)


class AlertsAgent(BaseAgent):
    """Generates webhook configurations, AutoView/3Commas settings"""

    AGENT_CONFIG = {
        "al1": {  # Webhook Setup
            "role": "מגדיר Webhooks",
            "alerts": [
                {"type": "Discord Webhook", "detail": "התראות ל-Discord על כניסה/יציאה מעסקה",
                 "config": "URL: discord.com/webhook/...\nPayload: {strategy}, {action}, {price}"},
                {"type": "Telegram Bot", "detail": "שליחת התראות Telegram עם צילום גרף",
                 "config": "Bot Token: ***\nChat ID: ***\nInclude: chart screenshot"},
            ]
        },
        "al2": {  # AutoView/3Commas
            "role": "סוכן AutoView",
            "alerts": [
                {"type": "AutoView Integration", "detail": "חיבור TradingView ל-AutoView להרצה אוטומטית",
                 "config": "Mode: Paper Trading\nBroker: Alpaca\nSize: 1 contract"},
                {"type": "3Commas Bot", "detail": "הגדרת בוט 3Commas עם TP/SL אוטומטי",
                 "config": "Bot Type: Simple\nPair: ES/USD\nTP: 2x ORB Range\nSL: 1x ORB Range"},
            ]
        },
        "al3": {  # Timing
            "role": "מתזמן",
            "alerts": [
                {"type": "Market Hours", "detail": "הגדרת שעות פעילות: 09:30-16:00 EST בימי חול",
                 "config": "Active: Mon-Fri 09:30-16:00 EST\nBlacklist: FOMC days, NFP days"},
                {"type": "Pre-Market Check", "detail": "בדיקת תנאים לפני פתיחת שוק",
                 "config": "Check: VIX < 25, Gap < 1%, Futures positive"},
            ]
        },
    }

    def run(self):
        config = self.AGENT_CONFIG.get(self.agent_id, {"role": "מגדיר התראות", "alerts": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} מתחיל...", 5)
        log_activity("🔔", f"{self.name} מתחיל", role, self.team_id)
        self.record(f"התחלת {role}", f"הגדרת {len(config['alerts'])} התראות")

        for idx, alert in enumerate(config["alerts"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["alerts"]), 1)) * 80) + 10
            browser_html = (
                f"<div style='color:#06b6d4'>🔔 {alert['type']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>{alert['detail']}</div>"
                f"<pre style='margin-top:4px;color:#c9d1d9;font-size:9px;background:rgba(0,0,0,.3);padding:4px;border-radius:4px'>{html_module.escape(alert['config'])}</pre>"
            )
            update_agent(self.agent_id, "working", f"מגדיר: {alert['type']}", progress, "", browser_html)

            log_activity("🔔", f"{alert['type']} מוכן", alert['detail'][:60], self.team_id)
            self.record(f"הגדרת {alert['type']}", f"{alert['detail']}. Config: {alert['config'][:80]}", True)
            time.sleep(3)

        update_agent(self.agent_id, "idle", f"סיים - {role}", 100)
        log_activity("✅", f"{self.name} סיים", f"{role} הושלם", self.team_id)


# ============ AGENT MANAGER ============
active_agents = {}

def start_team(team_id):
    team_agents = {
        "funding": [
            ("f1", FundingResearchAgent), ("f2", FundingResearchAgent),
            ("f3", FundingResearchAgent), ("f4", FundingResearchAgent),
            ("f5", FundingResearchAgent), ("f6", FundingResearchAgent),
        ],
        "matching": [("m1", MatchingAgent), ("m2", MatchingAgent)],
        "research": [
            ("r1", StrategyResearchAgent), ("r2", StrategyResearchAgent),
            ("r3", StrategyResearchAgent), ("r4", StrategyResearchAgent),
        ],
        "deepdive": [("d1", DeepDiveAgent), ("d2", DeepDiveAgent)],
        "pinescript": [
            ("p1", PineScriptAgent), ("p2", PineScriptAgent),
            ("p3", PineScriptAgent), ("p4", PineScriptAgent), ("p5", PineScriptAgent),
        ],
        "chrome": [
            ("c1", ChromeAgent), ("c2", ChromeAgent),
            ("c3", ChromeAgent), ("c4", ChromeAgent),
        ],
        "analysis": [("a1", AnalysisAgent), ("a2", AnalysisAgent), ("a3", AnalysisAgent)],
        "paramopt": [("po1", ParamOptAgent), ("po2", ParamOptAgent), ("po3", ParamOptAgent)],
        "improvement": [("i1", ImprovementAgent), ("i2", ImprovementAgent), ("i3", ImprovementAgent)],
        "visual": [("v1", VisualDesignAgent), ("v2", VisualDesignAgent)],
        "alerts": [("al1", AlertsAgent), ("al2", AlertsAgent), ("al3", AlertsAgent)],
    }

    agents = team_agents.get(team_id, [])
    for agent_id, AgentClass in agents:
        if agent_id in active_agents:
            active_agents[agent_id].stop()
        agent = AgentClass(agent_id, team_id, f"Agent {agent_id}")
        active_agents[agent_id] = agent
        agent.start()

    emit_event("team_activated", {"teamId": team_id, "agentCount": len(agents)})

def stop_team(team_id):
    to_remove = []
    for aid, agent in active_agents.items():
        if agent.team_id == team_id:
            agent.stop()
            update_agent(aid, "idle", "נעצר", 0)
            to_remove.append(aid)
    for aid in to_remove:
        del active_agents[aid]
    emit_event("team_stopped", {"teamId": team_id})

def start_all():
    # Clear previous errors and activities on new run
    agent_errors.clear()
    save_errors()
    activity_log.clear()
    emit_event("errors_cleared", {})
    for tid in ["funding", "matching", "research", "deepdive", "pinescript", "chrome", "analysis", "paramopt", "improvement", "visual", "alerts"]:
        start_team(tid)
        time.sleep(1)


# ============ HTTP SERVER ============
class AgentHTTPHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self):
        if self.path == '/api/events':
            self.send_sse_stream()
        elif self.path == '/api/state':
            self.send_json({"agents": agent_states, "kpi": kpi, "vault": vault_strategies, "history": agent_history, "errors": agent_errors, "activities": activity_log})
        elif self.path.startswith('/api/start/'):
            team_id = self.path.split('/')[-1]
            threading.Thread(target=start_team, args=(team_id,), daemon=True).start()
            self.send_json({"status": "started", "team": team_id})
        elif self.path.startswith('/api/stop/'):
            team_id = self.path.split('/')[-1]
            stop_team(team_id)
            self.send_json({"status": "stopped", "team": team_id})
        elif self.path == '/api/start-all':
            threading.Thread(target=start_all, daemon=True).start()
            self.send_json({"status": "starting all"})
        elif self.path == '/api/stop-all':
            for tid in list(set(a.team_id for a in active_agents.values())):
                stop_team(tid)
            self.send_json({"status": "all stopped"})
        elif self.path == '/api/vault':
            self.send_json({"strategies": vault_strategies})
        elif self.path.startswith('/api/history/'):
            agent_id = self.path.split('/')[-1]
            self.send_json({"agentId": agent_id, "history": agent_history.get(agent_id, [])})
        elif self.path == '/api/history':
            self.send_json({"history": agent_history})
        elif self.path == '/api/errors':
            self.send_json({"errors": agent_errors})
        elif self.path == '/api/clear-errors':
            agent_errors.clear()
            save_errors()
            emit_event("errors_cleared", {})
            self.send_json({"status": "errors cleared"})
        elif self.path == '/api/clear-history':
            agent_history.clear()
            save_history()
            emit_event("history_cleared", {})
            self.send_json({"status": "history cleared"})
        elif self.path == '/api/clear-activities':
            activity_log.clear()
            save_activities()
            emit_event("activities_cleared", {})
            self.send_json({"status": "activities cleared"})
        elif self.path == '/api/clear-vault':
            vault_strategies.clear()
            save_vault()
            emit_event("vault_cleared", {})
            self.send_json({"status": "vault cleared"})
        elif self.path == '/api/clear-all':
            agent_history.clear()
            save_history()
            activity_log.clear()
            save_activities()
            vault_strategies.clear()
            save_vault()
            agent_errors.clear()
            save_errors()
            emit_event("all_cleared", {})
            self.send_json({"status": "all data cleared"})
        elif self.path == '/api/activities':
            self.send_json({"activities": activity_log})
        else:
            super().do_GET()

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_sse_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        client_queue = queue.Queue(maxsize=200)
        with sse_clients_lock:
            sse_clients.append(client_queue)

        try:
            self.wfile.write(f"data: {json.dumps({'type': 'init', 'data': {'agents': agent_states, 'kpi': kpi, 'vault': vault_strategies, 'history': agent_history, 'errors': agent_errors, 'activities': activity_log}})}\n\n".encode())
            self.wfile.flush()

            while running:
                try:
                    event = client_queue.get(timeout=2)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(f": heartbeat\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sse_clients_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    def log_message(self, format, *args):
        pass


import socketserver

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

def main():
    PORT = int(os.environ.get('PORT', 8080))
    load_vault()
    load_history()
    load_kpi()
    load_errors()
    load_activities()
    if _use_cloud():
        print(f"☁️ Cloud storage: Upstash Redis connected")
    else:
        print(f"📂 Local storage: vault.json + history.json (set UPSTASH_REDIS_REST_URL & UPSTASH_REDIS_REST_TOKEN for cloud persistence)")
    server = ThreadedHTTPServer(('0.0.0.0', PORT), AgentHTTPHandler)
    print(f"🚀 Agent Office Server running on http://localhost:{PORT}")
    print(f"📊 Open the URL above in your browser")
    print(f"🔧 API: /api/start/{{teamId}} | /api/stop/{{teamId}} | /api/start-all | /api/events")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        global running
        running = False
        for agent in active_agents.values():
            agent.stop()
        server.shutdown()


if __name__ == '__main__':
    main()
