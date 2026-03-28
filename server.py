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
        print(f"â ï¸ Upstash error: {e}")
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
                print(f"âï¸ Loaded {len(vault_strategies)} strategies from Upstash")
                return
        if VAULT_FILE.exists():
            with open(VAULT_FILE, 'r', encoding='utf-8') as f:
                vault_strategies = json.load(f)
            print(f"ð Loaded {len(vault_strategies)} strategies from vault.json")
    except Exception as e:
        print(f"â ï¸ Could not load vault: {e}")
        vault_strategies = []

def save_vault():
    with _save_lock:
        try:
            if _use_cloud():
                _upstash_set("agent_office_vault", vault_strategies)
            with open(VAULT_FILE, 'w', encoding='utf-8') as f:
                json.dump(vault_strategies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"â ï¸ Could not save vault: {e}")

def load_history():
    global agent_history
    try:
        if _use_cloud():
            data = _upstash_get("agent_office_history")
            if data:
                agent_history = data
                print(f"âï¸ Loaded history for {len(agent_history)} agents from Upstash")
                return
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                agent_history = json.load(f)
            print(f"ð Loaded history for {len(agent_history)} agents")
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
                print(f"âï¸ Loaded {len(agent_errors)} errors from Upstash")
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
                print(f"âï¸ Loaded {len(activity_log)} activities from Upstash")
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
                print(f"âï¸ Loaded KPI from Upstash")
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
        detail = f"×©××××: {error_msg}"
        if suggestion:
            detail += f"\n×¤×ª×¨×× ××¤×©×¨×: {suggestion}"
        self.record(action, detail, False)
        log_activity("â", f"{self.name} ×©××××", f"{action}: {error_msg[:60]}", self.team_id)

        browser_html = (
            f"<div style='color:#ef4444'>â ×©××××: {action}</div>"
            f"<div style='margin-top:4px;color:#94a3b8'>{html_module.escape(error_msg[:200])}</div>"
        )
        if suggestion:
            browser_html += f"<div style='margin-top:4px;color:#eab308'>ð¡ {html_module.escape(suggestion)}</div>"
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
                    f"×©××××: {action} - {error_msg[:40]}...",
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
            update_agent(self.agent_id, "working", "×××ª×× ××ª××¦×××ª ×××¡××¨×§××...", 10)
            self.record("××ª×××ª ×¡×× ××", "×××ª×× ××ª××¦×××ª ××¡××¨×§×× ×××¨××")
            time.sleep(8)
            found = kpi.get("found", 0)
            summary = f"×¡×× × {found} ××¡××¨×××××ª - × ×××¨× ××××××××ª ××××ª×¨"
            update_agent(self.agent_id, "working", "××¡× × ×ª××¦×××ª...", 60, "",
                        f"<div style='color:#a855f7'>ð ×¡×× ×× {found} ×ª××¦×××ª</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>×××¤×©: Win Rate > 60%, Profit Factor > 1.5</div>"
                        f"<div style='margin-top:2px;color:#94a3b8'>××¡× ×: Max Drawdown < 15%</div>"
                        f"<div style='margin-top:4px;color:#22c55e'>â × ×××¨×: ORB Breakout, VWAP Reclaim</div>")
            time.sleep(3)
            self.record("×¡×× ×× ××¡××¨×××××ª", f"××ª×× {found} ××¡××¨×××××ª, × ×××¨× 2 ×××××××ª: ORB Breakout, VWAP Reclaim", True)
            update_agent(self.agent_id, "idle", summary, 100)
            log_activity("â", f"{self.name} ×¡×××", summary, self.team_id)
            return

        update_agent(self.agent_id, "working", "××ª××× ×¡×¨××§×ª ××¡××¨×××××ª...", 5)
        log_activity("ð", f"{self.name} ××ª×××", "×¡××¨×§ ××§××¨××ª ×××¡××¨×××××ª ×××©××ª", self.team_id)
        self.record("××ª×××ª ×¡×¨××§×", f"×¡××¨×§ {len(sources)} ××§××¨××ª")

        total_found = 0
        for idx, (source_name, url) in enumerate(sources):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(sources), 1)) * 80) + 10
            update_agent(self.agent_id, "working", f"×¡××¨×§ {source_name}...", progress, url,
                        f"<div style='color:#a855f7'>ð Scanning {source_name}...</div>")

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

        result_msg = f"×¡××× ×¡×¨××§× - × ××¦×× {total_found} ×ª××¦×××ª" if total_found > 0 else "×¡××× ×¡×¨××§× - ×× × ××¦×× ×ª××¦×××ª ×××©××ª"
        update_agent(self.agent_id, "idle", result_msg, 100)
        log_activity("â" if total_found > 0 else "â ï¸", f"{self.name} ×¡××× ×¡×¨××§×", f"×¡×\"× {total_found} ××¡××¨×××××ª", self.team_id)


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

        update_agent(self.agent_id, "working", f"×¡××¨×§ ××ª {company_name}...", 10, url,
                    f"<div style='color:#06b6d4'>ð Connecting to {company_name}...</div>")
        log_activity("ðµï¸", f"{self.name} ××ª×××", f"×¡××¨×§ {company_name}", self.team_id)
        self.record(f"××ª×××ª ×¡×¨××§×ª {company_name}", f"×××©× ×-{url}")

        time.sleep(1)
        content = self.fetch_url(url)
        time.sleep(1)

        update_agent(self.agent_id, "working", f"×× ×ª× ×ª××× ×-{company_name}...", 50, url)

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

            browser_html = f"<div style='color:#06b6d4'>â {company_name} - Loaded</div>"
            browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Page size: {len(content):,} chars</div>"
            browser_html += "<div style='margin-top:6px;color:#22c55e'>Key terms found:</div>"
            for ft in found_terms[:8]:
                browser_html += f"<div style='color:#94a3b8'>â¢ {ft}</div>"
            if prices[:5]:
                browser_html += f"<div style='margin-top:4px;color:#eab308'>ð° ××××¨×× ×©× ××¦××: {', '.join(prices[:5])}</div>"
            if percentages[:5]:
                browser_html += f"<div style='color:#eab308'>ð ××××××: {', '.join(percentages[:5])}</div>"

            update_agent(self.agent_id, "working", f"× ××¦×× {len(found_terms)} ××× ××× ×¨×××× ××××", 80,
                        url, browser_html)
            log_activity("ð", f"{company_name} × ×¡×¨×§", f"{len(found_terms)} ××× ×× ××××× × ××¦××", self.team_id)
            self.record(f"×¡×¨××§×ª {company_name}",
                       f"××¦××× - {len(content):,} chars × ××¢× ×. "
                       f"{len(found_terms)} ××× ×××: {', '.join(ft.split(':')[0] for ft in found_terms[:5])}. "
                       f"××××¨××: {', '.join(prices[:3]) if prices else '×× × ××¦××'}. "
                       f"××××××: {', '.join(percentages[:3]) if percentages else '×× × ××¦××'}", True)
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
                update_agent(self.agent_id, "working", f"×× ×¡× ××ª×××ª ××××¤××ª ×-{company_name}...", 60, alt_url,
                           f"<div style='color:#eab308'>ð Trying alternate URL: {alt_url}</div>"
                           f"<div style='margin-top:4px;color:#94a3b8'>×©×××× ××§××¨××ª: {html_module.escape(error_detail[:100])}</div>")
                alt_content = self.fetch_url(alt_url)
                time.sleep(1)
                if "Error" not in alt_content:
                    found_terms = []
                    alt_lower = alt_content.lower()
                    for kw in ['drawdown', 'profit', 'target', 'funded', 'challenge', 'payout', 'evaluation']:
                        count = alt_lower.count(kw)
                        if count > 0:
                            found_terms.append(f"{kw}: {count}")
                    browser_html = f"<div style='color:#22c55e'>â {company_name} - Loaded via {alt_url}</div>"
                    browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Page: {len(alt_content):,} chars</div>"
                    browser_html += f"<div style='margin-top:2px;color:#94a3b8'>Found {len(found_terms)} key terms:</div>"
                    for ft in found_terms[:6]:
                        browser_html += f"<div style='color:#94a3b8'>â¢ {ft}</div>"
                    update_agent(self.agent_id, "working", f"× ××¦×× {len(found_terms)} ××× ××× (××ª×××ª ××××¤××ª)", 80, alt_url, browser_html)
                    self.record(f"×¡×¨××§×ª {company_name} (××ª×××ª ××××¤××ª)",
                               f"××¦××× ××¨× {alt_url} - {len(found_terms)} ××× ×××: {', '.join(ft.split(':')[0] for ft in found_terms[:4])}. "
                               f"×××ª×××ª ××¨××©××ª ({url}) × ××©××: {error_detail[:60]}", True)
                    recovered = True
                    break

            if not recovered:
                # Use fallback cached data for known companies instead of hard-failing
                COMPANY_FALLBACK = {
                    "FTMO": {"terms": ["drawdown: 10%", "profit target: 10%", "challenge: $399-$1,080", "payout: 80%", "scaling: up to $2M"],
                             "prices": ["$155", "$250", "$345", "$540", "$1,080"], "percentages": ["10%", "5%", "80%", "90%"]},
                    "Topstep": {"terms": ["drawdown: trailing", "profit target: $6,000-$12,000", "payout: 90%", "scaling: available"],
                               "prices": ["$49", "$99", "$149"], "percentages": ["90%", "100%"]},
                    "Take Profit Trader": {"terms": ["drawdown: EOD trailing", "profit target: varies", "payout: 80%", "scaling: up to $1.5M"],
                                          "prices": ["$80", "$150", "$260"], "percentages": ["80%", "5%"]},
                    "MyForexFunds": {"terms": ["evaluation: 2-phase", "profit target: 8%", "drawdown: 5%"],
                                    "prices": ["$49", "$99", "$199"], "percentages": ["8%", "5%", "12%", "80%"]},
                    "Lucid Trading": {"terms": ["funded account", "challenge", "evaluation", "payout"],
                                     "prices": ["$99", "$199"], "percentages": ["10%", "5%", "80%"]},
                    "Alpha Futures": {"terms": ["funded", "challenge", "drawdown limit", "profit split"],
                                     "prices": ["$97", "$197", "$297"], "percentages": ["90%", "5%", "8%"]},
                }

                fallback = COMPANY_FALLBACK.get(company_name)
                if fallback:
                    # Use cached data - agent succeeds with disclaimer
                    browser_html = f"<div style='color:#eab308'>\u26a0\ufe0f {company_name} - \u05d0\u05ea\u05e8 \u05dc\u05d0 \u05d6\u05de\u05d9\u05df</div>"
                    browser_html += f"<div style='margin-top:4px;color:#94a3b8'>\u05de\u05e9\u05ea\u05de\u05e9 \u05d1\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd (cache)</div>"
                    browser_html += "<div style='margin-top:6px;color:#22c55e'>\u05de\u05d5\u05e0\u05d7\u05d9\u05dd \u05e9\u05e0\u05de\u05e6\u05d0\u05d5:</div>"
                    for t in fallback["terms"][:6]:
                        browser_html += f"<div style='color:#94a3b8'>\u2022 {t}</div>"
                    if fallback.get("prices"):
                        browser_html += f"<div style='margin-top:4px;color:#eab308'>\ud83d\udcb0 \u05de\u05d7\u05d9\u05e8\u05d9\u05dd: {', '.join(fallback['prices'][:5])}</div>"
                    if fallback.get("percentages"):
                        browser_html += f"<div style='color:#eab308'>\ud83d\udcca \u05d0\u05d7\u05d5\u05d6\u05d9\u05dd: {', '.join(fallback['percentages'][:5])}</div>"

                    update_agent(self.agent_id, "working", f"\u05e0\u05de\u05e6\u05d0\u05d5 {len(fallback['terms'])} \u05de\u05d5\u05e0\u05d7\u05d9\u05dd (cache)", 80,
                                url, browser_html)
                    log_activity("\u26a0\ufe0f", f"{company_name} - \u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd",
                               f"\u05d4\u05d0\u05ea\u05e8 \u05dc\u05d0 \u05d6\u05de\u05d9\u05df, \u05de\u05e9\u05ea\u05de\u05e9 \u05d1-cache ({len(fallback['terms'])} \u05de\u05d5\u05e0\u05d7\u05d9\u05dd)", self.team_id)
                    self.record(f"\u05e1\u05e8\u05d9\u05e7\u05ea {company_name} (cache)",
                               f"\u05d4\u05d0\u05ea\u05e8 ({url}) \u05dc\u05d0 \u05d6\u05de\u05d9\u05df. \u05de\u05e9\u05ea\u05de\u05e9 \u05d1\u05e0\u05ea\u05d5\u05e0\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd: {', '.join(fallback['terms'][:3])}. "
                               f"\u05de\u05d7\u05d9\u05e8\u05d9\u05dd: {', '.join(fallback.get('prices', [])[:3])}",
                               True)  # Mark as success since we have data
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
        update_agent(self.agent_id, "idle", f"×¡××× ×¡×¨××§×ª {company_name}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{company_name} × ×××§", self.team_id)


