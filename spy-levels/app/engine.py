"""SPY Levels engine: data providers + level/gamma computation.

Standard library only (plus the `tzdata` package on systems without a
timezone database). Two providers:

  yahoo   - default, no account needed. Yahoo can rate-limit cloud IPs.
  tradier - set DATA_PROVIDER=tradier and TRADIER_TOKEN. Use
            TRADIER_SANDBOX=1 for a free sandbox token (15-min delayed data).

Run once from the command line:  python -m app.engine out.json
"""
import json, math, os, sys, time, datetime as dt, urllib.request, urllib.parse, http.cookiejar
from statistics import NormalDist
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PDF = NormalDist().pdf
SYMBOL = os.environ.get("SYMBOL", "SPY")
N_EXPIRIES = int(os.environ.get("N_EXPIRIES", "6"))
STOP = float(os.environ.get("STOP_DISTANCE", "0.30"))
ALERT_DISTANCE = float(os.environ.get("ALERT_DISTANCE", "0.25"))


class DataError(Exception):
    pass


# ====================================================================== providers
class YahooProvider:
    name = "Yahoo Finance"

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [("User-Agent", "Mozilla/5.0")]
        self._crumb = None

    def _get(self, url, tries=3):
        last = None
        for i in range(tries):
            try:
                with self.opener.open(url, timeout=25) as r:
                    return r.read().decode()
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (i + 1))
        raise DataError(f"Yahoo request failed: {last}")

    def bars(self):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
               "?interval=1m&range=5d&includePrePost=false")
        d = json.loads(self._get(url))
        res = (d.get("chart") or {}).get("result")
        if not res:
            raise DataError("Yahoo returned no price data")
        r = res[0]
        q = r["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(r.get("timestamp") or []):
            c = q["close"][i]
            if c is None:
                continue
            ts = dt.datetime.fromtimestamp(t, ET)
            if dt.time(9, 30) <= ts.time() < dt.time(16, 0):
                out.append(dict(t=ts, o=q["open"][i], h=q["high"][i], l=q["low"][i], c=c, v=q["volume"][i] or 0))
        return out

    def _crumb_value(self, force=False):
        if self._crumb and not force:
            return self._crumb
        for u in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                self._get(u, tries=1)
            except DataError:
                pass
        self._crumb = self._get("https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
        return self._crumb

    def chain_rows(self):
        for attempt in (0, 1):
            try:
                base = f"https://query1.finance.yahoo.com/v7/finance/options/{SYMBOL}?crumb={self._crumb_value(force=attempt == 1)}"
                first = json.loads(self._get(base))["optionChain"]["result"][0]
                break
            except (DataError, KeyError, IndexError, TypeError):
                if attempt == 1:
                    raise DataError("Yahoo option chain unavailable")
        rows = []
        for e in first["expirationDates"][:N_EXPIRIES]:
            o = json.loads(self._get(base + f"&date={e}"))["optionChain"]["result"][0]["options"][0]
            exp = dt.datetime.fromtimestamp(o["expirationDate"], dt.timezone.utc).date().isoformat()
            for side, typ in (("calls", "call"), ("puts", "put")):
                for c in o.get(side, []):
                    rows.append({"exp": exp, "k": c["strike"], "type": typ,
                                 "oi": c.get("openInterest") or 0, "iv": c.get("impliedVolatility") or 0})
        return rows


class TradierProvider:
    name = "Tradier"

    def __init__(self):
        self.token = os.environ.get("TRADIER_TOKEN", "")
        if not self.token:
            raise DataError("TRADIER_TOKEN is not set")
        sandbox = os.environ.get("TRADIER_SANDBOX", "0") == "1"
        self.base = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"

    def _get(self, path, params):
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        last = None
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode())
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (i + 1))
        raise DataError(f"Tradier request failed: {last}")

    def bars(self):
        end = dt.datetime.now(ET)
        start = end - dt.timedelta(days=7)
        d = self._get("/markets/timesales", {"symbol": SYMBOL, "interval": "1min",
                                              "start": start.strftime("%Y-%m-%d 09:30"),
                                              "end": end.strftime("%Y-%m-%d 16:00"),
                                              "session_filter": "open"})
        data = ((d or {}).get("series") or {}).get("data") or []
        if isinstance(data, dict):
            data = [data]
        out = []
        for b in data:
            ts = dt.datetime.fromisoformat(b["time"]).replace(tzinfo=ET)
            if dt.time(9, 30) <= ts.time() < dt.time(16, 0):
                out.append(dict(t=ts, o=b["open"], h=b["high"], l=b["low"], c=b["close"], v=b.get("volume") or 0))
        if not out:
            raise DataError("Tradier returned no price data")
        return out

    def chain_rows(self):
        ex = self._get("/markets/options/expirations", {"symbol": SYMBOL})
        dates = ((ex or {}).get("expirations") or {}).get("date") or []
        if isinstance(dates, str):
            dates = [dates]
        rows = []
        for e in dates[:N_EXPIRIES]:
            ch = self._get("/markets/options/chains", {"symbol": SYMBOL, "expiration": e, "greeks": "true"})
            opts = ((ch or {}).get("options") or {}).get("option") or []
            if isinstance(opts, dict):
                opts = [opts]
            for o in opts:
                g = o.get("greeks") or {}
                rows.append({"exp": e, "k": o["strike"], "type": o["option_type"],
                             "oi": o.get("open_interest") or 0, "iv": g.get("mid_iv") or g.get("smv_vol") or 0})
        if not rows:
            raise DataError("Tradier returned no option chain")
        return rows


