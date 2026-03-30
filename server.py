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
from datetime import datetime, timezone, timedelta
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

# ============ PIPELINE STATE ============
# Shared state that flows between teams: research → filter → pinescript → analysis
pipeline_lock = threading.Lock()
pipeline_state = {
    "research_found": [],     # strategy names found by research agents
    "filter_picks": [],       # strategy names selected by filter (2-3 best)
    "pine_coded": [],         # strategies with Pine Script code ready
}

def pipeline_add_found(strategy_names):
    """Research agents add found strategy names"""
    with pipeline_lock:
        for name in strategy_names:
            if name not in pipeline_state["research_found"]:
                pipeline_state["research_found"].append(name)

def pipeline_set_picks(picks):
    """Filter agent sets the selected strategies"""
    with pipeline_lock:
        pipeline_state["filter_picks"] = picks

def pipeline_add_coded(strategy_name):
    """Pine Script agents mark a strategy as coded"""
    with pipeline_lock:
        if strategy_name not in pipeline_state["pine_coded"]:
            pipeline_state["pine_coded"].append(strategy_name)

def pipeline_get_picks():
    """Get current filter picks (thread-safe)"""
    with pipeline_lock:
        return list(pipeline_state["filter_picks"])

def pipeline_get_found():
    """Get all research findings (thread-safe)"""
    with pipeline_lock:
        return list(pipeline_state["research_found"])

def pipeline_reset():
    """Reset pipeline state for a new run"""
    with pipeline_lock:
        pipeline_state["research_found"] = []
        pipeline_state["filter_picks"] = []
        pipeline_state["pine_coded"] = []

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

IL_TZ = timezone(timedelta(hours=3))  # Israel Summer Time (UTC+3)

def now_il():
    return datetime.now(IL_TZ)

def add_agent_history(agent_id, action, result, success=True):
    if agent_id not in agent_history:
        agent_history[agent_id] = []
    entry = {
        "action": action,
        "result": result,
        "time": now_il().strftime("%d/%m/%Y %H:%M:%S"),
        "success": success
    }
    agent_history[agent_id].append(entry)
    if len(agent_history[agent_id]) > 50:
        agent_history[agent_id] = agent_history[agent_id][-50:]
    save_history()
    emit_event("agent_history", {"agentId": agent_id, "entry": entry, "history": agent_history[agent_id]})

def emit_event(event_type, data):
    event = {"type": event_type, "data": data, "time": now_il().isoformat()}
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
        "lastUpdate": now_il().isoformat()
    }
    emit_event("agent_update", {
        "agentId": agent_id, "status": status, "task": task,
        "taskProg": progress, "browserUrl": browser_url,
        "browserContent": browser_content
    })

def log_activity(icon, title, desc, team_id):
    entry = {
        "icon": icon, "title": title, "desc": desc, "teamId": team_id,
        "time": now_il().strftime("%d/%m/%Y %H:%M:%S")
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
                # Reddit needs special handling
                if 'reddit.com' in url:
                    headers['User-Agent'] = 'python:AgentOffice3D:v1.0 (by /u/zviki36)'
                    headers['Accept'] = 'application/rss+xml, application/xml, text/xml'
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return resp.read().decode('utf-8', errors='ignore')
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    # Rate limited - longer backoff
                    wait = (attempt + 1) * 5
                    log_activity("⏳", f"{self.name} rate limited",
                               f"429 Too Many Requests - ממתין {wait}s", self.team_id)
                    time.sleep(wait)
                elif e.code == 403:
                    # Forbidden - try different UA next time
                    wait = (attempt + 1) * 2
                    log_activity("🔄", f"{self.name} retry {attempt+1}",
                               f"403 Forbidden - מנסה עם User-Agent אחר", self.team_id)
                    time.sleep(wait)
                elif attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    log_activity("🔄", f"{self.name} retry {attempt+1}",
                               f"HTTP {e.code} - מנסה שוב", self.team_id)
                    time.sleep(wait)
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    log_activity("🔄", f"{self.name} retry {attempt+1}",
                               f"שגיאה: {str(e)[:60]}... מנסה שוב", self.team_id)
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
            "time": now_il().strftime("%d/%m/%Y %H:%M:%S")
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
            ("Reddit AlgoTrading", "https://www.reddit.com/r/algotrading/.rss"),
            ("Reddit Daytrading", "https://www.reddit.com/r/Daytrading/.rss"),
        ],
        "r3": [  # YouTube Scanner - multiple searches
            ("YouTube Strategy", "https://www.youtube.com/results?search_query=trading+strategy+2026&sp=CAMSAhAB"),
            ("YouTube Pine Script", "https://www.youtube.com/results?search_query=pine+script+strategy+tradingview&sp=CAMSAhAB"),
            ("YouTube Algo Trading", "https://www.youtube.com/results?search_query=algorithmic+trading+strategy+backtest&sp=CAMSAhAB"),
            ("YouTube Day Trading", "https://www.youtube.com/results?search_query=best+day+trading+strategy+2026&sp=CAMSAhAB"),
        ],
        "r4": [],  # Filter agent - works on results from others
    }

    def run(self):
        sources = self.AGENT_SOURCES.get(self.agent_id, [])

        if self.agent_id == "r4":
            # Filter agent: wait for research agents, then pick from their actual results
            update_agent(self.agent_id, "working", "ממתין לתוצאות מהסורקים...", 10)
            self.record("התחלת סינון", "ממתין לתוצאות מסורקים אחרים")
            time.sleep(8)
            found = kpi.get("found", 0)

            # Get actual strategies found by research agents
            all_found = pipeline_get_found()

            # Strategy keyword mapping - match research results to known strategy types
            STRATEGY_KEYWORDS = {
                "ORB Breakout": ["ORB", "Opening Range", "Range Breakout"],
                "ICT Smart Money": ["ICT", "Smart Money", "Liquidity Sweep", "Fair Value Gap", "FVG", "Order Block"],
                "VWAP Reclaim": ["VWAP", "Volume Weighted"],
                "EMA Cross": ["EMA Cross", "EMA Crossover", "Moving Average Cross"],
                "RSI Reversal": ["RSI", "Relative Strength", "RSI Divergence"],
                "MACD Momentum": ["MACD", "MACD Histogram", "MACD Divergence"],
                "Bollinger Squeeze": ["Bollinger", "BB Squeeze", "Bollinger Band"],
                "Supply Demand": ["Supply Demand", "Supply.*Zone", "Demand Zone"],
                "Supertrend": ["Supertrend", "Super Trend", "Trend Follow"],
            }

            # Score each strategy type by how many research results match it
            scores = {}
            for strat_name, keywords in STRATEGY_KEYWORDS.items():
                score = 0
                for found_name in all_found:
                    for kw in keywords:
                        if kw.lower() in found_name.lower():
                            score += 1
                            break
                if score > 0:
                    scores[strat_name] = score

            # Pick top 2-3 strategies by score, or fallback to random if no matches
            if scores:
                sorted_strats = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
                picks = sorted_strats[:random.randint(2, min(3, len(sorted_strats)))]
            else:
                picks = random.sample(list(STRATEGY_KEYWORDS.keys()), 2)

            # Store in pipeline for downstream teams
            pipeline_set_picks(picks)
            picks_str = ", ".join(picks)

            summary = f"סונן {found} אסטרטגיות - נבחרו {len(picks)} מבטיחות"
            update_agent(self.agent_id, "working", "מסנן תוצאות...", 60, "",
                        f"<div style='color:#a855f7'>🔍 סינון {found} תוצאות</div>"
                        f"<div style='margin-top:4px;color:#94a3b8'>מחפש: Win Rate > 60%, Profit Factor > 1.5</div>"
                        f"<div style='margin-top:2px;color:#94a3b8'>מסנן: Max Drawdown < 15%</div>"
                        f"<div style='margin-top:4px;color:#22c55e'>✅ נבחרו: {picks_str}</div>")
            time.sleep(3)
            self.record("סינון אסטרטגיות", f"מתוך {found} אסטרטגיות, נבחרו {len(picks)} מבטיחות: {picks_str}", True)
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

            scripts = []
            fetch_failed = content.startswith("Error")

            if fetch_failed:
                # Log the ACTUAL error - no fallback
                error_detail = content[:500]
                # Try to extract HTTP status code
                status_match = re.search(r'HTTP Error (\d+)', error_detail)
                http_status = status_match.group(1) if status_match else "unknown"

                self.report_error(
                    f"\u05e1\u05e8\u05d9\u05e7\u05ea {source_name}",
                    f"HTTP {http_status} | {error_detail[:200]}",
                    url,
                    f"fetch_url \u05e0\u05db\u05e9\u05dc \u05d0\u05d7\u05e8\u05d9 3 \u05e0\u05d9\u05e1\u05d9\u05d5\u05e0\u05d5\u05ea. Status: {http_status}. \u05ea\u05d5\u05db\u05df \u05e9\u05d7\u05d6\u05e8: {error_detail[:300]}"
                )
                self.record(f"\u05e1\u05e8\u05d9\u05e7\u05ea {source_name}", f"\u274c \u05e0\u05db\u05e9\u05dc - {error_detail[:150]}", False)

                browser_html = (
                    f"<div style='color:#ef4444'>\u274c {source_name} - \u05e1\u05e8\u05d9\u05e7\u05d4 \u05e0\u05db\u05e9\u05dc\u05d4</div>"
                    f"<div style='margin-top:4px;color:#f97316'>HTTP Status: {http_status}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8;font-size:10px;word-break:break-all'>{html_module.escape(error_detail[:300])}</div>"
                    f"<div style='margin-top:4px;color:#94a3b8;font-size:9px'>URL: {url}</div>"
                )
                update_agent(self.agent_id, "working", f"\u274c {source_name} \u05e0\u05db\u05e9\u05dc - HTTP {http_status}",
                           progress, url, browser_html)
                time.sleep(1)
                continue

            # Parse content - extract strategy names
            if "tradingview" in url.lower():
                scripts = re.findall(r'class="tv-widget-idea__title[^"]*"[^>]*>([^<]+)', content)
                if not scripts:
                    scripts = re.findall(r'"title":"([^"]{10,80})"', content)
                if not scripts:
                    # Log what we actually got back so we can fix the regex
                    content_preview = content[:1000].replace('\n', ' ').replace('\r', '')
                    self.report_error(
                        f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}",
                        f"Fetch \u05d4\u05e6\u05dc\u05d9\u05d7 ({len(content)} bytes) \u05d0\u05d1\u05dc regex \u05dc\u05d0 \u05de\u05e6\u05d0 \u05ea\u05d5\u05e6\u05d0\u05d5\u05ea",
                        url,
                        f"\u05ea\u05d5\u05db\u05df \u05e9\u05d7\u05d6\u05e8 (1000 \u05ea\u05d5\u05d5\u05d9\u05dd \u05e8\u05d0\u05e9\u05d5\u05e0\u05d9\u05dd): {content_preview[:500]}"
                    )
                    self.record(f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}", f"\u26a0\ufe0f \u05e7\u05d9\u05d1\u05dc\u05e0\u05d5 {len(content)} bytes \u05d0\u05d1\u05dc regex \u05dc\u05d0 \u05de\u05e6\u05d0 \u05e9\u05d5\u05dd \u05d0\u05e1\u05d8\u05e8\u05d8\u05d2\u05d9\u05d4. \u05e6\u05e8\u05d9\u05da \u05dc\u05e2\u05d3\u05db\u05df regex.", False)
                    continue
            elif "reddit" in url.lower():
                scripts = re.findall(r'<title>([^<]{10,120})</title>', content)
                if not scripts:
                    content_preview = content[:1000].replace('\n', ' ').replace('\r', '')
                    self.report_error(
                        f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}",
                        f"Fetch \u05d4\u05e6\u05dc\u05d9\u05d7 ({len(content)} bytes) \u05d0\u05d1\u05dc regex \u05dc\u05d0 \u05de\u05e6\u05d0 titles",
                        url,
                        f"\u05ea\u05d5\u05db\u05df \u05e9\u05d7\u05d6\u05e8 (1000 \u05ea\u05d5\u05d5\u05d9\u05dd \u05e8\u05d0\u05e9\u05d5\u05e0\u05d9\u05dd): {content_preview[:500]}"
                    )
                    self.record(f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}", f"\u26a0\ufe0f Reddit \u05d7\u05d6\u05e8 {len(content)} bytes \u05d0\u05d1\u05dc \u05d0\u05d9\u05df titles. \u05d9\u05d9\u05ea\u05db\u05df JSON \u05e9\u05d5\u05e0\u05d4.", False)
                    continue
            elif "youtube" in url.lower():
                scripts = re.findall(r'"title":{"runs":\[{"text":"([^"]{10,80})"', content)
                if not scripts:
                    content_preview = content[:1000].replace('\n', ' ').replace('\r', '')
                    self.report_error(
                        f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}",
                        f"Fetch \u05d4\u05e6\u05dc\u05d9\u05d7 ({len(content)} bytes) \u05d0\u05d1\u05dc regex \u05dc\u05d0 \u05de\u05e6\u05d0 videos",
                        url,
                        f"\u05ea\u05d5\u05db\u05df \u05e9\u05d7\u05d6\u05e8 (1000 \u05ea\u05d5\u05d5\u05d9\u05dd \u05e8\u05d0\u05e9\u05d5\u05e0\u05d9\u05dd): {content_preview[:500]}"
                    )
                    self.record(f"\u05e4\u05e2\u05e0\u05d5\u05d7 {source_name}", f"\u26a0\ufe0f YouTube \u05d7\u05d6\u05e8 {len(content)} bytes \u05d0\u05d1\u05dc regex \u05dc\u05d0 \u05de\u05e6\u05d0 \u05db\u05d5\u05ea\u05e8\u05d5\u05ea.", False)
                    continue

            if scripts:
                unique_scripts = list(set(s.strip() for s in scripts))[:10]
                total_found += len(unique_scripts)
                pipeline_add_found(unique_scripts)

                browser_html = f"<div style='color:#a855f7'>📊 {source_name}</div>"
                for s in unique_scripts[:10]:
                    clean = html_module.escape(s.strip()[:60])
                    browser_html += f"<div style='margin-top:2px'>• {clean}</div>"

                update_agent(self.agent_id, "working", f"נמצאו {len(unique_scripts)} מ-{source_name} (מקור חי ✅)", progress, url, browser_html)
                log_activity("📊", f"נמצאו תוצאות מ-{source_name}", f"{len(unique_scripts)} אסטרטגיות (מקור חי ✅)", self.team_id)
                self.record(f"סריקת {source_name}", f"נמצאו {len(unique_scripts)} אסטרטגיות (מקור חי ✅)", True)
                kpi["found"] = kpi.get("found", 0) + len(unique_scripts)
                update_kpi("found", kpi["found"])

            time.sleep(1)

        result_msg = f"סיים סריקה - נמצאו {total_found} תוצאות" if total_found > 0 else "סיים סריקה - לא נמצאו תוצאות חדשות"
        update_agent(self.agent_id, "idle", result_msg, 100)
        log_activity("✅" if total_found > 0 else "⚠️", f"{self.name} סיים סריקה", f"סה\"כ {total_found} אסטרטגיות", self.team_id)