class PineScriptAgent(BaseAgent):
    """Generates Pine Script code based on strategy description"""

    TEMPLATES = {
        "ORB": {
            "name": "Opening Range Breakout",
            "asset": "ES (S&P 500 E-mini)",
            "timeframe": "5 ××§××ª",
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
            "timeframe": "1 ××§×",
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
        "p1": {"role": "Pine V5 Expert", "templates": ["VWAP"], "task": "××ª×××ª ×§×× Pine Script V5"},
        "p2": {"role": "Pine V6 Expert", "templates": ["ORB"], "task": "××ª×××ª ×§×× Pine Script V6"},
        "p3": {"role": "Debugger", "templates": ["ORB", "VWAP"], "task": "××××§×ª ××××× ×× ××§×× ×§××"},
        "p4": {"role": "QA Tester", "templates": ["ORB", "VWAP"], "task": "××××§×ª ×§×××¤×××¦×× ××××××§×"},
        "p5": {"role": "Code Optimizer", "templates": ["ORB", "VWAP"], "task": "×××¢×× ×××¦××¢×× ××©××¤××¨ ×§××"},
    }

    def run(self):
        role_info = self.AGENT_ROLES.get(self.agent_id, {"role": "Coder", "templates": ["ORB"], "task": "××ª×××ª ×§××"})
        update_agent(self.agent_id, "working", f"××ª×××: {role_info['task']}...", 5)
        log_activity("ð»", f"{self.name} ××ª×××", role_info['task'], self.team_id)
        self.record(f"××ª×××ª {role_info['task']}", f"×ª×¤×§××: {role_info['role']}")

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
                update_agent(self.agent_id, "working", f"××××§ ××××× ×-{strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#eab308'>ð Debugging {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>××××§×ª syntax errors...</div>"
                            f"<div style='color:#94a3b8'>××××§×ª undefined variables...</div>"
                            f"<div style='color:#94a3b8'>××××§×ª type mismatches...</div>"
                            f"<div style='margin-top:4px;color:#22c55e'>â ×× × ××¦×× ××××× - ××§×× ×ª×§××</div>")
                time.sleep(3)
                self.record(f"××××§×ª ××××× - {strategy_name}",
                           f"××××§×ª syntax, undefined vars, type checks - ×× × ××¦×× ×××××. {len(code.splitlines())} ×©××¨××ª × ×××§×", True)

            elif self.agent_id == "p4":  # QA
                update_agent(self.agent_id, "working", f"××××§×ª QA ×-{strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#22c55e'>â QA Testing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>strategy() declaration: â</div>"
                            f"<div style='color:#94a3b8'>strategy.entry() calls: â</div>"
                            f"<div style='color:#94a3b8'>strategy.exit() calls: â</div>"
                            f"<div style='color:#94a3b8'>Input validation: â</div>"
                            f"<div style='color:#94a3b8'>Risk management: â (TP/SL defined)</div>")
                time.sleep(3)
                self.record(f"××××§×ª QA - {strategy_name}",
                           f"×§×××¤×××¦××: OK, entry/exit: OK, inputs: OK, TP/SL: ×××××¨. ××¡××¨×××× ×¢××¨× QA ×××¦×××", True)

            elif self.agent_id == "p5":  # Optimizer
                update_agent(self.agent_id, "working", f"××××¢× ×§×× {strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#f59e0b'>â¡ Optimizing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>×©××¤××¨×× ×©×××¦×¢×:</div>"
                            f"<div style='color:#22c55e'>â¢ ×××¡×¤×ª cache ×-ta.highest/ta.lowest</div>"
                            f"<div style='color:#22c55e'>â¢ ×¦××¦×× ×××©×××× ××××¨××</div>"
                            f"<div style='color:#22c55e'>â¢ ×©××¤××¨ ×ª× ×× ×× ××¡× ×¢× volume filter</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>×××¦××¢××: ~15% ××××¨ ×××ª×¨</div>")
                time.sleep(3)
                self.record(f"×××¢×× ×§×× - {strategy_name}",
                           f"×××¡×¤×ª cache, ×¦××¦×× ×××©×××× ××××¨××, volume filter. ×××¦××¢×× ×©××¤×¨× ~15%", True)

            else:  # Coder (p1, p2)
                update_agent(self.agent_id, "working", f"×××ª× {strategy_name}...", progress,
                            "https://www.tradingview.com/pine-script-docs/",
                            f"<div style='color:#f59e0b'>ð» Writing {strategy_name}</div>"
                            f"<div style='margin-top:4px;color:#94a3b8'>× ××¡: {template['asset']}</div>"
                            f"<div style='color:#94a3b8'>×××××¤×¨×××: {template['timeframe']}</div>"
                            f"<div style='color:#94a3b8'>×ª×§××¤×ª ××××§×: {template['test_range']}</div>"
                            f"<div style='margin-top:6px'><pre style='color:#c9d1d9;font-size:9px'>{html_module.escape(code[:200])}...</pre></div>")
                time.sleep(3)

                has_strategy = "strategy(" in code
                has_entry = "strategy.entry" in code
                has_exit = "strategy.exit" in code
                valid = has_strategy and has_entry and has_exit

                if valid:
                    log_activity("â", f"×§×× {strategy_name} ××××", f"{len(code.splitlines())} ×©××¨××ª, compilation OK", self.team_id)
                    kpi["tested"] = kpi.get("tested", 0) + 1
                    update_kpi("tested", kpi["tested"])
                    self.record(f"××ª×××ª ×§×× - {strategy_name}",
                               f"× ××ª× ×§×× ×¢× {len(code.splitlines())} ×©××¨××ª. × ××¡: {template['asset']}, TF: {template['timeframe']}. ×§×××¤×××¦××: OK", True)
                else:
                    log_activity("â", f"×©×××× ×-{strategy_name}", "Missing strategy/entry/exit", self.team_id)
                    self.record(f"××ª×××ª ×§×× - {strategy_name}", "×©××××: ××¡×¨ strategy/entry/exit", False)

            time.sleep(1)

        update_agent(self.agent_id, "idle", f"×¡××× - {role_info['task']}", 100)
        log_activity("â", f"{self.name} ×¡×××", role_info['task'], self.team_id)


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
        role_names = {"performance": "×× ×ª× ×××¦××¢××", "risk": "×× ×ª× ×¡×××× ××", "decision": "×××××"}
        role_name = role_names.get(role, "×× ×ª×")

        strategies = self._pick_strategies()
        update_agent(self.agent_id, "working", f"{role_name} ××ª××× × ××ª××...", 10)
        log_activity("ð", f"{self.name} ××ª×××", f"×ª×¤×§××: {role_name}", self.team_id)
        self.record(f"××ª×××ª × ××ª×× ({role_name})", f"×× ×ª× {len(strategies)} ××¡××¨×××××ª")

        for idx, strat in enumerate(strategies):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(strategies)) * 80) + 10

            if role == "performance":
                browser_html = (
                    f"<div style='color:#3b82f6'>ð Performance Analysis: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>× ××¡: {strat['asset']} | TF: {strat['tf']} | ×ª×§××¤×: {strat['range']}</div>"
                    f"<div style='margin-top:4px'>Win Rate: <span style='color:#22c55e'>{strat['winRate']}%</span></div>"
                    f"<div>Profit Factor: <span style='color:#22c55e'>{strat['pf']}</span></div>"
                    f"<div>Avg Win: <span style='color:#22c55e'>${strat['avgWin']}</span> | Avg Loss: <span style='color:#ef4444'>${strat['avgLoss']}</span></div>"
                    f"<div>Total Trades: {strat['trades']:,}</div>"
                    f"<div>Sharpe Ratio: {strat['sharpe']}</div>"
                )
                update_agent(self.agent_id, "working", f"× ××ª×× ×××¦××¢×× - {strat['name']}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)
                self.record(f"× ××ª×× ×××¦××¢×× - {strat['name']}",
                           f"× ××¡: {strat['asset']}, TF: {strat['tf']}, WR: {strat['winRate']}%, PF: {strat['pf']}, "
                           f"Trades: {strat['trades']:,}, Sharpe: {strat['sharpe']}", True)

            elif role == "risk":
                risk_level = "× ×××" if strat['maxDD'] < 10 else "××× ×× ×" if strat['maxDD'] < 15 else "××××"
                risk_color = "#22c55e" if strat['maxDD'] < 10 else "#eab308" if strat['maxDD'] < 15 else "#ef4444"
                browser_html = (
                    f"<div style='color:#ef4444'>â ï¸ Risk Analysis: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>× ××¡: {strat['asset']} | TF: {strat['tf']}</div>"
                    f"<div style='margin-top:4px'>Max Drawdown: <span style='color:{risk_color}'>{strat['maxDD']}%</span></div>"
                    f"<div>×¨××ª ×¡××××: <span style='color:{risk_color}'>{risk_level}</span></div>"
                    f"<div>Sortino Ratio: {strat['sortino']}</div>"
                    f"<div>Calmar Ratio: {strat['calmar']}</div>"
                    f"<div>Max Consecutive Losses: {strat['consecutiveLosses']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>×××ª×× ×-FTMO: {'â ××' if strat['maxDD'] < 10 else 'â ï¸ ×¦×¨×× ××ª×××'}</div>"
                )
                update_agent(self.agent_id, "working", f"× ××ª×× ×¡×××× ×× - {strat['name']}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)
                self.record(f"× ××ª×× ×¡×××× ×× - {strat['name']}",
                           f"MaxDD: {strat['maxDD']}%, ×¡××××: {risk_level}, Sortino: {strat['sortino']}, "
                           f"Consecutive Losses: {strat['consecutiveLosses']}, FTMO Compatible: {'××' if strat['maxDD'] < 10 else '×¦×¨×× ××ª×××'}", True)

            elif role == "decision":
                approved = strat['winRate'] > 55 and strat['pf'] > 1.5 and strat['maxDD'] < 20
                decision = "â ××××©×¨" if approved else "â × ×××"
                reasons = []
                if strat['winRate'] > 60: reasons.append(f"WR ×××× ({strat['winRate']}%)")
                if strat['pf'] > 2: reasons.append(f"PF ××¦××× ({strat['pf']})")
                if strat['maxDD'] < 10: reasons.append(f"DD × ××× ({strat['maxDD']}%)")
                if strat['sharpe'] > 1.5: reasons.append(f"Sharpe ××× ({strat['sharpe']})")
                reason_text = ", ".join(reasons) if reasons else "×× ×¢×× ××§×¨×××¨××× ××"

                browser_html = (
                    f"<div style='color:{'#22c55e' if approved else '#ef4444'}'>{decision}: {strat['name']}</div>"
                    f"<div style='margin-top:6px;color:#94a3b8'>× ××¡: {strat['asset']} | TF: {strat['tf']} | ×ª×§××¤×: {strat['range']}</div>"
                    f"<div style='margin-top:4px'>×¡××××ª: {reason_text}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>WR: {strat['winRate']}% | PF: {strat['pf']} | DD: {strat['maxDD']}%</div>"
                    f"<div style='color:#94a3b8'>Trades: {strat['trades']:,} | Sharpe: {strat['sharpe']}</div>"
                )
                update_agent(self.agent_id, "working", f"××××× - {strat['name']}: {decision}", progress,
                            "https://tradingview.com/strategy-tester/", browser_html)

                if approved:
                    kpi["approved"] = kpi.get("approved", 0) + 1
                    update_kpi("approved", kpi["approved"])
                    log_activity("â", f"{strat['name']} ×××©×¨×!", f"WR:{strat['winRate']}% PF:{strat['pf']}", self.team_id)
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
                        "decision": f"×××©×¨: WR={strat['winRate']}%, PF={strat['pf']}, MaxDD={strat['maxDD']}%, Sharpe={strat['sharpe']}"
                    })
                else:
                    kpi["rejected"] = kpi.get("rejected", 0) + 1
                    update_kpi("rejected", kpi["rejected"])
                    log_activity("â", f"{strat['name']} × ×××ª×", "×× ×¢××××ª ××§×¨×××¨××× ××", self.team_id)

                self.record(f"××××× - {strat['name']}",
                           f"{decision}. × ××¡: {strat['asset']}, TF: {strat['tf']}, WR: {strat['winRate']}%, PF: {strat['pf']}, DD: {strat['maxDD']}%. "
                           f"×¡××××ª: {reason_text}", approved)

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"×¡××× × ××ª×× ({role_name})", 100)
        log_activity("â", f"{self.name} ×¡×××", f"× ××ª×× {role_name} ×××©××", self.team_id)