def make_provider():
    p = os.environ.get("DATA_PROVIDER", "yahoo").lower()
    return TradierProvider() if p == "tradier" else YahooProvider()


# ====================================================================== math
def gamma(S, K, T, iv):
    if iv <= 0.01 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + iv * iv / 2 * T) / (iv * math.sqrt(T))
    return PDF(d1) / (S * iv * math.sqrt(T))


def build_legs(rows, S, now):
    legs = []
    for r in rows:
        K, oi, iv = r["k"], r["oi"], r["iv"]
        if oi <= 0 or iv <= 0.01 or abs(K / S - 1) > 0.08:
            continue
        exp_day = dt.date.fromisoformat(r["exp"])
        exp = dt.datetime.combine(exp_day, dt.time(16, 0), ET)
        T = max((exp - now).total_seconds() / (365 * 86400), 1 / (365 * 6.5 * 4))
        legs.append((K, 1 if r["type"] == "call" else -1, oi, iv, T, r["exp"]))
    return legs


def gex_at(legs, x):
    """Net dealer gamma in $ per 1% move, assuming dealers are long calls and short puts."""
    return sum(sg * oi * 100 * gamma(x, K, T, iv) * x * x * 0.01 for K, sg, oi, iv, T, _ in legs)


# ====================================================================== compute
def compute(bars, chain_rows, provider_name, now=None):
    now = now or dt.datetime.now(ET)
    if not bars:
        raise DataError("No price bars")
    days = sorted({b["t"].date() for b in bars})
    sess_day = days[-1]
    S_bars = [b for b in bars if b["t"].date() == sess_day]
    P_bars = [b for b in bars if b["t"].date() == days[-2]] if len(days) > 1 else []
    price = S_bars[-1]["c"]

    cv = cp = 0.0
    series = []
    for b in S_bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        cp += tp * b["v"]
        cv += b["v"]
        series.append({"t": b["t"].strftime("%H:%M"), "c": round(b["c"], 2), "v": b["v"],
                       "w": round(cp / cv, 2) if cv else None})
    vwap = cp / cv if cv else price

    orb = S_bars[:30]
    or_high = max(b["h"] for b in orb)
    or_low = min(b["l"] for b in orb)
    day_high = max(b["h"] for b in S_bars)
    day_low = min(b["l"] for b in S_bars)
    prev = None
    if P_bars:
        prev = {"high": max(b["h"] for b in P_bars), "low": min(b["l"] for b in P_bars), "close": P_bars[-1]["c"]}

    vols = [b["v"] for b in S_bars]
    vol_ratio = (sum(vols[-5:]) / 5) / (sum(vols) / len(vols)) if len(vols) > 10 and sum(vols) else None

    in_session = now.date() == sess_day and now.time() < dt.time(16)
    ref_now = now if in_session else dt.datetime.combine(sess_day, dt.time(16, 0), ET)
    legs = build_legs(chain_rows or [], price, ref_now)

    strikes = sorted({l[0] for l in legs if abs(l[0] / price - 1) <= 0.03 and float(l[0]).is_integer()})
    by_k = {}
    for K, sg, oi, iv, T, _ in legs:
        if K not in strikes:
            continue
        g = sg * oi * 100 * gamma(price, K, T, iv) * price * price * 0.01
        e = by_k.setdefault(K, {"k": K, "net": 0.0, "call": 0.0, "put": 0.0, "oi": 0})
        e["net"] += g
        e["oi"] += oi
        e["call" if sg > 0 else "put"] += g
    gex = [{"k": e["k"], "net": round(e["net"] / 1e6, 1), "call": round(e["call"] / 1e6, 1),
            "put": round(e["put"] / 1e6, 1), "oi": e["oi"]} for e in (by_k[k] for k in strikes)]

    flip = None
    total_gex = None
    if legs:
        grid = [price * (1 + i / 1000) for i in range(-40, 41)]
        vals = [gex_at(legs, x) for x in grid]
        for (x1, v1), (x2, v2) in zip(zip(grid, vals), zip(grid[1:], vals[1:])):
            if v1 == 0 or v1 * v2 < 0:
                xc = x1 + (x2 - x1) * (abs(v1) / (abs(v1) + abs(v2)))
                if flip is None or abs(xc - price) < abs(flip - price):
                    flip = xc
        total_gex = gex_at(legs, price) / 1e6

    above = [g for g in gex if g["k"] > price]
    below = [g for g in gex if g["k"] < price]
    call_walls = sorted(above, key=lambda g: -g["call"])[:3]
    put_walls = sorted(below, key=lambda g: g["put"])[:3]

    L = []

    def add(name, px, kind, note):
        if px is not None:
            L.append({"name": name, "px": round(px, 2), "kind": kind, "note": note})

    add("VWAP", vwap, "anchor", "Session volume-weighted average price")
    add("Opening range high", or_high, "structure", "High of the first 30 minutes")
    add("Opening range low", or_low, "structure", "Low of the first 30 minutes")
    add("Day high", day_high, "structure", "Session high so far")
    add("Day low", day_low, "structure", "Session low so far")
    if prev:
        add("Prior day high", prev["high"], "structure", "Previous session high")
        add("Prior day low", prev["low"], "structure", "Previous session low")
        add("Prior close", prev["close"], "structure", "Previous session close")
    if flip:
        add("Gamma flip", flip, "flip", "Below this, dealer hedging speeds moves up")
    for g in call_walls:
        add(f"Call wall {g['k']:g}", g["k"], "resistance", f"{g['call']:+.0f}M gamma, {g['oi']:,} OI")
    for g in put_walls:
        add(f"Put wall {g['k']:g}", g["k"], "support", f"{g['put']:+.0f}M gamma, {g['oi']:,} OI")
    L.sort(key=lambda x: -x["px"])
    for x in L:
        x["dist"] = round(x["px"] - price, 2)

    ups = sorted([x for x in L if x["px"] > price + 0.02], key=lambda x: x["px"])
    dns = sorted([x for x in L if x["px"] < price - 0.02], key=lambda x: -x["px"])
    r1, r2 = (ups + [None, None])[:2]
    s1, s2 = (dns + [None, None])[:2]

    positive = flip is None or price > flip
    regime = {
        "positive": positive,
        "label": "Range day" if positive else "Trend day",
        "detail": ("Price is above the gamma flip. Dealers sell rallies and buy dips, so moves tend to stall at walls. Favor fading the edges."
                   if positive else
                   "Price is below the gamma flip. Dealer hedging adds to moves, so breaks tend to run. Favor following breaks with volume."),
    }

    setups = []
    if positive:
        if s1 and r1:
            setups.append({"kind": "fade", "side": "long", "title": f"Buy the dip at {s1['name']}",
                           "entry": s1["px"], "stop": round(s1["px"] - STOP, 2), "target": r1["px"],
                           "trigger": f"SPY pulls back to {s1['px']:.2f} and holds for 1-2 minutes"})
            setups.append({"kind": "fade", "side": "short", "title": f"Sell the rip at {r1['name']}",
                           "entry": r1["px"], "stop": round(r1["px"] + STOP, 2), "target": s1["px"],
                           "trigger": f"SPY tags {r1['px']:.2f} and stalls"})
        if r1:
            setups.append({"kind": "break", "side": "long", "title": f"Breakout above {r1['name']} (lower odds in a range day)",
                           "entry": round(r1["px"] + 0.05, 2), "stop": round(r1["px"] - STOP, 2),
                           "target": r2["px"] if r2 else round(r1["px"] + 1.5, 2),
                           "trigger": "A 1-minute close above the level on at least 2x average volume"})
    else:
        if s1:
            setups.append({"kind": "break", "side": "short", "title": f"Breakdown below {s1['name']}",
                           "entry": round(s1["px"] - 0.05, 2), "stop": round(s1["px"] + STOP, 2),
                           "target": s2["px"] if s2 else round(s1["px"] - 1.5, 2),
                           "trigger": "A 1-minute close below the level on at least 2x average volume"})
        if r1:
            setups.append({"kind": "break", "side": "long", "title": f"Reclaim of {r1['name']}",
                           "entry": round(r1["px"] + 0.05, 2), "stop": round(r1["px"] - STOP, 2),
                           "target": r2["px"] if r2 else round(r1["px"] + 1.5, 2),
                           "trigger": "A 1-minute close back above the level on at least 2x average volume"})
    for s in setups:
        risk = abs(s["entry"] - s["stop"])
        reward = abs(s["target"] - s["entry"])
        s["risk"] = round(risk, 2)
        s["reward"] = round(reward, 2)
        s["rr"] = round(reward / risk, 1) if risk else None

    nearest = min(L, key=lambda x: abs(x["dist"])) if L else None
    alert = None
    if nearest and abs(nearest["dist"]) <= ALERT_DISTANCE:
        alert = {"level": nearest["name"], "px": nearest["px"], "dist": nearest["dist"],
                 "msg": f"SPY is {abs(nearest['dist']):.2f} from {nearest['name']} ({nearest['px']:.2f})"}

    market_open = is_market_hours(now) and now.date() == sess_day
    return {
        "generatedAt": now.isoformat(timespec="seconds"),
        "session": sess_day.isoformat(),
        "marketOpen": market_open,
        "price": round(price, 2),
        "lastBar": S_bars[-1]["t"].strftime("%H:%M"),
        "change": round(price - prev["close"], 2) if prev else None,
        "changePct": round((price / prev["close"] - 1) * 100, 2) if prev else None,
        "vwap": round(vwap, 2),
        "flip": round(flip, 2) if flip else None,
        "totalGex": round(total_gex, 0) if total_gex is not None else None,
        "volRatio": round(vol_ratio, 2) if vol_ratio else None,
        "regime": regime,
        "r1": r1, "s1": s1,
        "alert": alert,
        "setups": setups,
        "levels": L,
        "gex": gex,
        "series": series,
        "expiries": sorted({l[5] for l in legs}),
        "source": f"{provider_name} 1-minute bars and option chains. Gamma is an estimate that assumes dealers are long calls and short puts.",
    }


def is_market_hours(now=None):
    now = now or dt.datetime.now(ET)
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 0)


if __name__ == "__main__":
    p = make_provider()
    doc = compute(p.bars(), p.chain_rows(), p.name)
    path = sys.argv[1] if len(sys.argv) > 1 else "spy_levels.json"
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print(f"ok {doc['session']} price {doc['price']} flip {doc['flip']} {doc['regime']['label']} -> {path}")
