"""SPY Levels web app.

Serves the dashboard and a JSON API, and keeps the data fresh in a background
thread:
  - market hours (Mon-Fri 9:30-16:00 ET): price every 60 s, option chains every 15 min
  - otherwise: price every 30 min, option chains every 60 min

Environment variables (all optional):
  PORT               port to listen on (default 8000; hosts set this for you)
  APP_PASSWORD       turn on a password prompt for the whole site
  NTFY_TOPIC         send phone alerts to https://ntfy.sh/<topic> (install the ntfy app, subscribe to the topic)
  NTFY_SERVER        ntfy server (default https://ntfy.sh)
  DATA_PROVIDER      yahoo (default) or tradier
  TRADIER_TOKEN      Tradier API token (when DATA_PROVIDER=tradier)
  TRADIER_SANDBOX    1 to use Tradier's free sandbox (15-minute delayed)
  PRICE_SECONDS      price refresh interval in market hours (default 60)
  CHAIN_MINUTES      option-chain refresh interval in market hours (default 15)

Run locally:  python -m app.server   then open http://localhost:8000
"""
import base64, datetime as dt, hmac, json, os, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import engine

ET = engine.ET
ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "static" / "index.html").read_bytes()

PRICE_SECONDS = int(os.environ.get("PRICE_SECONDS", "60"))
CHAIN_MINUTES = int(os.environ.get("CHAIN_MINUTES", "15"))
PASSWORD = os.environ.get("APP_PASSWORD", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.doc = None
        self.body = b"{}"
        self.bars = None
        self.bars_at = 0.0
        self.chain = None
        self.chain_at = 0.0
        self.error = None
        self.error_at = None
        self.last_refresh_request = 0.0
        self.last_alert_key = None
        self.last_alert_at = 0.0
        self.last_regime = None


S = State()


def log(*a):
    print(dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- alerts
def notify(title, message, tags="chart_with_upwards_trend", priority="default"):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=message.encode(), method="POST",
                                     headers={"Title": title, "Tags": tags, "Priority": priority})
        urllib.request.urlopen(req, timeout=10).read()
        log("alert sent:", title, "-", message)
    except Exception as e:  # noqa: BLE001
        log("alert failed:", e)


def maybe_alert(doc):
    if not doc.get("marketOpen"):
        return
    a = doc.get("alert")
    now = time.time()
    if a:
        key = a["level"]
        # one alert per level, repeated at most every 20 minutes
        if key != S.last_alert_key or now - S.last_alert_at > 1200:
            notify(f"SPY at {a['level']}", f"{a['msg']}. Regime: {doc['regime']['label']}.", tags="round_pushpin")
            S.last_alert_key, S.last_alert_at = key, now
    elif S.last_alert_key and now - S.last_alert_at > 300:
        S.last_alert_key = None
    reg = doc["regime"]["label"]
    if S.last_regime and reg != S.last_regime:
        notify(f"SPY regime changed: {reg}", doc["regime"]["detail"], tags="warning", priority="high")
    S.last_regime = reg


# ---------------------------------------------------------------- refresh loop
def refresh(provider, force_chain=False):
    now_ts = time.time()
    market = engine.is_market_hours()
    chain_every = CHAIN_MINUTES * 60 if market else 3600
    need_chain = force_chain or S.chain is None or now_ts - S.chain_at >= chain_every
    try:
        bars = provider.bars()
        S.bars, S.bars_at = bars, now_ts
        if need_chain:
            try:
                S.chain, S.chain_at = provider.chain_rows(), now_ts
            except engine.DataError as e:
                log("chain refresh failed, keeping previous chain:", e)
                if S.chain is None:
                    raise
        doc = engine.compute(S.bars, S.chain, provider.name)
        doc["chainAt"] = dt.datetime.fromtimestamp(S.chain_at, ET).isoformat(timespec="seconds")
        doc["status"] = {"ok": True}
        with S.lock:
            S.doc = doc
            S.body = json.dumps(doc, separators=(",", ":")).encode()
            S.error = None
        maybe_alert(doc)
        log(f"refreshed price {doc['price']} flip {doc['flip']} {doc['regime']['label']}"
            + (" (chain updated)" if need_chain else ""))
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        log("refresh failed:", msg)
        traceback.print_exc()
        with S.lock:
            S.error, S.error_at = msg, dt.datetime.now(ET).isoformat(timespec="seconds")
            if S.doc:
                d = dict(S.doc)
                d["status"] = {"ok": False, "error": msg, "at": S.error_at}
                S.body = json.dumps(d, separators=(",", ":")).encode()


def loop():
    try:
        provider = engine.make_provider()
    except engine.DataError as e:
        with S.lock:
            S.error = str(e)
        log("provider error:", e)
        return
    log("data provider:", provider.name)
    backoff = 0
    while True:
        force = S.wake.is_set()
        S.wake.clear()
        refresh(provider, force_chain=force and time.time() - S.chain_at > 120)
        market = engine.is_market_hours()
        if S.error:
            backoff = min(backoff * 2 or 30, 300)   # 30 s, 60 s, 120 s ... up to 5 min
            wait = backoff
        else:
            backoff = 0
            wait = PRICE_SECONDS if market else 1800
            if market and PRICE_SECONDS == 60:
                # land ~5 s after each new 1-minute bar closes
                wait = 65 - dt.datetime.now(ET).second
        S.wake.wait(timeout=wait)


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "SPYLevels/1.0"

    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user_pass = base64.b64decode(h[6:]).decode()
                pw = user_pass.split(":", 1)[1] if ":" in user_pass else ""
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:  # noqa: BLE001
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SPY Levels", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            ok = S.doc is not None
            return self._send(200 if ok else 503, b"ok" if ok else b"starting", "text/plain")
        if not self._authed():
            return
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/levels":
            with S.lock:
                body = S.body if S.doc else json.dumps({"status": {"ok": False, "error": S.error or "Loading the first data pull…"}}).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authed():
            return
        if path == "/api/refresh":
            now = time.time()
            if now - S.last_refresh_request < 30:
                return self._send(429, b'{"ok":false,"message":"Refreshed recently. Try again in 30 seconds."}', "application/json")
            S.last_refresh_request = now
            S.wake.set()
            return self._send(202, b'{"ok":true}', "application/json")
        self._send(404, b"not found", "text/plain")


def main():
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=loop, daemon=True, name="refresher").start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"SPY Levels on http://0.0.0.0:{port}" + (" (password on)" if PASSWORD else "") + (f" alerts -> ntfy topic '{NTFY_TOPIC}'" if NTFY_TOPIC else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