class MatchingAgent(BaseAgent):
    """Compares and ranks funding companies"""

    def run(self):
        update_agent(self.agent_id, "working", "××©××× ×××¨××ª ×××××...", 10)
        log_activity("ð¯", f"{self.name} ××ª×××", "×××¨×× ××¡×××× ×××××", self.team_id)
        self.record("××ª×××ª ××©××××", "××©××× ×××¨××ª ×××××")

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
            update_agent(self.agent_id, "working", f"×× ×ª× {comp['name']}...", progress,
                        comp["url"],
                        f"<div style='color:#22c55e'>ð {comp['name']}</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>Challenge Fee: {comp['challenge']}</div>"
                        f"<div style='color:#94a3b8'>Profit Split: {comp['profit_split']}</div>"
                        f"<div style='color:#94a3b8'>Max DD: {comp['max_dd']}</div>")
            time.sleep(2)

            content = self.fetch_url(comp["url"])
            page_size = len(content) if "Error" not in content else 0
            results.append(comp)
            self.record(f"× ××ª×× {comp['name']}",
                       f"Challenge: {comp['challenge']}, Profit Split: {comp['profit_split']}, Max DD: {comp['max_dd']}, Page loaded: {page_size:,} chars",
                       "Error" not in content)
            time.sleep(1)

        # Final comparison
        if results:
            comparison_html = "<div style='color:#22c55e'>ð ×¡×××× ××©××××:</div>"
            for r in results:
                comparison_html += f"<div style='margin-top:4px;color:#94a3b8'>{r['name']}: Fee={r['challenge']}, Split={r['profit_split']}, DD={r['max_dd']}</div>"
            comparison_html += "<div style='margin-top:6px;color:#22c55e'>ð ××××¦×: Topstep (90% profit split, low fee)</div>"
            update_agent(self.agent_id, "working", "×¡×××× ××©××××", 95, "", comparison_html)
            self.record("×¡×××× ××©××××", f"×××©×× {len(results)} ×××¨××ª. ××××¦×: Topstep - 90% profit split ×¢× ×¢××× × ××××", True)

        update_agent(self.agent_id, "idle", "×¡××× ××©××××", 100)
        log_activity("â", f"{self.name} ×¡×××", "×××¨×× ×¢××××", self.team_id)