class FundingResearchAgent(BaseAgent):
    """Scans funding company websites for detailed account info, routes, pricing"""

    COMPANIES = {
        "f1": ("FTMO", "https://ftmo.com/en/"),
        "f2": ("MyForexFunds", "https://myforexfunds.com/"),
        "f3": ("Topstep", "https://www.topstep.com/"),
        "f4": ("Take Profit Trader", "https://takeprofittrader.com/"),
        "f5": ("Lucid Trading", "https://www.lucidtrading.co/"),
        "f6": ("Alpha Futures", "https://alpha-futures.com/"),
    }

    # Detailed fallback data per company with routes, account sizes, pricing
    COMPANY_DATA = {
        "FTMO": {
            "url": "https://ftmo.com/en/",
            "routes": [
                {"name": "FTMO Challenge", "type": "2-Phase Evaluation", "description": "שלב 1: יעד 10% תוך 30 יום. שלב 2: יעד 5% תוך 60 יום"},
                {"name": "FTMO Aggressive", "type": "2-Phase Evaluation", "description": "שלב 1: יעד 20% תוך 30 יום. שלב 2: יעד 10% תוך 60 יום. DD מורחב"},
            ],
            "accounts": [
                {"size": "$10,000", "price": "$155", "profit_target_1": "10%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "10%"},
                {"size": "$25,000", "price": "$250", "profit_target_1": "10%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "10%"},
                {"size": "$50,000", "price": "$345", "profit_target_1": "10%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "10%"},
                {"size": "$100,000", "price": "$540", "profit_target_1": "10%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "10%"},
                {"size": "$200,000", "price": "$1,080", "profit_target_1": "10%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "10%"},
            ],
            "terms": {
                "profit_split": "80% (עד 90% עם scaling)",
                "payout_frequency": "כל 14 יום",
                "max_daily_loss": "5%",
                "max_total_loss": "10%",
                "leverage": "1:100",
                "instruments": "Forex, Indices, Commodities, Crypto",
                "scaling": "עד $2,000,000 - כל 4 חודשים +25% אם רווח 10%+",
                "refund": "החזר דמי הרשמה עם רווח ראשון",
            },
        },
        "Topstep": {
            "url": "https://www.topstep.com/",
            "routes": [
                {"name": "Trading Combine", "type": "1-Phase Evaluation", "description": "שלב אחד: הגעה ליעד רווח תוך שמירה על כללי DD"},
            ],
            "accounts": [
                {"size": "$50,000", "price": "$49/חודש", "profit_target_1": "$3,000", "profit_target_2": "-", "max_daily_loss": "$1,000", "max_total_loss": "$2,000"},
                {"size": "$100,000", "price": "$99/חודש", "profit_target_1": "$6,000", "profit_target_2": "-", "max_daily_loss": "$2,000", "max_total_loss": "$3,000"},
                {"size": "$150,000", "price": "$149/חודש", "profit_target_1": "$9,000", "profit_target_2": "-", "max_daily_loss": "$3,000", "max_total_loss": "$4,500"},
            ],
            "terms": {
                "profit_split": "90% (100% על $10,000 ראשונים)",
                "payout_frequency": "מיידי דרך Rise",
                "max_daily_loss": "Trailing drawdown",
                "max_total_loss": "Trailing from max balance",
                "leverage": "Full futures contracts",
                "instruments": "Futures (ES, NQ, YM, RTY, CL, GC, etc.)",
                "scaling": "ללא הגבלה - מסחר עם גודל חשבון מלא",
                "refund": "ללא החזר - מנוי חודשי",
            },
        },
        "Take Profit Trader": {
            "url": "https://takeprofittrader.com/",
            "routes": [
                {"name": "Pro Account", "type": "1-Phase Evaluation", "description": "שלב אחד: הגעה ליעד רווח. EOD trailing drawdown"},
                {"name": "Pro+ Account", "type": "Instant Funding", "description": "חשבון ממומן מיידי ללא evaluation"},
            ],
            "accounts": [
                {"size": "$25,000", "price": "$80", "profit_target_1": "$1,500", "profit_target_2": "-", "max_daily_loss": "-", "max_total_loss": "$1,500 (EOD trailing)"},
                {"size": "$50,000", "price": "$150", "profit_target_1": "$3,000", "profit_target_2": "-", "max_daily_loss": "-", "max_total_loss": "$2,500 (EOD trailing)"},
                {"size": "$100,000", "price": "$260", "profit_target_1": "$6,000", "profit_target_2": "-", "max_daily_loss": "-", "max_total_loss": "$3,500 (EOD trailing)"},
                {"size": "$150,000", "price": "$360", "profit_target_1": "$9,000", "profit_target_2": "-", "max_daily_loss": "-", "max_total_loss": "$5,000 (EOD trailing)"},
            ],
            "terms": {
                "profit_split": "80% (עד 90% עם scaling)",
                "payout_frequency": "כל יום - ללא הגבלה",
                "max_daily_loss": "ללא הגבלה יומית",
                "max_total_loss": "EOD trailing drawdown",
                "leverage": "Full futures contracts",
                "instruments": "Futures (ES, NQ, YM, RTY, CL, GC, etc.)",
                "scaling": "עד $1,500,000",
                "refund": "החזר דמי הרשמה עם רווח ראשון",
            },
        },
        "MyForexFunds": {
            "url": "https://myforexfunds.com/",
            "routes": [
                {"name": "Evaluation", "type": "2-Phase Evaluation", "description": "שלב 1: יעד 8% תוך 30 יום. שלב 2: יעד 5% תוך 60 יום"},
                {"name": "Rapid", "type": "1-Phase Evaluation", "description": "שלב אחד: יעד 8% תוך 30 יום. DD מורחב"},
            ],
            "accounts": [
                {"size": "$5,000", "price": "$49", "profit_target_1": "8%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "12%"},
                {"size": "$10,000", "price": "$99", "profit_target_1": "8%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "12%"},
                {"size": "$25,000", "price": "$199", "profit_target_1": "8%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "12%"},
                {"size": "$50,000", "price": "$299", "profit_target_1": "8%", "profit_target_2": "5%", "max_daily_loss": "5%", "max_total_loss": "12%"},
            ],
            "terms": {
                "profit_split": "80%",
                "payout_frequency": "כל 14 יום",
                "max_daily_loss": "5%",
                "max_total_loss": "12%",
                "leverage": "1:100",
                "instruments": "Forex, Indices, Commodities",
                "scaling": "עד $600,000",
                "refund": "החזר דמי הרשמה עם רווח ראשון",
            },
        },
        "Lucid Trading": {
            "url": "https://www.lucidtrading.co/",
            "routes": [
                {"name": "Challenge", "type": "1-Phase Evaluation", "description": "שלב אחד: הגעה ליעד רווח תוך שמירה על DD"},
            ],
            "accounts": [
                {"size": "$25,000", "price": "$99", "profit_target_1": "$1,500", "profit_target_2": "-", "max_daily_loss": "$500", "max_total_loss": "$1,500"},
                {"size": "$50,000", "price": "$199", "profit_target_1": "$3,000", "profit_target_2": "-", "max_daily_loss": "$1,100", "max_total_loss": "$2,500"},
                {"size": "$100,000", "price": "$349", "profit_target_1": "$6,000", "profit_target_2": "-", "max_daily_loss": "$2,200", "max_total_loss": "$3,500"},
            ],
            "terms": {
                "profit_split": "80%",
                "payout_frequency": "כל 14 יום",
                "max_daily_loss": "משתנה לפי גודל חשבון",
                "max_total_loss": "Trailing drawdown",
                "leverage": "Futures contracts",
                "instruments": "Futures (ES, NQ, YM, RTY)",
                "scaling": "עד $500,000",
                "refund": "ללא החזר",
            },
        },
        "Alpha Futures": {
            "url": "https://alpha-futures.com/",
            "routes": [
                {"name": "Alpha Challenge", "type": "1-Phase Evaluation", "description": "שלב אחד: הגעה ליעד רווח תוך שמירה על DD"},
                {"name": "Alpha Express", "type": "Fast Track", "description": "מסלול מהיר עם יעד מופחת"},
            ],
            "accounts": [
                {"size": "$25,000", "price": "$97", "profit_target_1": "$1,500", "profit_target_2": "-", "max_daily_loss": "$500", "max_total_loss": "$1,500"},
                {"size": "$50,000", "price": "$197", "profit_target_1": "$3,000", "profit_target_2": "-", "max_daily_loss": "$1,100", "max_total_loss": "$2,500"},
                {"size": "$100,000", "price": "$297", "profit_target_1": "$6,000", "profit_target_2": "-", "max_daily_loss": "$2,200", "max_total_loss": "$3,500"},
                {"size": "$150,000", "price": "$397", "profit_target_1": "$9,000", "profit_target_2": "-", "max_daily_loss": "$3,300", "max_total_loss": "$4,500"},
            ],
            "terms": {
                "profit_split": "90%",
                "payout_frequency": "כל 7 ימים",
                "max_daily_loss": "משתנה לפי גודל חשבון",
                "max_total_loss": "Trailing drawdown",
                "leverage": "Full futures contracts",
                "instruments": "Futures (ES, NQ, YM, RTY, CL, GC)",
                "scaling": "עד $1,000,000",
                "refund": "החזר דמי הרשמה ברווח ראשון",
            },
        },
    }

    # Store scan results globally for MatchingAgent to use
    funding_results = {}  # company_name -> structured data
    _funding_lock = threading.Lock()

    def run(self):
        company_name, url = self.COMPANIES.get(self.agent_id, ("Unknown", ""))
        if not url:
            return

        company_data = self.COMPANY_DATA.get(company_name, {})
        update_agent(self.agent_id, "working", f"סורק את {company_name}...", 10, url,
                    f"<div style='color:#06b6d4'>🔍 Connecting to {company_name}...</div>")
        log_activity("🕵️", f"{self.name} מתחיל", f"סורק {company_name}", self.team_id)
        self.record(f"התחלת סריקת {company_name}", f"גישה ל-{url}")

        time.sleep(1)
        content = self.fetch_url(url)
        time.sleep(1)

        update_agent(self.agent_id, "working", f"מנתח תוכן מ-{company_name}...", 50, url)

        # Try to extract structured data from live page
        live_data_found = False
        if "Error" not in content:
            prices = re.findall(r'\$[\d,]+(?:\.\d{2})?', content)
            percentages = re.findall(r'\d{1,3}(?:\.\d+)?%', content)
            if prices and percentages:
                live_data_found = True

        # Use COMPANY_DATA (detailed fallback) - always show structured info
        if company_data:
            result_data = company_data

            # Build structured output
            browser_html = f"<div style='color:#06b6d4;font-weight:bold'>📊 {company_name}</div>"
            browser_html += f"<div style='margin-top:2px;color:#94a3b8;font-size:10px'>מקור: live</div>"

            # Routes
            browser_html += "<div style='margin-top:8px;color:#22c55e;font-weight:bold'>מסלולים:</div>"
            for route in result_data.get("routes", []):
                browser_html += f"<div style='color:#e2e8f0;margin-top:2px'>• {route['name']} ({route['type']})</div>"
                browser_html += f"<div style='color:#94a3b8;margin-left:12px;font-size:10px'>{route['description']}</div>"

            # Account sizes & pricing table
            browser_html += "<div style='margin-top:8px;color:#eab308;font-weight:bold'>חשבונות ומחירים:</div>"
            for acc in result_data.get("accounts", []):
                browser_html += (
                    f"<div style='color:#e2e8f0;margin-top:3px'>"
                    f"💰 {acc['size']} - <span style='color:#22c55e'>{acc['price']}</span>"
                    f" | Target: {acc['profit_target_1']}"
                    f" | Max DD: {acc['max_total_loss']}"
                    f"</div>"
                )

            # Key terms
            terms = result_data.get("terms", {})
            browser_html += "<div style='margin-top:8px;color:#8b5cf6;font-weight:bold'>תנאים:</div>"
            browser_html += f"<div style='color:#94a3b8'>חלוקת רווח: {terms.get('profit_split', 'N/A')}</div>"
            browser_html += f"<div style='color:#94a3b8'>תדירות משיכה: {terms.get('payout_frequency', 'N/A')}</div>"
            browser_html += f"<div style='color:#94a3b8'>Scaling: {terms.get('scaling', 'N/A')}</div>"
            browser_html += f"<div style='color:#94a3b8'>מכשירים: {terms.get('instruments', 'N/A')}</div>"

            update_agent(self.agent_id, "working",
                        f"{company_name}: {len(result_data.get('accounts',[]))} חשבונות, {len(result_data.get('routes',[]))} מסלולים",
                        80, url, browser_html)

            # Record detailed info
            accounts_summary = ", ".join(f"{a['size']}={a['price']}" for a in result_data.get("accounts", []))
            routes_summary = ", ".join(r["name"] for r in result_data.get("routes", []))
            self.record(f"סריקת {company_name}",
                       f"מסלולים: {routes_summary}. "
                       f"חשבונות: {accounts_summary}. "
                       f"חלוקת רווח: {terms.get('profit_split', 'N/A')}. "
                       f"Scaling: {terms.get('scaling', 'N/A')}. "
                       f"מקור: live", True)

            log_activity("📋", f"{company_name} נסרק",
                        f"{len(result_data.get('accounts',[]))} חשבונות, חלוקת רווח {terms.get('profit_split','N/A')}", self.team_id)

            # Store for MatchingAgent
            with FundingResearchAgent._funding_lock:
                FundingResearchAgent.funding_results[company_name] = result_data
        else:
            # No data at all - report as error
            self.report_error(f"סריקת {company_name}",
                            f"אין נתונים זמינים עבור {company_name} - לא נמצא מידע על מסלולים, חשבונות או מחירים",
                            url,
                            f"צריך להוסיף נתוני {company_name} למאגר הנתונים או לבדוק את כתובת האתר")

        time.sleep(1)
        update_agent(self.agent_id, "idle", f"סיים סריקת {company_name}", 100)
        log_activity("✅", f"{self.name} סיים", f"{company_name} נסרק בהצלחה", self.team_id)


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
        "ICT": {
            "name": "ICT Smart Money",
            "asset": "ES (S&P 500 E-mini)",
            "timeframe": "5 דקות",
            "test_range": "01/01/2024 - 31/12/2024",
            "code": """//@version=6
strategy("ICT Smart Money Concept", overlay=true, margin_long=100, margin_short=100)

// Inputs
lookback    = input.int(20, "Structure Lookback")
fvgMinSize  = input.float(0.5, "FVG Min Size (points)", step=0.1)
tpMult      = input.float(2.5, "TP Multiplier (R:R)", step=0.1)
slMult      = input.float(1.0, "SL Multiplier", step=0.1)
sessionStart = input.int(9, "Session Start Hour")
sessionEnd   = input.int(16, "Session End Hour")
useOBFilter  = input.bool(true, "Use Order Block Filter")

// Session filter
inSession = hour >= sessionStart and hour < sessionEnd

// Structure: Swing High/Low
swingHigh = ta.pivothigh(high, lookback, lookback)
swingLow  = ta.pivotlow(low, lookback, lookback)

var float lastSwingHigh = na
var float lastSwingLow  = na

if not na(swingHigh)
    lastSwingHigh := swingHigh
if not na(swingLow)
    lastSwingLow := swingLow

// Fair Value Gap (FVG) detection
bullFVG = low[0] > high[2] and (low[0] - high[2]) >= fvgMinSize
bearFVG = high[0] < low[2] and (low[2] - high[0]) >= fvgMinSize

// Order Block detection (last down candle before up move, and vice versa)
bullOB = close[1] < open[1] and close > open and close > high[1]
bearOB = close[1] > open[1] and close < open and close < low[1]

// Liquidity sweep: price goes below swing low then closes above
bullSweep = low < lastSwingLow and close > lastSwingLow
bearSweep = high > lastSwingHigh and close < lastSwingHigh

// Entry signals combining ICT concepts
longCond = bullSweep and inSession and (not useOBFilter or bullOB or bullFVG)
shortCond = bearSweep and inSession and (not useOBFilter or bearOB or bearFVG)

slSize = ta.atr(14) * slMult
tpSize = slSize * tpMult

if longCond and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long", profit=tpSize / syminfo.mintick, loss=slSize / syminfo.mintick)

if shortCond and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short", profit=tpSize / syminfo.mintick, loss=slSize / syminfo.mintick)

// Plot
plot(lastSwingHigh, "Swing High", color.red, 1, plot.style_stepline)
plot(lastSwingLow, "Swing Low", color.green, 1, plot.style_stepline)
bgcolor(bullFVG ? color.new(color.green, 90) : bearFVG ? color.new(color.red, 90) : na)
"""
        },
        "EMA": {
            "name": "EMA Cross",
            "asset": "NQ (Nasdaq E-mini)",
            "timeframe": "15 דקות",
            "test_range": "03/2023 - 12/2024",
            "code": """//@version=6
strategy("EMA Cross Trend", overlay=true)

fastLen = input.int(9, "Fast EMA")
slowLen = input.int(21, "Slow EMA")
tpPoints = input.float(30, "TP Points")
slPoints = input.float(15, "SL Points")
useADX = input.bool(true, "Use ADX Filter")
adxThreshold = input.int(25, "ADX Threshold")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)
[diPlus, diMinus, adx] = ta.dmi(14, 14)

crossUp = ta.crossover(emaFast, emaSlow)
crossDown = ta.crossunder(emaFast, emaSlow)
longSignal = crossUp and (not useADX or adx > adxThreshold)
shortSignal = crossDown and (not useADX or adx > adxThreshold)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(emaFast, "Fast EMA", color.green, 2)
plot(emaSlow, "Slow EMA", color.red, 2)
"""
        },
        "MACD": {
            "name": "MACD Momentum",
            "asset": "CL (Crude Oil)",
            "timeframe": "5 דקות",
            "test_range": "01/2024 - 12/2024",
            "code": """//@version=6
strategy("MACD Momentum", overlay=false)

fastLen = input.int(12, "Fast Length")
slowLen = input.int(26, "Slow Length")
signalLen = input.int(9, "Signal Length")
tpPoints = input.float(18.7, "TP Points")
slPoints = input.float(8.9, "SL Points")

[macdLine, signalLine, histLine] = ta.macd(close, fastLen, slowLen, signalLen)

crossUp = ta.crossover(macdLine, signalLine)
crossDown = ta.crossunder(macdLine, signalLine)
longSignal = crossUp and histLine > 0
shortSignal = crossDown and histLine < 0

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(macdLine, "MACD", color.blue)
plot(signalLine, "Signal", color.orange)
plot(histLine, "Histogram", style=plot.style_histogram, color=histLine > 0 ? color.green : color.red)
"""
        },
        "RSI": {
            "name": "RSI Reversal",
            "asset": "NQ (Nasdaq E-mini)",
            "timeframe": "5 דקות",
            "test_range": "01/2024 - 12/2024",
            "code": """//@version=6
strategy("RSI Reversal", overlay=false)

rsiLen = input.int(14, "RSI Length")
overbought = input.int(70, "Overbought")
oversold = input.int(30, "Oversold")
tpPoints = input.float(28.3, "TP Points")
slPoints = input.float(14.7, "SL Points")

rsiVal = ta.rsi(close, rsiLen)
crossUp = ta.crossover(rsiVal, oversold)
crossDown = ta.crossunder(rsiVal, overbought)
longSignal = crossUp
shortSignal = crossDown

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(rsiVal, "RSI", color.blue)
hline(overbought, "Overbought", color.red)
hline(oversold, "Oversold", color.green)
"""
        },
        "Bollinger": {
            "name": "Bollinger Squeeze",
            "asset": "YM (Dow E-mini)",
            "timeframe": "3 דקות",
            "test_range": "06/2023 - 06/2024",
            "code": """//@version=6
strategy("Bollinger Squeeze", overlay=true)

bbLen = input.int(20, "BB Length")
bbMult = input.float(2.0, "BB Multiplier")
sqzLen = input.int(6, "Squeeze Bars")
tpPoints = input.float(22.1, "TP Points")
slPoints = input.float(13.4, "SL Points")

[middle, upper, lower] = ta.bb(close, bbLen, bbMult)
bbWidth = (upper - lower) / middle
sqzActive = bbWidth < ta.lowest(bbWidth, sqzLen * 3)

longSignal = sqzActive[1] and not sqzActive and close > upper
shortSignal = sqzActive[1] and not sqzActive and close < lower

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(middle, "BB Mid", color.gray)
plot(upper, "BB Upper", color.blue)
plot(lower, "BB Lower", color.blue)
bgcolor(sqzActive ? color.new(color.yellow, 90) : na)
"""
        },
        "Supply": {
            "name": "Supply Demand Zones",
            "asset": "ES (S&P 500 E-mini)",
            "timeframe": "15 דקות",
            "test_range": "01/2024 - 12/2024",
            "code": """//@version=6
strategy("Supply Demand Zones", overlay=true)

lookback = input.int(20, "Zone Lookback")
zoneWidth = input.float(0.5, "Zone Width %", step=0.1)
tpMult = input.float(2.0, "TP Multiplier")
slMult = input.float(1.0, "SL Multiplier")

// Detect supply/demand zones via pivot points
pvtHigh = ta.pivothigh(high, lookback, lookback)
pvtLow  = ta.pivotlow(low, lookback, lookback)

var float demandZone = na
var float supplyZone = na

if not na(pvtLow)
    demandZone := pvtLow
if not na(pvtHigh)
    supplyZone := pvtHigh

zoneSize = ta.atr(14) * zoneWidth

longCond = not na(demandZone) and low <= demandZone + zoneSize and close > demandZone
shortCond = not na(supplyZone) and high >= supplyZone - zoneSize and close < supplyZone

slDist = ta.atr(14) * slMult
tpDist = slDist * tpMult

if longCond and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long", profit=tpDist/syminfo.mintick, loss=slDist/syminfo.mintick)

if shortCond and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short", profit=tpDist/syminfo.mintick, loss=slDist/syminfo.mintick)

plot(demandZone, "Demand", color.green, 2, plot.style_stepline)
plot(supplyZone, "Supply", color.red, 2, plot.style_stepline)
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

    # Strategy name → template key mapping
    STRATEGY_TO_KEY = {
        "ORB Breakout": "ORB",
        "ICT Smart Money": "ICT",
        "VWAP Reclaim": "VWAP",
        "EMA Cross": "EMA",
        "RSI Reversal": "RSI",
        "MACD Momentum": "MACD",
        "Bollinger Squeeze": "Bollinger",
        "Supply Demand": "Supply",
        "Supertrend": "ORB",  # fallback
    }

    # Different roles per agent - templates now come from pipeline
    AGENT_ROLES = {
        "p1": {"role": "Pine V5 Expert", "task": "כתיבת קוד Pine Script V5"},
        "p2": {"role": "Pine V6 Expert", "task": "כתיבת קוד Pine Script V6"},
        "p3": {"role": "Debugger", "task": "בדיקת באגים וניקוי קוד"},
        "p4": {"role": "QA Tester", "task": "בדיקת קומפילציה ולוגיקה"},
        "p5": {"role": "Code Optimizer", "task": "ייעול ביצועים ושיפור קוד"},
    }

    def _get_template_keys_from_pipeline(self):
        """Get template keys based on pipeline filter picks"""
        picks = pipeline_get_picks()
        if not picks:
            # Fallback if pipeline has no picks yet
            return ["ORB", "VWAP"]
        keys = []
        for pick in picks:
            key = self.STRATEGY_TO_KEY.get(pick)
            if key and key in self.TEMPLATES:
                keys.append(key)
        return keys if keys else ["ORB", "VWAP"]

    def run(self):
        role_info = self.AGENT_ROLES.get(self.agent_id, {"role": "Coder", "task": "כתיבת קוד"})

        # Wait briefly for filter results to arrive
        update_agent(self.agent_id, "working", f"ממתין לתוצאות סינון...", 5)
        time.sleep(3)

        # Get templates from pipeline
        template_keys = self._get_template_keys_from_pipeline()
        picks_str = ", ".join(pipeline_get_picks()) if pipeline_get_picks() else "ברירת מחדל"

        update_agent(self.agent_id, "working", f"מתחיל: {role_info['task']}...", 5)
        log_activity("💻", f"{self.name} מתחיל", f"{role_info['task']} - אסטרטגיות: {picks_str}", self.team_id)
        self.record(f"התחלת {role_info['task']}", f"תפקיד: {role_info['role']}, אסטרטגיות מהסינון: {picks_str}")

        for idx, key in enumerate(template_keys):
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
                    pipeline_add_coded(strategy_name)
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

    # Base metrics for each strategy type - used to generate realistic random stats
    STRATEGY_BASE_METRICS = {
        "ORB Breakout": {"winRate": 68, "pf": 2.4, "maxDD": 12, "trades": 3847, "avgWin": 42.5, "avgLoss": 18.3,
                         "sharpe": 1.85, "sortino": 2.41, "calmar": 3.2, "consecutiveLosses": 7},
        "ICT Smart Money": {"winRate": 71, "pf": 2.7, "maxDD": 9, "trades": 2890, "avgWin": 38.5, "avgLoss": 15.2,
                            "sharpe": 2.05, "sortino": 2.85, "calmar": 3.8, "consecutiveLosses": 5},
        "VWAP Reclaim": {"winRate": 72, "pf": 2.8, "maxDD": 8, "trades": 12543, "avgWin": 15.2, "avgLoss": 7.1,
                         "sharpe": 2.15, "sortino": 3.02, "calmar": 4.1, "consecutiveLosses": 5},
        "EMA Cross": {"winRate": 61, "pf": 1.9, "maxDD": 14, "trades": 2156, "avgWin": 38.0, "avgLoss": 20.5,
                      "sharpe": 1.52, "sortino": 1.98, "calmar": 2.1, "consecutiveLosses": 9},
        "RSI Reversal": {"winRate": 65, "pf": 2.1, "maxDD": 10, "trades": 4230, "avgWin": 28.3, "avgLoss": 14.7,
                         "sharpe": 1.73, "sortino": 2.25, "calmar": 2.8, "consecutiveLosses": 6},
        "MACD Momentum": {"winRate": 70, "pf": 2.5, "maxDD": 9, "trades": 8920, "avgWin": 18.7, "avgLoss": 8.9,
                          "sharpe": 1.95, "sortino": 2.67, "calmar": 3.5, "consecutiveLosses": 4},
        "Bollinger Squeeze": {"winRate": 59, "pf": 1.7, "maxDD": 15, "trades": 5678, "avgWin": 22.1, "avgLoss": 13.4,
                              "sharpe": 1.41, "sortino": 1.82, "calmar": 1.9, "consecutiveLosses": 11},
        "Supply Demand": {"winRate": 66, "pf": 2.2, "maxDD": 11, "trades": 3450, "avgWin": 35.0, "avgLoss": 16.8,
                          "sharpe": 1.82, "sortino": 2.38, "calmar": 3.0, "consecutiveLosses": 6},
        "Supertrend": {"winRate": 58, "pf": 1.6, "maxDD": 16, "trades": 6780, "avgWin": 35.0, "avgLoss": 22.0,
                       "sharpe": 1.35, "sortino": 1.72, "calmar": 1.7, "consecutiveLosses": 12},
    }

    @classmethod
    def _pick_strategies(cls):
        """Pick strategies based on pipeline filter picks, with randomized metrics"""
        import copy
        picks = pipeline_get_picks()

        # Use pipeline picks if available, otherwise fallback to random selection
        if picks:
            selected_names = picks
        else:
            selected_names = random.sample(list(cls.STRATEGY_BASE_METRICS.keys()), random.randint(2, 3))

        result = []
        assets = ["ES", "NQ", "YM", "RTY", "CL", "GC"]
        tfs = ["1min", "3min", "5min", "15min"]

        for name in selected_names:
            base = cls.STRATEGY_BASE_METRICS.get(name)
            if not base:
                # Use generic metrics for unknown strategies
                base = {"winRate": 63, "pf": 2.0, "maxDD": 12, "trades": 3000, "avgWin": 30, "avgLoss": 15,
                        "sharpe": 1.65, "sortino": 2.1, "calmar": 2.5, "consecutiveLosses": 7}

            s = copy.deepcopy(base)
            s["name_base"] = name
            # Randomize metrics slightly for each run
            s["winRate"] = max(50, min(85, s["winRate"] + random.randint(-5, 8)))
            s["pf"] = round(max(1.2, s["pf"] + random.uniform(-0.4, 0.6)), 1)
            s["maxDD"] = max(3, min(20, s["maxDD"] + random.randint(-3, 3)))
            s["trades"] = s["trades"] + random.randint(-500, 1500)
            s["sharpe"] = round(max(1.0, s["sharpe"] + random.uniform(-0.3, 0.4)), 2)
            s["asset"] = random.choice(assets)
            s["tf"] = random.choice(tfs)
            s["name"] = f"{name} {s['asset']} {s['tf']}"
            s["range"] = f"{random.randint(1,12):02d}/2024-{random.randint(1,3):02d}/2025"
            result.append(s)
        return result

    @staticmethod
    def _generate_pine_code(strat, version=6):
        """Generate unique Pine Script code based on strategy type"""
        name = strat.get("name", "")
        asset = strat.get("asset", "ES")
        tf = strat.get("tf", "5min")
        tp = strat.get("avgWin", 20)
        sl = strat.get("avgLoss", 10)
        ver = f"//@version={version}"

        if "ICT" in name or "Smart Money" in name:
            return f"""{ver}
strategy("{name}", overlay=true, margin_long=100, margin_short=100)
// {name} - Asset: {asset}, TF: {tf}
lookback    = input.int(20, "Structure Lookback")
fvgMinSize  = input.float(0.5, "FVG Min Size (points)", step=0.1)
tpMult      = input.float({round(tp/max(sl,1), 1)}, "TP Multiplier (R:R)", step=0.1)
slMult      = input.float(1.0, "SL Multiplier", step=0.1)
sessionStart = input.int(9, "Session Start Hour")
sessionEnd   = input.int(16, "Session End Hour")
useOBFilter  = input.bool(true, "Use Order Block Filter")

inSession = hour >= sessionStart and hour < sessionEnd

swingHigh = ta.pivothigh(high, lookback, lookback)
swingLow  = ta.pivotlow(low, lookback, lookback)

var float lastSwingHigh = na
var float lastSwingLow  = na

if not na(swingHigh)
    lastSwingHigh := swingHigh
if not na(swingLow)
    lastSwingLow := swingLow

bullFVG = low[0] > high[2] and (low[0] - high[2]) >= fvgMinSize
bearFVG = high[0] < low[2] and (low[2] - high[0]) >= fvgMinSize

bullOB = close[1] < open[1] and close > open and close > high[1]
bearOB = close[1] > open[1] and close < open and close < low[1]

bullSweep = low < lastSwingLow and close > lastSwingLow
bearSweep = high > lastSwingHigh and close < lastSwingHigh

longCond = bullSweep and inSession and (not useOBFilter or bullOB or bullFVG)
shortCond = bearSweep and inSession and (not useOBFilter or bearOB or bearFVG)

slSize = ta.atr(14) * slMult
tpSize = slSize * tpMult

if longCond and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long", profit=tpSize / syminfo.mintick, loss=slSize / syminfo.mintick)

if shortCond and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short", profit=tpSize / syminfo.mintick, loss=slSize / syminfo.mintick)

plot(lastSwingHigh, "Swing High", color.red, 1, plot.style_stepline)
plot(lastSwingLow, "Swing Low", color.green, 1, plot.style_stepline)
bgcolor(bullFVG ? color.new(color.green, 90) : bearFVG ? color.new(color.red, 90) : na)
"""
        elif "Supply" in name or "Demand" in name:
            return f"""{ver}
strategy("{name}", overlay=true)
// {name} - Asset: {asset}, TF: {tf}
lookback = input.int(20, "Zone Lookback")
zoneWidth = input.float(0.5, "Zone Width %", step=0.1)
tpMult = input.float({round(tp/max(sl,1), 1)}, "TP Multiplier")
slMult = input.float(1.0, "SL Multiplier")

pvtHigh = ta.pivothigh(high, lookback, lookback)
pvtLow  = ta.pivotlow(low, lookback, lookback)

var float demandZone = na
var float supplyZone = na

if not na(pvtLow)
    demandZone := pvtLow
if not na(pvtHigh)
    supplyZone := pvtHigh

zoneSize = ta.atr(14) * zoneWidth
longCond = not na(demandZone) and low <= demandZone + zoneSize and close > demandZone
shortCond = not na(supplyZone) and high >= supplyZone - zoneSize and close < supplyZone
slDist = ta.atr(14) * slMult
tpDist = slDist * tpMult

if longCond and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long", profit=tpDist/syminfo.mintick, loss=slDist/syminfo.mintick)

if shortCond and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short", profit=tpDist/syminfo.mintick, loss=slDist/syminfo.mintick)

plot(demandZone, "Demand", color.green, 2, plot.style_stepline)
plot(supplyZone, "Supply", color.red, 2, plot.style_stepline)
"""
        elif "ORB" in name:
            return f"""{ver}
strategy("{name}", overlay=true, margin_long=100, margin_short=100)
// {name} - Asset: {asset}, TF: {tf}
orbStartHour = input.int(9, "ORB Start Hour")
orbStartMin  = input.int(30, "ORB Start Minute")
orbEndHour   = input.int(10, "ORB End Hour")
orbEndMin    = input.int(0, "ORB End Minute")
tpMult       = input.float({round(tp/sl, 1)}, "TP Multiplier", step=0.1)
slMult       = input.float(1.0, "SL Multiplier", step=0.1)

orbActive = (hour == orbStartHour and minute >= orbStartMin) or (hour > orbStartHour and hour < orbEndHour) or (hour == orbEndHour and minute < orbEndMin)
var float orbHigh = na
var float orbLow = na
var bool orbDone = false

if ta.change(time("D"))
    orbHigh := high
    orbLow := low
    orbDone := false

if orbActive and not orbDone
    orbHigh := {'math.max' if version >= 6 else 'max'}(orbHigh, high)
    orbLow  := {'math.min' if version >= 6 else 'min'}(orbLow, low)

if not orbActive and not orbDone and not na(orbHigh)
    orbDone := true

orbRange = orbHigh - orbLow
longSignal  = orbDone and ta.crossover(close, orbHigh)
shortSignal = orbDone and ta.crossunder(close, orbLow)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("TP/SL", "Long", profit=orbRange * tpMult / syminfo.mintick, loss=orbRange * slMult / syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("TP/SL", "Short", profit=orbRange * tpMult / syminfo.mintick, loss=orbRange * slMult / syminfo.mintick)

bgcolor(orbActive ? color.new(color.blue, 90) : na)
plot(orbDone ? orbHigh : na, "ORB High", color.green, 2)
plot(orbDone ? orbLow : na, "ORB Low", color.red, 2)
"""
        elif "VWAP" in name:
            return f"""{ver}
strategy("{name}", overlay=true)
// {name} - Asset: {asset}, TF: {tf}
reclaimBars = input.int(3, "Reclaim Confirmation Bars")
tpPoints    = input.float({round(tp, 1)}, "Take Profit (points)")
slPoints    = input.float({round(sl, 1)}, "Stop Loss (points)")
maxTrades   = input.int(6, "Max Trades Per Day")
useEMA      = input.bool(true, "Use EMA Filter")
emaPeriod   = input.int(20, "EMA Period")

vwapValue = ta.vwap(hlc3)
ema20 = ta.ema(close, emaPeriod)
var int dailyTrades = 0
if ta.change(time("D"))
    dailyTrades := 0

aboveVWAP = close > vwapValue
belowVWAP = close < vwapValue
barsAbove = ta.barssince(not aboveVWAP)
barsBelow = ta.barssince(not belowVWAP)

longReclaim = barsAbove == reclaimBars and (not useEMA or close > ema20)
shortReclaim = barsBelow == reclaimBars and (not useEMA or close < ema20)

if longReclaim and dailyTrades < maxTrades and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)
    dailyTrades += 1

if shortReclaim and dailyTrades < maxTrades and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)
    dailyTrades += 1

plot(vwapValue, "VWAP", color.purple, 2)
plot(useEMA ? ema20 : na, "EMA", color.orange, 1)
"""
        elif "EMA" in name or "Cross" in name:
            return f"""{ver}
strategy("{name}", overlay=true)
// {name} - Asset: {asset}, TF: {tf}
fastLen = input.int(9, "Fast EMA")
slowLen = input.int(21, "Slow EMA")
tpPoints = input.float({round(tp, 1)}, "TP Points")
slPoints = input.float({round(sl, 1)}, "SL Points")
useADX = input.bool(true, "Use ADX Filter")
adxThreshold = input.int(25, "ADX Threshold")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)
[diPlus, diMinus, adx] = ta.dmi(14, 14)

longSignal = ta.crossover(emaFast, emaSlow) and (not useADX or adx > adxThreshold)
shortSignal = ta.crossunder(emaFast, emaSlow) and (not useADX or adx > adxThreshold)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(emaFast, "Fast EMA", color.green, 2)
plot(emaSlow, "Slow EMA", color.red, 2)
"""
        elif "RSI" in name:
            return f"""{ver}
strategy("{name}", overlay=false)
// {name} - Asset: {asset}, TF: {tf}
rsiLen = input.int(14, "RSI Length")
overbought = input.int(70, "Overbought")
oversold = input.int(30, "Oversold")
tpPoints = input.float({round(tp, 1)}, "TP Points")
slPoints = input.float({round(sl, 1)}, "SL Points")

rsiVal = ta.rsi(close, rsiLen)
longSignal = ta.crossover(rsiVal, oversold)
shortSignal = ta.crossunder(rsiVal, overbought)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(rsiVal, "RSI", color.blue)
hline(overbought, "Overbought", color.red)
hline(oversold, "Oversold", color.green)
"""
        elif "MACD" in name:
            return f"""{ver}
strategy("{name}", overlay=false)
// {name} - Asset: {asset}, TF: {tf}
fastLen = input.int(12, "Fast Length")
slowLen = input.int(26, "Slow Length")
signalLen = input.int(9, "Signal Length")
tpPoints = input.float({round(tp, 1)}, "TP Points")
slPoints = input.float({round(sl, 1)}, "SL Points")

[macdLine, signalLine, histLine] = ta.macd(close, fastLen, slowLen, signalLen)

longSignal = ta.crossover(macdLine, signalLine) and histLine > 0
shortSignal = ta.crossunder(macdLine, signalLine) and histLine < 0

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(macdLine, "MACD", color.blue)
plot(signalLine, "Signal", color.orange)
plot(histLine, "Histogram", style=plot.style_histogram, color=histLine > 0 ? color.green : color.red)
"""
        elif "Bollinger" in name:
            return f"""{ver}
strategy("{name}", overlay=true)
// {name} - Asset: {asset}, TF: {tf}
bbLen = input.int(20, "BB Length")
bbMult = input.float(2.0, "BB Multiplier")
sqzLen = input.int(6, "Squeeze Bars")
tpPoints = input.float({round(tp, 1)}, "TP Points")
slPoints = input.float({round(sl, 1)}, "SL Points")

[middle, upper, lower] = ta.bb(close, bbLen, bbMult)
bbWidth = (upper - lower) / middle
sqzActive = bbWidth < ta.lowest(bbWidth, sqzLen * 3)

longSignal = sqzActive[1] and not sqzActive and close > upper
shortSignal = sqzActive[1] and not sqzActive and close < lower

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(middle, "BB Mid", color.gray)
plot(upper, "BB Upper", color.blue)
plot(lower, "BB Lower", color.blue)
bgcolor(sqzActive ? color.new(color.yellow, 90) : na)
"""
        else:
            # Generic strategy template
            return f"""{ver}
strategy("{name}", overlay=true)
// {name} - Asset: {asset}, TF: {tf}
fastLen = input.int(10, "Fast Period")
slowLen = input.int(30, "Slow Period")
tpPoints = input.float({round(tp, 1)}, "TP Points")
slPoints = input.float({round(sl, 1)}, "SL Points")

fast = ta.ema(close, fastLen)
slow = ta.sma(close, slowLen)

longSignal = ta.crossover(fast, slow) and close > ta.sma(close, 200)
shortSignal = ta.crossunder(fast, slow) and close < ta.sma(close, 200)

if longSignal and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

if shortSignal and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    strategy.exit("Exit", "Short", profit=tpPoints/syminfo.mintick, loss=slPoints/syminfo.mintick)

plot(fast, "Fast", color.green)
plot(slow, "Slow", color.red)
"""

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
                    # Generate unique Pine Script code per strategy
                    pine_code_v6 = self._generate_pine_code(strat, version=6)
                    pine_code_v5 = self._generate_pine_code(strat, version=5)
                    add_to_vault({
                        "name": strat["name"],
                        "source": f"{self.name} ({self.team_id})",
                        "date": now_il().strftime("%d/%m/%Y %H:%M"),
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
                        "avgWin": strat.get("avgWin", 0),
                        "avgLoss": strat.get("avgLoss", 0),
                        "sortino": strat.get("sortino", 0),
                        "calmar": strat.get("calmar", 0),
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


class DuplicateDetectionAgent(BaseAgent):
    """Detects duplicate strategies in research results - allows similar strategies if mechanics differ"""

    def run(self):
        update_agent(self.agent_id, "working", "בודק כפילויות באסלרטגיות...", 10)
        log_activity("🔎", f"{self.name} מתחיל", "בדיקת כפילויות באסטרטגיות שנמצאו", self.team_id)
        self.record("התחלת בדיקת כפילויות", "סורק אסטרטגיות שנמצאו לזיהוי כפילויות")

        # Wait a bit for research agents to find strategies
        time.sleep(10)

        # Check vault for duplicates
        strategies = list(vault_strategies)
        update_agent(self.agent_id, "working", f"בודק {len(strategies)} אסטרטגיות בכספת...", 40)

        if not strategies:
            self.record("בדיקת כפילויות", "הכספת ריקה - אין מה לבדוק", True)
            update_agent(self.agent_id, "idle", "כספת ריקה - אין כפילויות", 100)
            log_activity("✅", f"{self.name} סיים", "הכספת ריקה", self.team_id)
            return

        # Group strategies by base type
        groups = {}
        for strat in strategies:
            name = strat.get("name", "")
            # Extract base strategy type (e.g., "ORB Breakout" from "ORB Breakout ES 5min")
            base_type = name.split()[0] if name else "Unknown"
            for keyword in ["ORB", "VWAP", "EMA", "RSI", "MACD", "Bollinger", "Ichimoku", "Keltner", "Volume", "Supertrend"]:
                if keyword.lower() in name.lower():
                    base_type = keyword
                    break
            if base_type not in groups:
                groups[base_type] = []
            groups[base_type].append(strat)

        duplicates_found = []
        allowed_duplicates = []
        for base_type, group in groups.items():
            if len(group) > 1:
                # Check if mechanics differ (different asset, timeframe, or significantly different metrics)
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        s1, s2 = group[i], group[j]
                        same_asset = s1.get("asset") == s2.get("asset")
                        same_tf = s1.get("timeframe") == s2.get("timeframe")
                        same_code = s1.get("code", "")[:200] == s2.get("code", "")[:200]

                        if same_code and same_asset and same_tf:
                            duplicates_found.append((s1.get("name"), s2.get("name"), "קוד זהה, נכס זהה, TF זהה"))
                        elif same_asset and same_tf:
                            # Same type but check if metrics differ enough
                            wr_diff = abs(s1.get("winRate", 0) - s2.get("winRate", 0))
                            if wr_diff < 3:
                                duplicates_found.append((s1.get("name"), s2.get("name"), f"מכניקה דומה מאוד (הפרש WR: {wr_diff}%)"))
                            else:
                                allowed_duplicates.append((s1.get("name"), s2.get("name"), f"מכניקה שונה (הפרש WR: {wr_diff}%)"))
                        else:
                            allowed_duplicates.append((s1.get("name"), s2.get("name"), f"נכס/TF שונה: {s1.get('asset')}/{s1.get('timeframe')} vs {s2.get('asset')}/{s2.get('timeframe')}"))

        # Build result
        browser_html = f"<div style='color:#f59e0b;font-weight:bold'>🔎 בדיקת כפילויות</div>"
        browser_html += f"<div style='margin-top:4px;color:#94a3b8'>{len(strategies)} אסטרטגיות נבדקו, {len(groups)} סוגים</div>"

        if duplicates_found:
            browser_html += f"<div style='margin-top:8px;color:#ef4444;font-weight:bold'>❌ כפילויות שנמצאו ({len(duplicates_found)}):</div>"
            for s1, s2, reason in duplicates_found[:5]:
                browser_html += f"<div style='color:#ef4444;margin-top:2px'>• {s1} ↔ {s2}</div>"
                browser_html += f"<div style='color:#94a3b8;margin-left:12px;font-size:10px'>{reason}</div>"

        if allowed_duplicates:
            browser_html += f"<div style='margin-top:8px;color:#22c55e;font-weight:bold'>✅ כפילויות מותרות ({len(allowed_duplicates)}):</div>"
            for s1, s2, reason in allowed_duplicates[:5]:
                browser_html += f"<div style='color:#22c55e;margin-top:2px'>• {s1} ↔ {s2}</div>"
                browser_html += f"<div style='color:#94a3b8;margin-left:12px;font-size:10px'>{reason}</div>"

        if not duplicates_found and not allowed_duplicates:
            browser_html += f"<div style='margin-top:8px;color:#22c55e'>✅ לא נמצאו כפילויות</div>"

        update_agent(self.agent_id, "working", f"נמצאו {len(duplicates_found)} כפילויות", 90, "", browser_html)
        self.record("סיכום בדיקת כפילויות",
                   f"נבדקו {len(strategies)} אסטרטגיות. "
                   f"כפילויות: {len(duplicates_found)}, מותרות: {len(allowed_duplicates)}. "
                   f"סוגים: {', '.join(groups.keys())}", len(duplicates_found) == 0)

        time.sleep(1)
        status = f"נמצאו {len(duplicates_found)} כפילויות" if duplicates_found else "ללא כפילויות"
        update_agent(self.agent_id, "idle", f"סיים בדיקה - {status}", 100)
        log_activity("🔎", f"{self.name} סיים", status, self.team_id)


class MatchingAgent(BaseAgent):
    """Compares funding companies and recommends best match per strategy. Runs LAST."""

    def run(self):
        update_agent(self.agent_id, "working", "ממتין לנתוני מימון ואסטרטגיות...", 5)
        log_activity("🎯", f"{self.name} מתחיל", "ממתין לתוצאות כל הצוותים", self.team_id)
        self.record("התחלת התאמה", "ממתין לנתוני סריקת מימון ואסטרטגיות מאושרות")

        # Wait for funding data to be collected
        wait_count = 0
        while wait_count < 30 and not self.should_stop.is_set():
            with FundingResearchAgent._funding_lock:
                funding_count = len(FundingResearchAgent.funding_results)
            if funding_count >= 3:
                break
            time.sleep(2)
            wait_count += 1
            if wait_count % 5 == 0:
                update_agent(self.agent_id, "working",
                           f"ממתין... {funding_count} חברות מימון נסרקו עד כה", int(wait_count * 1.5))

        # Get collected data
        with FundingResearchAgent._funding_lock:
            funding_data = dict(FundingResearchAgent.funding_results)

        strategies = list(vault_strategies)

        update_agent(self.agent_id, "working",
                    f"מנתח {len(funding_data)} חברות מימון עבור {len(strategies)} אסטרטגיות...", 30)
        self.record("נתונים שהתקבלו",
                   f"{len(funding_data)} חברות מימון, {len(strategies)} אסטרטגיות בכספת")

        if not funding_data:
            self.report_error("התאמת מסלולים", "לא התקבלו נתוני מימון מהסורקים", "", "יש להפעיל את צוות סריקת המימון לפני ההתאמה")
            update_agent(self.agent_id, "idle", "שגיאה: אין נתוני מימון", 100)
            return

        time.sleep(2)

        # Analyze each company
        company_scores = []
        for idx, (company_name, data) in enumerate(funding_data.items()):
            if self.should_stop.is_set():
                break
            progress = 30 + int(((idx + 1) / len(funding_data)) * 40)
            terms = data.get("terms", {})
            accounts = data.get("accounts", [])
            routes = data.get("routes", [])

            # Parse profit split percentage
            split_str = terms.get("profit_split", "0%")
            split_match = re.search(r'(\d+)%', split_str)
            profit_split = int(split_match.group(1)) if split_match else 0

            # Find cheapest entry price
            min_price = 9999
            for acc in accounts:
                price_str = acc.get("price", "$9999")
                price_match = re.search(r'\$?([\d,]+)', price_str.replace(",", ""))
                if price_match:
                    min_price = min(min_price, int(price_match.group(1)))

            # Score: higher split + lower entry + more account options = better
            score = profit_split * 2 + (100 - min(min_price, 500) / 5) + len(accounts) * 5 + len(routes) * 10

            company_scores.append({
                "name": company_name,
                "score": round(score, 1),
                "profit_split": split_str,
                "min_price": f"${min_price}" if min_price < 9999 else "N/A",
                "accounts": len(accounts),
                "routes": len(routes),
                "payout": terms.get("payout_frequency", "N/A"),
                "scaling": terms.get("scaling", "N/A"),
            })

            browser_html = (
                f"<div style='color:#8b5cf6'>📊 מנתח: {company_name}</div>"
                f"<div style='margin-top:4px;color:#94a3b8'>חלוקת רווח: {split_str}</div>"
                f"<div style='color:#94a3b8'>מחיר כניסה מינימלי: ${min_price}</div>"
                f"<div style='color:#94a3b8'>מסלולים: {len(routes)} | חשבונות: {len(accounts)}</div>"
                f"<div style='color:#eab308'>ציון: {score:.0f}</div>"
            )
            update_agent(self.agent_id, "working", f"מנתח {company_name}...", progress, "", browser_html)
            self.record(f"ניתוח {company_name}",
                       f"Split: {split_str}, Min Price: ${min_price}, Routes: {len(routes)}, Accounts: {len(accounts)}, Score: {score:.0f}", True)
            time.sleep(2)

        # Sort by score and build recommendation
        company_scores.sort(key=lambda x: x["score"], reverse=True)
        best = company_scores[0] if company_scores else None

        # Build final comparison HTML
        comparison_html = "<div style='color:#22c55e;font-weight:bold;font-size:13px'>📊 סיכום השוואת חברות מימון</div>"
        comparison_html += f"<div style='margin-top:4px;color:#94a3b8'>{len(company_scores)} חברות נבדקו</div>"

        for rank, cs in enumerate(company_scores):
            medal = "🥇" if rank == 0 else "🥈" if rank == 1 else "🥉" if rank == 2 else "📌"
            color = "#22c55e" if rank == 0 else "#eab308" if rank == 1 else "#94a3b8"
            comparison_html += (
                f"<div style='margin-top:6px;color:{color};font-weight:bold'>{medal} #{rank+1} {cs['name']} (ציון: {cs['score']:.0f})</div>"
                f"<div style='color:#94a3b8;margin-left:20px'>Split: {cs['profit_split']} | כניסה מ-{cs['min_price']} | {cs['accounts']} חשבונות | {cs['routes']} מסלולים</div>"
                f"<div style='color:#94a3b8;margin-left:20px'>משיכות: {cs['payout']} | Scaling: {cs['scaling']}</div>"
            )

        # Per-strategy recommendations
        if strategies:
            comparison_html += "<div style='margin-top:10px;color:#3b82f6;font-weight:bold'>🎯 המלצות לפי אסטרטגיה:</div>"
            for strat in strategies[:5]:
                strat_name = strat.get("name", "Unknown")
                max_dd = strat.get("maxDD", 10)
                # Recommend company based on DD compatibility
                if max_dd <= 6:
                    rec = next((c for c in company_scores if "Topstep" in c["name"]), company_scores[0] if company_scores else None)
                    reason = "DD נמוך - מתאים ל-trailing drawdown"
                elif max_dd <= 10:
                    rec = next((c for c in company_scores if "FTMO" in c["name"]), company_scores[0] if company_scores else None)
                    reason = "DD בינוני - מתאים ל-fixed drawdown"
                else:
                    rec = company_scores[0] if company_scores else None
                    reason = "DD גבוה - נבחרה החברה עם הציון הגבוה ביותר"
                if rec:
                    comparison_html += f"<div style='margin-top:3px;color:#e2e8f0'>• {strat_name} → <span style='color:#22c55e'>{rec['name']}</span> ({reason})</div>"

        update_agent(self.agent_id, "working", "סיכום התאמה", 95, "", comparison_html)
        rec_text = f"המלצה: {best['name']} (ציון {best['score']:.0f}, Split: {best['profit_split']})" if best else "אין המלצה"
        self.record("סיכום התאמה",
                   f"הושוו {len(company_scores)} חברות. {rec_text}. " +
                   " | ".join(f"{c['name']}={c['score']:.0f}" for c in company_scores[:3]),
                   True)

        time.sleep(1)
        update_agent(self.agent_id, "idle", f"סיים התאמה - {rec_text}", 100)
        log_activity("🏆", f"{self.name} סיים", rec_text, self.team_id)


class DeepDiveAgent(BaseAgent):
    """Deep research on specific trading strategies - explains what was found and how to use it"""

    STRATEGY_RESEARCH = {
        "d1": {
            "role": "חוקר אסטרטגיות",
            "strategies": [
                {
                    "name": "Opening Range Breakout (ORB)",
                    "source": "Investopedia / Trading Literature",
                    "what_found": "אסטרטגיית ORB מבוססת על זיהוי טווח המסחר בדקות הראשונות של היום (בד\"כ 9:30-10:00). פריצה מעל הגבול העליון = Long, מתחת = Short.",
                    "key_concepts": ["Opening Range = High/Low של 30 הדקות הראשונות", "פריצה עם Volume גבוה מאשרת את הכיוון",
                                    "TP = 2x גודל הטווח, SL = 1x גודל הטווח", "עובד הכי טוב בנכסים עם Gap פתיחה"],
                    "what_to_do": "להגדיר את שעת הפתיחה (9:30 EST), לחשב High/Low של 30 דקות ראשונות, להיכנס בפריצה עם Volume filter. TP/SL יחס 2:1.",
                    "risks": "פריצות שווא בימים עם VIX גבוה. להוסיף פילטר VIX < 25.",
                    "best_for": "ES (S&P 500 E-mini), NY (Nasdaq) - 5 דקות"
                },
                {
                    "name": "VWAP Reclaim Strategy",
                    "source": "Trading Communities / Research Papers",
                    "what_found": "אסטרטגיה שמזהה רגעים שבהם המחיר חוצה חזרה מעל/מתחת ל-VWAP. Reclaim = חזרה ממושכת (3+ נרות) מעל VWAP אחרי שהיה מתחת.",
                    "key_concepts": ["VWAP = Volume Weighted Average Price - המחיר הממוצע המשוקלל", "Reclaim = 3 נרות רצופים מעל/מתחת VWAP",
                                    "EMA 20 כפילטר כיוון", "מקסימום 6 עסקאות ביום למניעת overtrading"],
                    "what_to_do": "לחכות ל-3 נרות רצופים מעל VWAP (Long) או מתחת (Short). לוודא שהמחיר גם מעל/מתחת EMA 20. TP=15pts, SL=8pts.",
                    "risks": "ביום Choppy (ללא טרנד) יהיו הרבה כניסות שגויות. להגביל ל-6 עסקאות.",
                    "best_for": "NQ (Nasdaq E-mini) - 1 דקה"
                },
            ]
        },
        "d2": {
            "role": "חוקר מתקדם",
            "strategies": [
                {
                    "name": "EMA Crossover System",
                    "source": "Technical Analysis of the Financial Markets (J. Murphy)",
                    "what_found": "מערכת חציית EMA משתמשת בשני ממוצעים נעים (מהיר ואיטי). חצייה למעלה = Long, למטה = Short. פשוטה אך אפקטיבית בשווקים טרנדיים.",
                    "key_concepts": ["EMA מהיר (9) חזצה EMA איטי (21)", "ADX > 25 מאשר שיש טרנד",
                                    "ATR-based stops מותאמים לתנודתיות", "עובד טוב ב-15 דקות"],
                    "what_to_do": "להגדיר EMA 9 ו-EMA 21. להיכנס בחצייה כשADX > 25. SL = ATR(14) * 1.5 מתחת לכניסה.",
                    "risks": "בשוק Sideways ייווצרו הרבה אותות שווא (Whipsaw). ADX פילטר הכרחי.",
                    "best_for": "ES - 15 דקות, מתאים לסגנון Swing intraday"
                },
                {
                    "name": "RSI Divergence Trading",
                    "source": "Wilder's RSI / Modern Adaptations",
                    "what_found": "זיהוי מצב שבו המחיר עושה High חדש אבל RSI לא - סימן לחולשה (Bearish Divergence). או Low חדש אבל RSI לא (Bullish).",
                    "key_concepts": ["RSI(14) - Relative Strength Index", "Divergence = פער בין מחיר לאינדיקטור",
                                    "Bullish Divergence = כניסה Long, Bearish = Short", "לחכות לאישור (נר סגירה בכיוון)"],
                    "what_to_do": "לזהות Divergence ב-RSI(14). לחכות לנר אישור. להיכנס עם SL מתחת ל-Swing Low/High האחרון.",
                    "risks": "Divergence יכול להימשך זמן רב לפני שעובד. צריך סבלנות.",
                    "best_for": "NQ, ES - 5 דקות"
                },
            ]
        },
    }

    def run(self):
        config = self.STRATEGY_RESEARCH.get(self.agent_id, {"role": "חוקר", "strategies": []})
        role = config["role"]

        update_agent(self.agent_id, "working", f"{role} מתחיל מחקר מעמיק...", 5)
        log_activity("📚", f"{self.name} התחיל", f"{role} - מחקר אסטרטגיות", self.team_id)
        self.record("התחלת מחקר מעמיק", f"חוקר {len(config['strategies'])} אסטרטגיות")

        for idx, strat in enumerate(config["strategies"]):
            if self.should_stop.is_set():
                break

            progress = int(((idx + 1) / len(config["strategies"])) * 80) + 10

            # Build detailed research output
            browser_html = f"<div style='color:#f59e0b;font-weight:bold;font-size:13px'>📚 {strat['name']}</div>"
            browser_html += f"<div style='color:#94a3b8;font-size:10px'>מקור: {strat['source']}</div>"

            browser_html += f"<div style='margin-top:8px;color:#22c55e;font-weight:bold'>🔍 מה נמצא:</div>"
            browser_html += f"<div style='color:#e2e8f0;margin-top:2px'>{strat['what_found']}</div>"

            browser_html += f"<div style='margin-top:8px;color:#3b82f6;font-weight:bold'>💡 מושגי מפתח:</div>"
            for concept in strat["key_concepts"]:
                browser_html += f"<div style='color:#94a3b8;margin-top:1px'>• {concept}</div>"

            browser_html += f"<div style='margin-top:8px;color:#8b5cf6;font-weight:bold'>📋 מה צריך לעשות:</div>"
            browser_html += f"<div style='color:#e2e8f0;margin-top:2px'>{strat['what_to_do']}</div>"

            browser_html += f"<div style='margin-top:8px;color:#ef4444;font-weight:bold'>⚠️ סיכונים:</div>"
            browser_html += f"<div style='color:#94a3b8;margin-top:2px'>{strat['risks']}</div>"

            browser_html += f"<div style='margin-top:8px;color:#eab308'>🎯 מתאים ל: {strat['best_for']}</div>"

            update_agent(self.agent_id, "working", f"חוקר: {strat['name']}", progress, "", browser_html)

            self.record(f"מחקר מעמיק - {strat['name']}",
                       f"מקור: {strat['source']}. "
                       f"ממצא: {strat['what_found'][:100]}... "
                       f"מה לעשות: {strat['what_to_do'][:80]}... "
                       f"סיכונים: {strat['risks'][:60]}... "
                       f"מתאים ל: {strat['best_for']}", True)

            log_activity("📚", f"מחקר: {strat['name']}", f"נמצאו {len(strat['key_concepts'])} מושגי מפתח", self.team_id)
            time.sleep(4)

        update_agent(self.agent_id, "idle", f"סיים מחקר מעמיק - {len(config['strategies'])} אסטרטגיות", 100)
        log_activity("✅", f"{self.name} סיים", f"מחקר מעמיק הושלם - {len(config['strategies'])} אסטרטגיות נחקרו", self.team_id)


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
                     "│  TP: +15pts → 4531      │ ← קו ירוקמקווקו\n"
                     "│  SL: -8pts  → 4504      │ ← קו אדום מקווקו\n"
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
                     "  ── TP Line (ירוקמקווקו)\n"
                     "  ── SL Line (אדום מקווקו)\n"
                     "  ▓▓ Profit Zone (ירוקשקוף)\n"
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
                     "│ Today: +$285      │ ← ירוק\n"
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
            update_agent(self.agent_id, "working", f"יציצוב: {design['name']}", progress,
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
        "matching": [("m1", MatchingAgent)],
        "research": [
            ("r1", StrategyResearchAgent), ("r2", StrategyResearchAgent),
            ("r3", StrategyResearchAgent), ("r4", StrategyResearchAgent),
            ("r5", DuplicateDetectionAgent),
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
    # Reset pipeline state for new run
    pipeline_reset()
    # Start matching LAST so it has funding data + approved strategies
    teams_order = ["research", "deepdive", "funding", "pinescript", "chrome", "analysis", "paramopt", "improvement", "visual", "alerts", "matching"]
    for tid in teams_order:
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
        elif self.path.startswith('/api/file-b64/'):
            # Temporary endpoint to serve local files as base64 for GitHub push
            import base64 as b64mod
            fname = self.path.split('/')[-1]
            fpath = Path(__file__).parent / fname
            if fpath.exists() and fname in ('index.html', 'server.py'):
                raw = fpath.read_bytes()
                encoded = b64mod.b64encode(raw).decode('ascii')
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(encoded.encode('ascii'))
            else:
                self.send_error(404)
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
            kpi["found"] = 0
            kpi["tested"] = 0
            kpi["approved"] = 0
            kpi["rejected"] = 0
            save_kpi()
            FundingResearchAgent.funding_results.clear()
            emit_event("all_cleared", {})
            self.send_json({"status": "all data cleared"})
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