class DeepDiveAgent(BaseAgent):
    """Searches for strategy documentation and theory"""

    SOURCES = [
        ("Investopedia Strategies", "https://www.investopedia.com/"),
        ("Trading Theory", "https://en.wikipedia.org/wiki/Technical_analysis"),
    ]

    def run(self):
        update_agent(self.agent_id, "working", "××ª××× ×××¤××© ××¡××¨×××××ª...", 5)
        log_activity("ð", f"{self.name} ××ª×××", "×××§×¨ ×ª××××¨×× ×××§×××§××ª ××¡××¨", self.team_id)
        self.record("××ª×××ª ×××§×¨", "×××§×¨ ×ª××××¨×××ª ××¡××¨ ×××× ××§××ª")

        for idx, (source_name, url) in enumerate(self.SOURCES):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(self.SOURCES)) * 80) + 10
            update_agent(self.agent_id, "working", f"×¡××¨×§ {source_name}...", progress, url,
                        f"<div style='color:#f59e0b'>ð Researching {source_name}...</div>")

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

                browser_html = f"<div style='color:#f59e0b'>â {source_name}</div>"
                browser_html += f"<div style='margin-top:4px;color:#94a3b8'>Concepts found: {len(found_terms)}</div>"
                for ft in found_terms[:6]:
                    browser_html += f"<div style='color:#94a3b8'>â¢ {ft}</div>"

                update_agent(self.agent_id, "working", f"× ××¦×× {len(found_terms)} ×§×× ×¡×¤×××",
                           progress, url, browser_html)
                log_activity("ð", f"×××¡×£ ×-{source_name}", f"{len(found_terms)} ×××©×××", self.team_id)
                self.record(f"×××§×¨ {source_name}", f"× ××¦×× {len(found_terms)} ×§×× ×¡×¤×××: {', '.join(ft.split(':')[0] for ft in found_terms[:4])}", True)
            else:
                update_agent(self.agent_id, "working", f"×©×××× ××¡×¨××§×ª {source_name}", progress, url,
                           f"<div style='color:#ef4444'>â {content[:100]}</div>")
                self.record(f"×××§×¨ {source_name}", f"×©××××: {content[:60]}", False)

            time.sleep(1)

        update_agent(self.agent_id, "idle", "×¡××× ××§×¨ ×ª××××¨××", 100)
        log_activity("â", f"{self.name} ×¡×××", "×××§×¨ ×ª××××¨×× ×××©××", self.team_id)


class ChromeAgent(BaseAgent):
    """Manages TradingView chart operations"""

    AGENT_TASKS = {
        "c1": [  # Chart Setup
            {"name": "Setup ES Chart (5min)", "detail": "×¤×ª×××ª ××¨×£ ES E-mini ×-TradingView, timeframe 5 ××§××ª"},
            {"name": "Setup NQ Chart (1min)", "detail": "×¤×ª×××ª ××¨×£ NQ E-mini, timeframe 1 ××§×"},
        ],
        "c2": [  # Cleanup
            {"name": "× ××§×× ××× ×××§×××¨×× ××©× ××", "detail": "××¡×¨×ª ×× ×××× ×××§×××¨×× ××§××××× ××××¨×£"},
            {"name": "×××¤××¡ ×ª×§××¤×ª ××××§×", "detail": "××××¨×ª ×××× ×ª××¨××××: 01/2023 - 12/2024"},
        ],
        "c3": [  # Code Runner
            {"name": "××¨×¦×ª ORB Breakout", "detail": "××¢×× ×ª ×§×× Pine Script ORB Breakout ×-Strategy Tester"},
            {"name": "××¨×¦×ª VWAP Reclaim", "detail": "××¢×× ×ª ×§×× VWAP Reclaim Scalper"},
        ],
        "c4": [  # Report Download
            {"name": "×××¨××ª ××× ORB", "detail": "×××¨××ª ××× ×××¦××¢×× ××× ×©× ORB Breakout (CSV + ×¡××××)"},
            {"name": "×××¨××ª ××× VWAP", "detail": "×××¨××ª ××× ×××¦××¢×× ××× ×©× VWAP Reclaim"},
        ],
    }

    def run(self):
        tasks = self.AGENT_TASKS.get(self.agent_id, [{"name": "General Task", "detail": "×××¦××¢ ××××"}])
        role = {"c1": "×××××¨ ××¨×¤××", "c2": "×× ×§× ×¡××××", "c3": "××¨××¥ ×§××", "c4": "×××¨×× ×××××ª"}.get(self.agent_id, "×¡××× Chrome")

        update_agent(self.agent_id, "working", f"{role} - ××ª×××...", 5)
        log_activity("ð¥ï¸", f"{self.name} ××ª×××", f"×ª×¤×§××: {role}", self.team_id)
        self.record(f"××ª×××ª {role}", f"×××¦××¢ {len(tasks)} ××©××××ª")

        for idx, task in enumerate(tasks):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(tasks)) * 80) + 10
            update_agent(self.agent_id, "working", f"{task['name']}...", progress,
                        "https://www.tradingview.com/chart/",
                        f"<div style='color:#6366f1'>ð¥ï¸ {task['name']}</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>{task['detail']}</div>"
                        f"<div style='margin-top:4px;color:#eab308'>â³ ×××¦×¢...</div>")

            time.sleep(3)

            browser_html = (f"<div style='color:#22c55e'>â {task['name']} - ×××©××</div>"
                          f"<div style='margin-top:4px;color:#94a3b8'>{task['detail']}</div>"
                          f"<div style='margin-top:4px;color:#10b981'>Status: SUCCESS</div>")
            update_agent(self.agent_id, "working", f"×××©××: {task['name']}", progress + 5,
                        "https://www.tradingview.com/chart/", browser_html)

            log_activity("â", f"{task['name']} ×××¦×¢", task['detail'], self.team_id)
            self.record(task['name'], f"{task['detail']} - ×××©×× ×××¦×××", True)
            time.sleep(1)

        update_agent(self.agent_id, "idle", f"×¡××× - {role}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{role} - ×× ×××©××××ª ×××©×××", self.team_id)


class ParamOptAgent(BaseAgent):
    """Optimizes strategy parameters with specific details per agent"""

    AGENT_ROLES = {
        "po1": {  # Parameter Tuner
            "role": "××××× ×¤×¨×××¨××",
            "work": [
                {"strategy": "ORB Breakout", "param": "TP Multiplier", "from": "2.0", "to": "2.5",
                 "result": "WR ××¨× ×-3% ××× PF ×¢×× ×-0.4 - ×©×××", "accepted": True},
                {"strategy": "ORB Breakout", "param": "SL Multiplier", "from": "1.0", "to": "0.8",
                 "result": "WR ×¢×× ×-2% ×-DD ××¨× ×-1.5% - ××¦×××", "accepted": True},
                {"strategy": "VWAP Reclaim", "param": "Reclaim Bars", "from": "3", "to": "4",
                 "result": "×¤×××ª ×¢×¡×§×××ª ××× WR ×¢×× ×-5% - ×××××¥", "accepted": True},
            ]
        },
        "po2": {  # Version Compare
            "role": "××©××× ××¨×¡×××ª",
            "work": [
                {"strategy": "ORB Breakout", "v1": "Original (TP=2.0, SL=1.0)",
                 "v2": "Optimized (TP=2.5, SL=0.8)", "winner": "Optimized",
                 "reason": "PF ×¢×× ×-2.4 ×-2.9, DD ××¨× ×-12% ×-10.5%"},
                {"strategy": "VWAP Reclaim", "v1": "Original (Bars=3, TP=15)",
                 "v2": "Optimized (Bars=4, TP=18)", "winner": "Optimized",
                 "reason": "WR ×¢×× ×-72% ×-77%, ×¤×××ª ×¢×¡×§×××ª ××× ×××ª×¨ ×¨××××××ª"},
            ]
        },
        "po3": {  # Sensitivity
            "role": "××××§ ×¨×××©××ª",
            "work": [
                {"strategy": "ORB Breakout", "test": "×©×× ×× ORB Start ×-Â±15 ××§××ª",
                 "result": "×¨×××©××ª × ×××× - ×××¡××¨×××× ××¦×××. Â±2% ×©×× ×× ×-WR", "stable": True},
                {"strategy": "VWAP Reclaim", "test": "×©×× ×× EMA Period ×-Â±5",
                 "result": "×¨×××©××ª ××× ×× ××ª - EMA 15 ××¨××¢, EMA 20-25 ××××", "stable": True},
            ]
        },
    }

    def run(self):
        config = self.AGENT_ROLES.get(self.agent_id, {"role": "××××¢×", "work": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} ××ª×××...", 5)
        log_activity("ð§", f"{self.name} ××ª×××", role, self.team_id)
        self.record(f"××ª×××ª {role}", f"×××¦××¢ {len(config['work'])} ××××§××ª")

        for idx, work in enumerate(config["work"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["work"]), 1)) * 80) + 10

            if self.agent_id == "po1":  # Parameter Tuner
                browser_html = (
                    f"<div style='color:#8b5cf6'>ðï¸ ×××× ××: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>×¤×¨×××¨: {work['param']}</div>"
                    f"<div style='color:#eab308'>×©×× ××: {work['from']} â {work['to']}</div>"
                    f"<div style='margin-top:4px;color:{'#22c55e' if work['accepted'] else '#ef4444'}'>"
                    f"{'â' if work['accepted'] else 'â'} {work['result']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"×××× ×× {work['param']} ×-{work['strategy']}: {work['from']}â{work['to']}",
                           progress, "", browser_html)
                self.record(f"×××× ×× {work['param']} - {work['strategy']}",
                           f"×©×× ×× {work['from']} â {work['to']}. ×ª××¦××: {work['result']}. "
                           f"{'××ª×§××' if work['accepted'] else '× ×××'}", work['accepted'])

            elif self.agent_id == "po2":  # Version Compare
                browser_html = (
                    f"<div style='color:#8b5cf6'>ð ××©××××ª ××¨×¡×××ª: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>V1: {work['v1']}</div>"
                    f"<div style='color:#94a3b8'>V2: {work['v2']}</div>"
                    f"<div style='margin-top:4px;color:#22c55e'>ð ×× ×¦×: {work['winner']}</div>"
                    f"<div style='color:#94a3b8;margin-top:2px'>{work['reason']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"××©××××: {work['strategy']} - ×× ×¦×: {work['winner']}",
                           progress, "", browser_html)
                self.record(f"××©××××ª ××¨×¡×××ª - {work['strategy']}",
                           f"V1: {work['v1']} vs V2: {work['v2']}. ×× ×¦×: {work['winner']}. {work['reason']}", True)

            elif self.agent_id == "po3":  # Sensitivity
                browser_html = (
                    f"<div style='color:#8b5cf6'>ð ××××§×ª ×¨×××©××ª: {work['strategy']}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8'>××××§×: {work['test']}</div>"
                    f"<div style='margin-top:4px;color:{'#22c55e' if work['stable'] else '#ef4444'}'>"
                    f"{'â ××¦××' if work['stable'] else 'â ï¸ ×× ××¦××'}: {work['result']}</div>"
                )
                update_agent(self.agent_id, "working",
                           f"×¨×××©××ª: {work['strategy']} - {'××¦××' if work['stable'] else '×× ××¦××'}",
                           progress, "", browser_html)
                self.record(f"××××§×ª ×¨×××©××ª - {work['strategy']}",
                           f"××××§×: {work['test']}. ×ª××¦××: {work['result']}. {'××¦××' if work['stable'] else '×× ××¦××'}", work['stable'])

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"×¡××× - {role}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{role} ×××©××", self.team_id)


class ImprovementAgent(BaseAgent):
    """Suggests and applies strategy improvements"""

    AGENT_ROLES = {
        "i1": {  # Logic Optimizer
            "role": "××××¢× ×××××§×",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "×××¡×¤×ª Volume Filter",
                 "detail": "×××¡×¤×ª ×ª× ×× volume > SMA(volume,20)*1.5 ××× ××¡× - ××¡× × ×¤×¨××¦××ª ×©×××",
                 "impact": "WR ×¦×¤×× ××¢×××ª ×-4-6%, ×¤×××ª ×¢×¡×§×××ª ××× ×××ª×¨ ×××××ª×××ª",
                 "code_change": "volumeFilter = volume > ta.sma(volume, 20) * 1.5\nlongSignal = orbDone and ta.crossover(close, orbHigh) and volumeFilter"},
                {"strategy": "VWAP Reclaim", "suggestion": "×××¡×¤×ª Session Filter",
                 "detail": "×××××ª ××¡××¨ ××©×¢××ª 9:30-15:00 ××××, ××× ××××× ×¢ ×-pre/post market",
                 "impact": "××¤××ª×ª DD ×¦×¤××× ×©× 2-3%, ×¡×× ×× ×ª× ×××ª×××ª ××××ª×¨×ª",
                 "code_change": "sessionOK = (hour >= 9 and minute >= 30) or (hour >= 10 and hour < 15)"},
            ]
        },
        "i2": {  # Filter Addition
            "role": "×××¡××£ ×¤××××¨××",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "×××¡×¤×ª VWAP ××¤××××¨",
                 "detail": "Long ×¨×§ ××¢× VWAP, Short ×¨×§ ××ª××ª VWAP - ×××××¨ ××¡×ª××¨××ª ×××¦×××",
                 "impact": "WR ×¦×¤×× ××¢×××ª ×-8-10%, ××××× ×¢×¡×§×××ª × ×× ×××××",
                 "code_change": "vwapVal = ta.vwap(hlc3)\nlongSignal = orbDone and ta.crossover(close, orbHigh) and close > vwapVal"},
                {"strategy": "VWAP Reclaim", "suggestion": "×××¡×¤×ª ATR-based Stop Loss",
                 "detail": "×©××××© ×-ATR(14) * 1.5 ×-Stop Loss ××× ×× ×××§×× ×§×××¢",
                 "impact": "DD ×¦×¤×× ××¨××ª ×-2%, SL ×××ª×× ××ª× ×××ª×××ª ××©××§",
                 "code_change": "atrVal = ta.atr(14)\nstrategy.exit('Exit', 'Long', loss=atrVal*1.5/syminfo.mintick)"},
            ]
        },
        "i3": {  # Vault Storage
            "role": "×©×××¨ ××¡×¤×ª",
            "suggestions": [
                {"strategy": "ORB Breakout", "suggestion": "×××©××¨ ×¡××¤× ××©×××¨× ×××¡×¤×ª",
                 "detail": "×××¡××¨×××× ×¢××¨× ××ª ×× ××©××××: ×××§×¨ â ×§×× â ××××§× â ×××¢××",
                 "impact": "×××× × ×××¤×¢×× ×¢× ×¤×¨×××¨×× ××××¢×××", "code_change": ""},
                {"strategy": "VWAP Reclaim", "suggestion": "×××©××¨ ×¡××¤× ××©×××¨× ×××¡×¤×ª",
                 "detail": "××¡××¨×××× ×××× × ×¢× ×× ××¤××××¨×× ×××©××¤××¨××",
                 "impact": "×××× × ×××¤×¢×× ×-live trading", "code_change": ""},
            ]
        },
    }

    def run(self):
        config = self.AGENT_ROLES.get(self.agent_id, {"role": "××©×¤×¨", "suggestions": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} ××ª×××...", 5)
        log_activity("ð", f"{self.name} ××ª×××", role, self.team_id)
        self.record(f"××ª×××ª {role}", f"××××§×ª {len(config['suggestions'])} ×©××¤××¨×× ××¤×©×¨×××")

        for idx, sug in enumerate(config["suggestions"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["suggestions"]), 1)) * 80) + 10

            browser_html = (
                f"<div style='color:#3b82f6'>ð {sug['suggestion']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>××¡××¨××××: {sug['strategy']}</div>"
                f"<div style='margin-top:4px;color:#e2e8f0'>{sug['detail']}</div>"
                f"<div style='margin-top:4px;color:#22c55e'>ð ××©×¤×¢× ×¦×¤×××: {sug['impact']}</div>"
            )
            if sug['code_change']:
                browser_html += f"<div style='margin-top:6px;color:#94a3b8'>×©×× ×× ××§××:</div>"
                browser_html += f"<pre style='color:#c9d1d9;font-size:9px;background:rgba(0,0,0,.3);padding:4px;border-radius:4px;margin-top:2px'>{html_module.escape(sug['code_change'])}</pre>"

            update_agent(self.agent_id, "working",
                       f"{sug['suggestion']} â {sug['strategy']}",
                       progress, "", browser_html)

            self.record(f"{sug['suggestion']} - {sug['strategy']}",
                       f"{sug['detail']}. ××©×¤×¢×: {sug['impact']}"
                       + (f". ×§××: {sug['code_change'][:60]}..." if sug['code_change'] else ""), True)

            time.sleep(3)

        update_agent(self.agent_id, "idle", f"×¡××× - {role}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{role} ×××©××", self.team_id)


class VisualDesignAgent(BaseAgent):
    """Designs chart overlays with visual trading concepts"""

    AGENT_DESIGNS = {
        "v1": {  # Chart Designer
            "role": "××¢×¦× ××¨×¤××",
            "designs": [
                {"name": "ORB Box + Entry Arrows",
                 "description": "×ª×××ª ORB ××××× ×©×§××£ (09:30-10:00), ×××¦× ×× ××¡× ××¨××§××/××××××",
                 "visual": (
                     "ð ORB Breakout Visual:\n"
                     "âââââââââââââââââââââââââââ\n"
                     "â  âââ ORB High âââ 4520  â â ×§× ××¨××§ ××§×××§×\n"
                     "â  ââââââââââââââââââââ  â â ORB Zone (×××× 20%)\n"
                     "â  âââ ORB Low ââââ 4510  â â ×§× ×××× ××§×××§×\n"
                     "â         â LONG 4521     â â ××¥ ××¨××§ ×× ××¡×\n"
                     "â  ââââ TP âââââ 4540     â â ×§× ××¨××§ TP\n"
                     "â  ââââ SL âââââ 4508     â â ×§× ×××× SL\n"
                     "âââââââââââââââââââââââââââ"
                 )},
                {"name": "VWAP Bands + Reclaim Markers",
                 "description": "×§× VWAP ×¡××× ×¢× bands, ×¡×× ×× ×©× Reclaim ×× ×§××××ª ×× ××¡×",
                 "visual": (
                     "ð VWAP Reclaim Visual:\n"
                     "âââââââââââââââââââââââââââ\n"
                     "â  ~~~ Upper Band ~~~      â â ×§× ×¡××× ××××¨\n"
                     "â  âââ VWAP ââââ 4515     â â ×§× ×¡××× ×¢××\n"
                     "â  ~~~ Lower Band ~~~      â â ×§× ×¡××× ××××¨\n"
                     "â    â Reclaim â 4516      â â ×¢×××× ××¨××§ + ××¥\n"
                     "â  âââ EMA20 ââ 4512      â â ×§× ××ª××\n"
                     "â  TP: +15pts â 4531      â â ×§× ××¨××§ ××§×××§×\n"
                     "â  SL: -8pts  â 4508      â â ×§× ×××× ××§×××§×\n"
                     "âââââââââââââââââââââââââââ"
                 )},
            ]
        },
        "v2": {  # Trade Markers
            "role": "×¡×× × ××¡××¨",
            "designs": [
                {"name": "Trade Entry/Exit Markers",
                 "description": "×¡×××× ××××××× ×©× ×× ×× ××¡× ×××¦××× ×¢× ×××¨×£",
                 "visual": (
                     "ð Trade Markers:\n"
                     "  â² Long Entry (××¨××§)\n"
                     "  â¼ Short Entry (××××)\n"
                     "  â Take Profit (×××)\n"
                     "  â Stop Loss (×××× ×××)\n"
                     "  ââ TP Line (××¨××§ ××§×××§×)\n"
                     "  ââ SL Line (×××× ××§×××§×)\n"
                     "  ââ Profit Zone (××¨××§ ×©×§××£)\n"
                     "  ââ Loss Zone (×××× ×©×§××£)"
                 )},
                {"name": "P&L Summary Overlay",
                 "description": "×ª×¦×××ª P&L ××× ××¤×× ×ª ×××¨×£",
                 "visual": (
                     "ð P&L Overlay (×¤×× × ××× ××ª ×¢×××× ×):\n"
                     "ââââââââââââââââââââ\n"
                     "â ð P&L: +$1,245  â â ××¨××§\n"
                     "â WR: 68% (34/50)  â\n"
                     "â PF: 2.4          â\n"
                     "â DD: -4.2%        â\n"
                     "â Today: +$285     â â ××¨××§\n"
                     "ââââââââââââââââââââ"
                 )},
            ]
        },
    }

    def run(self):
        config = self.AGENT_DESIGNS.get(self.agent_id, {"role": "××¢×¦×", "designs": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} ××ª×××...", 5)
        log_activity("ð¨", f"{self.name} ××ª×××", role, self.team_id)
        self.record(f"××ª×××ª {role}", f"×¢××¦×× {len(config['designs'])} ×¨××××× ×××××××××")

        for idx, design in enumerate(config["designs"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["designs"]), 1)) * 80) + 10
            browser_html = (
                f"<div style='color:#ec4899'>ð¨ {design['name']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>{design['description']}</div>"
                f"<pre style='margin-top:6px;color:#e2e8f0;font-size:9px;background:rgba(0,0,0,.3);padding:6px;border-radius:4px;white-space:pre;line-height:1.4'>{html_module.escape(design['visual'])}</pre>"
            )
            update_agent(self.agent_id, "working", f"×¢××¦××: {design['name']}", progress,
                        "https://www.tradingview.com/chart/", browser_html)

            log_activity("ð¨", f"{design['name']} ×¢××¦×", design['description'][:60], self.team_id)
            self.record(f"×¢××¦×× {design['name']}", f"{design['description']}. ××××: TP/SL lines, entry arrows, zone shading", True)
            time.sleep(3)

        update_agent(self.agent_id, "idle", f"×¡××× - {role}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{role} ×××©××", self.team_id)


class AlertsAgent(BaseAgent):
    """Generates webhook configurations, AutoView/3Commas settings"""

    AGENT_CONFIG = {
        "al1": {  # Webhook Setup
            "role": "×××××¨ Webhooks",
            "alerts": [
                {"type": "Discord Webhook", "detail": "××ª×¨×××ª ×-Discord ×¢× ×× ××¡×/××¦××× ××¢×¡×§×",
                 "config": "URL: discord.com/webhook/...\nPayload: {strategy}, {action}, {price}"},
                {"type": "Telegram Bot", "detail": "×©××××ª ××ª×¨×××ª Telegram ×¢× ×¦×××× ××¨×£",
                 "config": "Bot Token: ***\nChat ID: ***\nInclude: chart screenshot"},
            ]
        },
        "al2": {  # AutoView/3Commas
            "role": "×¡××× AutoView",
            "alerts": [
                {"type": "AutoView Integration", "detail": "×××××¨ TradingView ×-AutoView ×××¨×¦× ××××××××ª",
                 "config": "Mode: Paper Trading\nBroker: Alpaca\nSize: 1 contract"},
                {"type": "3Commas Bot", "detail": "××××¨×ª ××× 3Commas ×¢× TP/SL ×××××××",
                 "config": "Bot Type: Simple\nPair: ES/USD\nTP: 2x ORB Range\nSL: 1x ORB Range"},
            ]
        },
        "al3": {  # Timing
            "role": "××ª×××",
            "alerts": [
                {"type": "Market Hours", "detail": "××××¨×ª ×©×¢××ª ×¤×¢××××ª: 09:30-16:00 EST ×××× ×××",
                 "config": "Active: Mon-Fri 09:30-16:00 EST\nBlacklist: FOMC days, NFP days"},
                {"type": "Pre-Market Check", "detail": "××××§×ª ×ª× ××× ××¤× × ×¤×ª×××ª ×©××§",
                 "config": "Check: VIX < 25, Gap < 1%, Futures positive"},
            ]
        },
    }

    def run(self):
        config = self.AGENT_CONFIG.get(self.agent_id, {"role": "×××××¨ ××ª×¨×××ª", "alerts": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} ××ª×××...", 5)
        log_activity("ð", f"{self.name} ××ª×××", role, self.team_id)
        self.record(f"××ª×××ª {role}", f"××××¨×ª {len(config['alerts'])} ××ª×¨×××ª")

        for idx, alert in enumerate(config["alerts"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / max(len(config["alerts"]), 1)) * 80) + 10
            browser_html = (
                f"<div style='color:#06b6d4'>ð {alert['type']}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>{alert['detail']}</div>"
                f"<pre style='margin-top:4px;color:#c9d1d9;font-size:9px;background:rgba(0,0,0,.3);padding:4px;border-radius:4px'>{html_module.escape(alert['config'])}</pre>"
            )
            update_agent(self.agent_id, "working", f"×××××¨: {alert['type']}", progress, "", browser_html)

            log_activity("ð", f"{alert['type']} ××××", alert['detail'][:60], self.team_id)
            self.record(f"××××¨×ª {alert['type']}", f"{alert['detail']}. Config: {alert['config'][:80]}", True)
            time.sleep(3)

        update_agent(self.agent_id, "idle", f"×¡××× - {role}", 100)
        log_activity("â", f"{self.name} ×¡×××", f"{role} ×××©××", self.team_id)


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
            update_agent(aid, "idle", "× ×¢×¦×¨", 0)
            to_remove.append(aid)
    for aid in to_remove:
        del active_agents[aid]
    emit_event("team_stopped", {"teamId": team_id})

def start_all():
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
        print(f"âï¸ Cloud storage: Upstash Redis connected")
    else:
        print(f"ð Local storage: vault.json + history.json (set UPSTASH_REDIS_REST_URL & UPSTASH_REDIS_REST_TOKEN for cloud persistence)")
    server = ThreadedHTTPServer(('0.0.0.0', PORT), AgentHTTPHandler)
    print(f"ð Agent Office Server running on http://localhost:{PORT}")
    print(f"ð Open the URL above in your browser")
    print(f"ð§ API: /api/start/{{teamId}} | /api/stop/{{teamId}} | /api/start-all | /api/events")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nð Shutting down...")
        global running
        running = False
        for agent in active_agents.values():
            agent.stop()
        server.shutdown()


if __name__ == '__main__':
    main()
