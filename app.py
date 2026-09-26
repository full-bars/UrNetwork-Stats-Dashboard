# ----------------------------------------
# Imports & Environment Setup
# ----------------------------------------
import os
import socket
import time
import datetime
import requests
from dotenv import load_dotenv, dotenv_values
from flask import (
    Flask, request, render_template_string,
    redirect, url_for, flash, current_app, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from flask_apscheduler import APScheduler

# Load .env variables into environment
load_dotenv()

API_BASE = "https://api.bringyour.com"
UR_USER  = os.getenv("UR_USER")
UR_PASS  = os.getenv("UR_PASS")
UR_JWT   = "UR_JWT"

# ----------------------------------------
# Flask & Extensions Configuration
# ----------------------------------------
class Config:
    SCHEDULER_API_ENABLED         = True
    SQLALCHEMY_DATABASE_URI       = "sqlite:////home/user/UrNetwork-Stats-Dashboard/transfer_stats.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY                    = os.urandom(24)

app       = Flask(__name__)
app.config.from_object(Config)

# Initialize database and scheduler
db        = SQLAlchemy(app)
scheduler = APScheduler()
scheduler.init_app(app)

# ----------------------------------------
# Database Model
# ----------------------------------------
class Stats(db.Model):
    """
    Represents a snapshot of paid vs unpaid bytes at a given timestamp.
    Columns:
      - id           : Primary key
      - timestamp    : Auto-generated timestamp of the record
      - paid_bytes   : Total paid bytes provided
      - paid_gb      : Paid bytes converted to gigabytes
      - unpaid_bytes : Total unpaid bytes provided
      - unpaid_gb    : Unpaid bytes converted to gigabytes
    """
    id           = db.Column(db.Integer,   primary_key=True)
    timestamp    = db.Column(db.DateTime,  server_default=db.func.now())
    # Nullable so a quarter-hour that the upstream API could not serve is still
    # recorded as a visible gap instead of vanishing from the grid.
    paid_bytes   = db.Column(db.BigInteger, nullable=True)
    paid_gb      = db.Column(db.Float,      nullable=True)
    unpaid_bytes = db.Column(db.BigInteger, nullable=True)
    unpaid_gb    = db.Column(db.Float,      nullable=True)
    # Set when this row is a placeholder: holds the failure reason, e.g.
    # "HTTP 500". Cleared once a retry succeeds and the real values land.
    fetch_error  = db.Column(db.String(200), nullable=True)

# ----------------------------------------
# Environment Token Management
# ----------------------------------------
def save_env_token(token: str):
    vals = dotenv_values(".env")
    vals["UR_JWT"] = token
    with open(".env", "w") as f:
        for k, v in vals.items():
            f.write(f"{k}={v}\n")


# ----------------------------------------
# HTTP Helper with Retries
# ----------------------------------------
def request_with_retry(
    method: str,
    url: str,
    retries: int = 3,
    backoff: int = 30,
    timeout: int = 60,
    **kwargs
):
    """
    Issue an HTTP request and retry on failure.
    - method : HTTP verb (get, post, etc.)
    - url    : Endpoint to hit
    - retries: Number of retry attempts
    - backoff: Seconds to wait before retry
    - timeout: Request timeout in seconds
    Raises:
      - RuntimeError if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            current_app.logger.warning(
                f"[{method.upper()} {url}] attempt {attempt}/{retries} failed: {e}"
            )
            if attempt < retries:
                time.sleep(backoff)
    raise RuntimeError(f"All {retries} attempts to {method.upper()} {url} failed: {last_exc}")

# ----------------------------------------
# Authentication & JWT Handling
# ----------------------------------------
def login_check():
    """
    Ensure we have a valid JWT token. If stored token is invalid or missing,
    log in with username and password to obtain a fresh token.
    Returns:
      - A valid JWT token string.
    Raises:
      - RuntimeError if login fails.
    """
    token = os.getenv("UR_JWT")
    if token:
        try:
            resp = request_with_retry(
                "get",
                f"{API_BASE}/transfer/stats",
                headers={"Authorization": f"Bearer {token}", "Accept": "*/*"}
            )
            body = resp.json()
            if "not authorized" not in str(body.get("message","")).lower():
                return token
        except Exception:
            current_app.logger.info("Re-acquiring JWT (stored one invalid or stats check failed)")

    resp = request_with_retry(
        "post",
        f"{API_BASE}/auth/login-with-password",
        headers={"Content-Type": "application/json"},
        json={"user_auth": UR_USER, "password": UR_PASS},
    )
    data = resp.json()
    token = data.get("network", {}).get("by_jwt")
    if not token:
        err = data.get("message") or data.get("error") or str(data)
        raise RuntimeError(f"Login failed: {err}")
    save_env_token(token)
    return token

# ----------------------------------------
# Transfer Statistics Fetching
# ----------------------------------------
# Retry budget for one quarter-hour tick. The scheduler fires every 15 minutes,
# so retries must finish well inside that or they would delay the next window.
# IN_FLIGHT marks a row whose fetch is still retrying. Such rows are hidden from
# the table: the window has not landed yet, so it is not a gap. If the retries
# are exhausted the row keeps a real reason (e.g. "HTTP 500") and is shown.
IN_FLIGHT = "__pending__"
# Rows written by the first version of this feature used a bare "pending"
# marker; treat those as in-flight too so they never render as a real gap.
IN_FLIGHT_LEGACY = "pending"
IN_FLIGHT_MARKERS = (IN_FLIGHT, IN_FLIGHT_LEGACY)


def _is_in_flight(row_error):
    return row_error is None or row_error in IN_FLIGHT_MARKERS


RETRY_BUDGET_SECONDS = 180
RETRY_BACKOFFS      = (5, 15, 45, 90)   # cumulative: 5, 20, 65, 155s (< 180)

def fetch_transfer_stats(jwt_token: str):
    """
    Retrieve the latest transfer statistics using the provided JWT.
    Retries transient upstream failures on a time-boxed schedule so a brief
    500 window does not cost us the whole reading.

    Returns:
      - A dict with keys: paid_bytes, paid_gb, unpaid_bytes, unpaid_gb.
    Raises:
      - RuntimeError carrying a short, display-friendly reason (e.g. "HTTP 500")
        when the budget is exhausted.
    """
    headers = {"Authorization": f"Bearer {jwt_token}", "Accept": "*/*"}
    url = f"{API_BASE}/transfer/stats"
    last_reason = "unknown error"
    started = time.monotonic()
    payload = None

    for delay in RETRY_BACKOFFS + (None,):
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            last_reason = f"HTTP {code}" if code else f"HTTP error: {e}"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {e}"

        if delay is None:
            break
        if time.monotonic() - started + delay > RETRY_BUDGET_SECONDS:
            current_app.logger.warning(
                f"transfer/stats failed ({last_reason}); out of retry budget")
            break
        current_app.logger.warning(
            f"transfer/stats failed ({last_reason}); retrying in {delay}s")
        time.sleep(delay)

    if payload is None:
        raise RuntimeError(last_reason)

    d = payload

    paid   = d.get("paid_bytes_provided",   0)
    unpaid = d.get("unpaid_bytes_provided", 0)
    return {
        "paid_bytes":   paid,
        "paid_gb":      paid   / 1e9,
        "unpaid_bytes": unpaid,
        "unpaid_gb":    unpaid / 1e9
    }

# ----------------------------------------
# Scheduler Utilities
# ----------------------------------------
def get_next_quarter(dt=None):
    """
    Compute the next 15-minute boundary from now (or provided dt).
    Returns:
      - A datetime object aligned to the next quarter-hour.
    """
    dt = dt or datetime.datetime.now()
    q  = (dt.minute // 15 + 1) * 15
    if q == 60:
        return (dt.replace(minute=0, second=0, microsecond=0)
                + datetime.timedelta(hours=1))
    return dt.replace(minute=0, second=0, microsecond=0) \
           + datetime.timedelta(minutes=q)

# ----------------------------------------
# Scheduled Task: Periodic Logging
# ----------------------------------------
@scheduler.task(id="log_stats", trigger="cron", minute="0,15,30,45")
def log_stats():
    """
    Runs every quarter-hour.

    The row is written the moment the tick fires, as a placeholder carrying the
    fetch error, so a window the upstream API could not serve still shows up on
    the grid as a visible gap rather than silently vanishing. If the fetch (and
    its time-boxed retries) succeeds, that same row is updated in place with the
    real values and the error is cleared.
    """
    with app.app_context():
        entry = Stats(fetch_error=IN_FLIGHT)
        db.session.add(entry)
        db.session.commit()
        current_app.logger.info(f"Placeholder written @ {entry.timestamp}")

        try:
            token = login_check()
            stats = fetch_transfer_stats(token)
            entry.paid_bytes   = stats["paid_bytes"]
            entry.paid_gb      = stats["paid_gb"]
            entry.unpaid_bytes = stats["unpaid_bytes"]
            entry.unpaid_gb    = stats["unpaid_gb"]
            entry.fetch_error  = None
            db.session.commit()
            current_app.logger.info(f"Logged @ {entry.timestamp}")
        except Exception as e:
            reason = str(e)
            # Keep it short so it fits the column.
            entry.fetch_error = reason[:200]
            db.session.commit()
            current_app.logger.error(f"log_stats recorded a gap @ {entry.timestamp}: {reason}")

# ----------------------------------------
# HTML Template
# ----------------------------------------
TEMPLATE = """
<!doctype html>
<html lang="en" data-bs-theme="{{ 'dark' if dark else 'light' }}">
<head>
  <meta charset="utf-8">
  <title>Transfer Stats</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css"
        rel="stylesheet">
</head>
<body>

<!-- DARK/LIGHT HEADER -->
<div class="w-100 bg-dark text-white py-2">
  <div class="container-fluid d-flex justify-content-between">
    <div>Next fetch: {{ next_fetch_str }}</div>
    <div>Countdown: <span id="countdown">--:--:--</span></div>
  </div>
</div>

<div class="container-fluid-wide py-4">

  <!-- TITLE + TOGGLE + BUTTONS -->
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h1 class="mb-0">Transfer Stats History</h1>
    <div class="d-flex align-items-center">
      <!-- Dark/Light Toggle -->
      <div class="form-check form-switch me-3">
        <input class="form-check-input" type="checkbox" id="darkSwitch"
               {{ 'checked' if dark else '' }}>
        <label class="form-check-label {{ 'text-white' if dark else 'text-dark' }}"
               for="darkSwitch">
          Dark Mode
        </label>
      </div>

      <!-- Fetch Now -->
      <form method="post" action="{{ url_for('trigger_fetch') }}?dark={{ '1' if dark else '0' }}"
            class="me-2">
        <button type="submit" class="btn btn-primary">Fetch Now</button>
      </form>

      <!-- Clear DB -->
      <form method="post" action="{{ url_for('clear_db') }}?dark={{ '1' if dark else '0' }}"
            onsubmit="return confirm('Are you sure you want to clear all records?');">
        <button type="submit" class="btn btn-danger">Clear DB</button>
      </form>
    </div>
  </div>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div class="alert alert-warning">{{ msgs[0] }}</div>
    {% endif %}
  {% endwith %}

  <style>
    /* Fluid page width: fills a wide monitor, stays comfortable on a phone.
       Bootstrap's .container is fixed-width (leaves dead space on large
       screens), so the main wrapper opts out of it. */
    .container-fluid-wide {
      width: 100%;
      max-width: 100%;
      margin-left: 0;
      margin-right: 0;
      padding-left: 16px;
      padding-right: 16px;
    }
    /* Keep the table readable instead of letting columns crush on phones. */
    @media (max-width: 768px) {
      .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
      .table-scroll table { min-width: 640px; }
    }
    /* Change (GB): continuous heat gradient by magnitude, either direction.
       >=1 GB per 15 min is the operator's "baseline good" line, so the ramp
       jumps straight from amber to green there - yellow is never used at
       1 GB or above. The 1-10 GB band is the common working range, so it gets
       many distinct steps (green -> mint -> emerald -> cyan -> blue) rather
       than one flat colour. Purple ends before 20; 20 GB and up is hot pink. */
    .heat-0  { color: #e5e7eb !important; }
    /* A quarter-hour the upstream API could not serve. The row is still there,
       dimmed and flagged, so a server-side gap is distinguishable from a
       genuine zero. */
    .gap-row td { opacity: .55; }
    .gap-flag {
      display: inline-block; padding: 1px 7px; border-radius: 999px;
      font-size: 11px; font-weight: 700; letter-spacing: .02em;
      background: rgba(239, 68, 68, .16); color: #fca5a5;
      border: 1px solid rgba(239, 68, 68, .45); white-space: nowrap;
      opacity: 1;
    }
    [data-bs-theme="light"] .gap-flag { color: #991b1b; background: rgba(220, 38, 38, .10); border-color: rgba(185, 28, 28, .40); }
    .gap-dash { color: #6b7280; }
    /* A window whose fetch is still retrying: neutral, not an error. */
    .gap-pending {
      display: inline-block; padding: 1px 7px; border-radius: 999px;
      font-size: 11px; font-weight: 600; letter-spacing: .02em;
      background: rgba(148, 163, 184, .16); color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, .40); white-space: nowrap;
    }
    [data-bs-theme="light"] .gap-pending { color: #475569; background: rgba(100,116,139,.12); border-color: rgba(71,85,105,.35); }
    .heat-1  { color: #cb2929 !important; }
    .heat-2  { color: #dd3737 !important; }
    .heat-3  { color: #ef4444 !important; }
    .heat-4  { color: #f25435 !important; }
    .heat-5  { color: #f66325 !important; }
    .heat-6  { color: #f97316 !important; }
    .heat-7  { color: #fa8c1b !important; }
    .heat-8  { color: #faa61f !important; }
    .heat-9  { color: #fbbf24 !important; }
    .heat-10 { color: #b3c137 !important; }
    .heat-11 { color: #6ac34b !important; }
    .heat-12 { color: #22c55e !important; }
    .heat-13 { color: #2fcd69 !important; }
    .heat-14 { color: #3dd675 !important; }
    .heat-15 { color: #4ade80 !important; }
    .heat-16 { color: #31e08b !important; }
    .heat-17 { color: #19e395 !important; }
    .heat-18 { color: #00e5a0 !important; }
    .heat-19 { color: #0bdfba !important; }
    .heat-20 { color: #17d9d4 !important; }
    .heat-21 { color: #22d3ee !important; }
    .heat-22 { color: #29ccf1 !important; }
    .heat-23 { color: #31c4f5 !important; }
    .heat-24 { color: #38bdf8 !important; }
    .heat-25 { color: #50adf8 !important; }
    .heat-26 { color: #699cf8 !important; }
    .heat-27 { color: #818cf8 !important; }
    .heat-28 { color: #a783e2 !important; }
    .heat-29 { color: #ce7bcc !important; }
    .heat-30 { color: #f472b6 !important; }

    /* Soft "breathe" for the low end (under 1 GB). Opacity only, so it is
       cheap and never fights the text-shadow pulse used above 1 GB. Keeps the
       quiet rows alive without making them loud. */
    @keyframes heat-breathe {
      0%, 100% { opacity: 1; }
      50%      { opacity: .45; }
    }
    .heat-breathe { animation: heat-breathe 3.2s ease-in-out infinite; }

    /* Pulse for the good range (>=1 GB). Only text-shadow animates, so it
       never triggers layout or a full repaint - cheap even with many rows.
       currentColor keeps each tier pulsing in its own colour. Speed increases
       with the value; the fastest tier also gets a wider glow. */
    @keyframes heat-pulse {
      0%, 100% { text-shadow: 0 0 5px currentColor, 0 0 12px currentColor; }
      50%      { text-shadow: 0 0 1px currentColor, 0 0 3px  currentColor; }
    }
    @keyframes heat-pulse-hard {
      0%, 100% { text-shadow: 0 0 7px currentColor, 0 0 18px currentColor, 0 0 30px currentColor; }
      50%      { text-shadow: 0 0 2px currentColor, 0 0 6px  currentColor; }
    }
    .heat-pulse      { animation: heat-pulse var(--pulse-dur, 2.4s) ease-in-out infinite; }
    .heat-pulse-hard { animation: heat-pulse-hard 0.6s ease-in-out infinite; }
    /* Respect the OS "reduce motion" accessibility setting. */
    @media (prefers-reduced-motion: reduce) {
      .heat-pulse, .heat-pulse-hard, .heat-breathe { animation: none; }
    }

    /* Light mode needs its own ramp: the dark-mode tier colours are tuned for a
       dark background and most fall below readable contrast on white (notably
       the whole green/cyan band, and heat-0 which would be white-on-white).
       Same hues, darkened to clear 4.5:1 on white. */
    [data-bs-theme="light"] .heat-0  { color: #374151 !important; }
    [data-bs-theme="light"] .heat-1  { color: #cb2929 !important; }
    [data-bs-theme="light"] .heat-2  { color: #cb3333 !important; }
    [data-bs-theme="light"] .heat-3  { color: #ca3a3a !important; }
    [data-bs-theme="light"] .heat-4  { color: #cd472d !important; }
    [data-bs-theme="light"] .heat-5  { color: #bf4d1d !important; }
    [data-bs-theme="light"] .heat-6  { color: #b25310 !important; }
    [data-bs-theme="light"] .heat-7  { color: #a55c11 !important; }
    [data-bs-theme="light"] .heat-8  { color: #986513 !important; }
    [data-bs-theme="light"] .heat-9  { color: #8d6b14 !important; }
    [data-bs-theme="light"] .heat-10 { color: #6d7622 !important; }
    [data-bs-theme="light"] .heat-11 { color: #468131 !important; }
    [data-bs-theme="light"] .heat-12 { color: #17833e !important; }
    [data-bs-theme="light"] .heat-13 { color: #1f8745 !important; }
    [data-bs-theme="light"] .heat-14 { color: #258347 !important; }
    [data-bs-theme="light"] .heat-15 { color: #2d864e !important; }
    [data-bs-theme="light"] .heat-16 { color: #1b7d4e !important; }
    [data-bs-theme="light"] .heat-17 { color: #0e7f53 !important; }
    [data-bs-theme="light"] .heat-18 { color: #008059 !important; }
    [data-bs-theme="light"] .heat-19 { color: #067c66 !important; }
    [data-bs-theme="light"] .heat-20 { color: #0e8481 !important; }
    [data-bs-theme="light"] .heat-21 { color: #158090 !important; }
    [data-bs-theme="light"] .heat-22 { color: #197b92 !important; }
    [data-bs-theme="light"] .heat-23 { color: #1d7894 !important; }
    [data-bs-theme="light"] .heat-24 { color: #257ca4 !important; }
    [data-bs-theme="light"] .heat-25 { color: #3a7bb2 !important; }
    [data-bs-theme="light"] .heat-26 { color: #4b6fb2 !important; }
    [data-bs-theme="light"] .heat-27 { color: #646dc1 !important; }
    [data-bs-theme="light"] .heat-28 { color: #8366b0 !important; }
    [data-bs-theme="light"] .heat-29 { color: #945892 !important; }
    [data-bs-theme="light"] .heat-30 { color: #af5283 !important; }
  </style>

  <!-- TABLE (newest first) -->
  <div class="table-scroll">
  <table class="table table-striped">
    <thead>
      <tr>
        <th title="Time the data was recorded">🗓️ Timestamp</th>
        <th title="Amount of paid traffic in gigabytes">💵 Paid (GB)</th>
        <th title="Total unpaid data in bytes">📦 Unpaid Bytes</th>
        <th title="Change from previous unpaid bytes">➕ Change Bytes</th>
        <th title="Total unpaid data in gigabytes">📦 Unpaid (GB)</th>
        <th title="Change from previous unpaid GB">➕ Change (GB)</th>
      </tr>
    </thead>
    <tbody id="statsBody">
      {% for row in rows %}
      <tr{% if row.fetch_error %} class="gap-row"{% endif %}>
        <td>{{ row.ts_str }}</td>
        <td>{% if row.paid_gb is not none %}{{ "%.3f"|format(row.paid_gb) }}{% else %}<span class="gap-dash">&mdash;</span>{% endif %}</td>
        <td>{% if row.unpaid_bytes is not none %}{{ "{:,}".format(row.unpaid_bytes) }}{% else %}<span class="gap-dash">&mdash;</span>{% endif %}</td>
        <td{% if row.fetch_error %} class="gap-flag-cell"{% endif %}>
          {% if row.fetch_error in (IN_FLIGHT, 'pending') %}
            <span class="gap-pending">fetching&hellip;</span>
          {% elif row.fetch_error %}
            <span class="gap-flag">{{ row.fetch_error }}</span>
          {% elif row.delta_bytes is not none %}
            {{ "{:,}".format(row.delta_bytes) }}
          {% else %}N/A{% endif %}
        </td>
        <td>{% if row.unpaid_gb is not none %}{{ "%.3f"|format(row.unpaid_gb) }}{% else %}<span class="gap-dash">&mdash;</span>{% endif %}</td>
        <td class="{{ '' if row.fetch_error else heat_class(row.delta_gb) }}" style="{{ '' if row.fetch_error else pulse_style(row.delta_gb) }}">
          {% if row.fetch_error %}
            <span class="gap-dash">&mdash;</span>
          {% elif row.delta_gb is not none %}
            {{ "%.3f"|format(row.delta_gb) }}
          {% else %}N/A{% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  </div>

  <!-- INFINITE SCROLL: sentinel below the table; observed to pull the next batch -->
  <div id="scrollSentinel" style="height:1px"></div>
  <div id="scrollStatus" class="text-center text-muted py-3" style="font-size:.9rem">
    {% if has_more %}Loading more&hellip;{% else %}End of history &mdash; {{ rows|length }} entries shown{% endif %}
  </div>
</div>

<!-- FOOTER: names the host serving this page -->
<div class="container-fluid-wide" style="padding:0 16px 28px">
  <div style="border-top:1px solid #374151; padding-top:12px; color:#4b5563; font-size:11px; line-height:1.6">
    <span id="hostFooter">host</span>
  </div>
</div>

<!-- TOGGLE + COUNTDOWN SCRIPTS -->
<script>
  // Footer: which box is serving this page.
  fetch('/api/host').then(r => r.json()).then(d => {
    const el = document.getElementById('hostFooter');
    if (el) el.textContent = d.hostname + ' \u00b7 ' + d.app;
  }).catch(() => {});

  // Theme toggle: flips ?dark= and reloads
  document.getElementById('darkSwitch').addEventListener('change', function(){
    const darkVal = this.checked ? '1' : '0';
    const url    = new URL(window.location.href);
    url.searchParams.set('dark', darkVal);
    window.location.href = url.toString();
  });

  // Countdown + auto-refresh (no POST)
  const target = new Date({{ next_fetch_ts }});
  let autoDone = false;
  function updateTimer(){
    const now  = new Date(),
          diff = target - now,
          span = document.getElementById('countdown');
    if(diff <= 0){
      span.innerHTML =
        '<span class="spinner-border spinner-border-sm text-white" role="status"></span> Refreshing...';
      if(!autoDone){
        autoDone = true;
        window.location.reload();
      }
      return;
    }
    const h = String(Math.floor(diff/3600000 )).padStart(2,'0'),
          m = String(Math.floor((diff%3600000)/60000)).padStart(2,'0'),
          s = String(Math.floor((diff%60000)/1000 )).padStart(2,'0');
    span.textContent = `${h}:${m}:${s}`;
  }
  updateTimer();
  setInterval(updateTimer, 1000);

  // ---- Infinite scroll -------------------------------------------------
  // Only the newest batch is rendered server-side; older rows are appended
  // here as the user scrolls. No paging controls, and opening the page stays
  // fast regardless of how much history exists.
  (function(){
    const body    = document.getElementById('statsBody');
    const sentinel= document.getElementById('scrollSentinel');
    const status  = document.getElementById('scrollStatus');
    const darkOn  = {{ 'true' if dark else 'false' }};
    const limit   = {{ scroll_page_size }};
    const IN_FLIGHT = {{ in_flight | tojson }};
    const HEAT_BOUNDS = {{ heat_bounds | tojson }};
    const PULSE_FROM  = {{ pulse_from }};
    const BREATHE_BELOW = {{ breathe_below_gb }};
    const STATIC_BELOW  = {{ static_below }};

    // Under 1 GB: gentle breathe. From 1 GB up: a text-shadow pulse that speeds
    // up as the value climbs, switching to the wider-glow variant once quick.
    // text-shadow/opacity only, so no layout or repaint cost. Mirrors
    // pulse_class()/BREATHE_BELOW/PULSE_FROM in app.py.
    // The pulse starts at the 1 GB "baseline good" line and speeds up from
    // there: 1.40s at 1 GB, 0.71s at 3 GB, 0.34s at 10 GB, 0.22s at 20 GB,
    // floored at 0.13s. Mirrors pulse_duration() in app.py.
    function pulseDuration(gb){
      const g = Math.max(Math.abs(gb || 0), 1.0);
      return Math.max(0.13, Math.min(1.4, 1.4 * Math.pow(1.0 / g, 0.62)));
    }

    function applyPulse(td, tier, gb){
      // Under 0.1 GB the change is noise: plain white, static. From there to
      // 1 GB a gentle breathe, then the text-shadow pulse above that.
      const mag = Math.abs(gb || 0);
      if (mag < STATIC_BELOW) return;          // noise: static white
      if (mag < BREATHE_BELOW){
        td.classList.add('heat-breathe');
        return;
      }
      if (tier < PULSE_FROM) return;
      const dur = pulseDuration(gb);
      td.style.setProperty('--pulse-dur', dur.toFixed(2) + 's');
      if (dur <= 0.40) td.classList.add('heat-pulse-hard');
      else           td.classList.add('heat-pulse');
    }

    let cursor  = {{ (next_cursor | tojson) }};
    let loading = false;
    let done    = {{ 'false' if has_more else 'true' }};

    const num  = (v, d) => (v === null || v === undefined) ? d : v;
    const fmtN = (v) => (v === null || v === undefined)
      ? 'N/A' : Number(v).toLocaleString('en-US');
    const fmtF = (v) => (v === null || v === undefined)
      ? 'N/A' : Number(v).toFixed(3);

    function buildRow(r){
      const tr = document.createElement('tr');
      const gap = r.fetch_error || null;
      const cells = [r.ts_str,
                     gap ? '\u2014' : fmtF(r.paid_gb),
                     gap ? '\u2014' : fmtN(r.unpaid_bytes),
                     gap ? r.fetch_error : fmtN(r.delta_bytes),
                     gap ? '\u2014' : fmtF(r.unpaid_gb),
                     gap ? '\u2014' : fmtF(r.delta_gb)];
      cells.forEach((text, i) => {
        const td = document.createElement('td');
        td.textContent = text;
        if (gap){
          if (i === 3){
            td.className = ((r.fetch_error === IN_FLIGHT || r.fetch_error === 'pending')) ? 'gap-pending' : 'gap-flag';
            tr.className = 'gap-row';
          }
          return;   // no heat colour or animation on a gap row
        }
        // Change Bytes: default body color. Change (GB): traffic-light heat
        // map. Thresholds mirror HEAT_THRESHOLDS in app.py.
        if (i === 5){
          const d = r.delta_gb;
          if (d === null || d === undefined || d === 0){ td.className = 'heat-0'; }
          else{
            const mag = Math.abs(d);
            let tier = 0;
            while (tier < HEAT_BOUNDS.length && mag >= HEAT_BOUNDS[tier]) tier++;
            td.className = 'heat-' + tier;
            applyPulse(td, tier, d);
          }
        }
        tr.appendChild(td);
      });
      return tr;
    }

    async function loadMore(){
      if (loading || done || !cursor) return;
      loading = true;
      status.textContent = 'Loading more…';
      try{
        const url = '/api/rows?cursor=' + encodeURIComponent(cursor) +
                    '&limit=' + limit +
                    '&dark=' + (darkOn ? '1' : '0');
        const res  = await fetch(url);
        if(!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        const frag = document.createDocumentFragment();
        (data.rows || []).forEach(r => frag.appendChild(buildRow(r)));
        body.appendChild(frag);

        done    = data.has_more === false || !(data.rows || []).length;
        cursor  = data.cursor || '';
        status.textContent = done
          ? 'End of history — ' + body.children.length + ' entries shown'
          : 'Loading more… (' + body.children.length + ' entries loaded)';
      }catch(err){
        status.textContent = 'Failed to load more rows: ' + err.message;
        done = true;                 // don't hammer a broken endpoint
      }finally{
        loading = false;
      }
    }

    // Pull the next batch when the user gets near the bottom. Deliberately a
    // scroll/resize handler rather than a bare IntersectionObserver: after each
    // append the table grows and the sentinel moves down, and an observer can
    // fail to re-fire (stalling the list) when the user scrolls fast. The
    // handler re-evaluates on every scroll, so it always converges.
    function maybeLoad(){
      if (loading || done || !cursor) return;
      const nearBottom = (window.innerHeight + window.scrollY) >=
                         (document.body.scrollHeight - 600);
      if (nearBottom) loadMore();
    }
    window.addEventListener('scroll', maybeLoad, {passive:true});
    window.addEventListener('resize', maybeLoad, {passive:true});
    // The page can be taller than the viewport on first load (500 rows), in
    // which case there is nothing to scroll — kick off the first batch.
    maybeLoad();
  })();
</script>
</body>
</html>
"""

# ----------------------------------------
# Flask Routes & Views
# ----------------------------------------

# Page sizes for the table. The first paint is deliberately small so the page
# is instant; scrolling then pulls large batches so a user who wants the whole
# history still gets it without paging controls.
FIRST_PAGE_SIZE = 500
SCROLL_PAGE_SIZE = 2500

def _row_payload(e, nxt):
    """Serialize one Stats row. `nxt` is the following (older) entry, used for
    the deltas; None means this is the oldest row and the deltas are unknown."""
    local_tz = datetime.datetime.now().astimezone().tzinfo
    utc_dt   = e.timestamp.replace(tzinfo=datetime.timezone.utc)
    local_dt = utc_dt.astimezone(local_tz)
    if nxt is not None and e.unpaid_bytes is not None and nxt.unpaid_bytes is not None:
        delta_b = e.unpaid_bytes - nxt.unpaid_bytes
        delta_g = e.unpaid_gb    - nxt.unpaid_gb
    else:
        delta_b = delta_g = None
    return {
        "ts_str":     local_dt.strftime("%m/%d/%Y %I:%M:%S %p"),
        "paid_gb":    e.paid_gb,
        "unpaid_bytes": e.unpaid_bytes,
        "unpaid_gb":  e.unpaid_gb,
        "delta_bytes": delta_b,
        "delta_gb":   delta_g,
        "cursor":     e.timestamp.isoformat(),
        "fetch_error": e.fetch_error,
    }

# HEAT_BOUNDS are the inclusive lower edges (GB) of each heat tier. The ramp
# jumps from amber straight to green at 1 GB (the operator's "baseline good"
# line), gives the common 1-10 GB range many distinct steps, and ends the
# purple band before 20 GB where hot pink takes over. 30 bounds = 31 tiers.
HEAT_BOUNDS = (0.0167, 0.05, 0.0834, 0.125, 0.175, 0.225, 0.2916, 0.375,
               0.4584, 0.5834, 0.75, 0.9166, 1.1666, 1.5, 1.8334, 2.3334, 3,
               3.6666, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.8333, 12.5, 14.1667,
               15.8333, 17.5, 19.1667)

def heat_tier(delta_gb):
    """Numeric heat tier for a Change (GB) value, in either direction.
    heat-0 when nothing moved, up to the top tier for the largest windows."""
    if delta_gb is None or delta_gb == 0:
        return 0
    mag = abs(delta_gb)
    tier = 0
    while tier < len(HEAT_BOUNDS) and mag >= HEAT_BOUNDS[tier]:
        tier += 1
    return tier

def heat_class(delta_gb):
    """CSS class for a Change (GB) value, including the pulse animation when
    the window is a standout. Mirrors the JS lookup exactly so scrolled-in
    rows match the server-rendered ones."""
    tier = heat_tier(delta_gb)
    return f"heat-{tier} {pulse_class(delta_gb)}".strip()



# BREATHE_BELOW is the exclusive upper bound (GB) of the calm "breathe" band:
# anything under 1 GB - the operator's "baseline good" line - gently fades
# instead of sitting dead. At or above 1 GB the value pulses, speeding up with
# the number; quick pulses also get the harder, wider-glow animation.
BREATHE_BELOW = 1.0
STATIC_BELOW = 0.1   # below this the change is noise: plain white, no animation
PULSE_FROM = 12
PULSE_FROM = 12

def is_static(delta_gb):
    """True when the change is too small to mean anything (under 0.1 GB).
    Those render as plain white with no animation at all."""
    return delta_gb is not None and abs(delta_gb) < STATIC_BELOW

def breathe_below(delta_gb):
    """True when the value is in the calm band (0.1 GB to 1 GB)."""
    return (delta_gb is not None and not is_static(delta_gb)
            and abs(delta_gb) < BREATHE_BELOW)

def pulse_class(delta_gb):
    """Animation class for a Change (GB) value ('' = no animation).
    Under 1 GB it breathes; from 1 GB it pulses, with the harder wider-glow
    variant once the pulse is quick enough (>=5 GB)."""
    if delta_gb is None or is_static(delta_gb):
        return ""          # nothing worth animating: static white
    if breathe_below(delta_gb):
        return "heat-breathe"
    return "heat-pulse-hard" if pulse_duration(delta_gb) <= 0.40 else "heat-pulse"

def pulse_duration(delta_gb):
    """Pulse period (seconds) for a Change (GB) value. Used from the 1 GB
    "baseline good" line upward, speeding up from there: 1.40s at 1 GB,
    0.71s at 3 GB, 0.34s at 10 GB, 0.22s at 20 GB, floored at 0.13s.
    Mirrors pulseDuration() in the template."""
    gb = max(abs(delta_gb or 0), 1.0)
    return max(0.13, min(1.4, 1.4 * (1.0 / gb) ** 0.62))

def pulse_style(delta_gb):
    """Inline --pulse-dur for the server-rendered cell; '' when not pulsing."""
    if is_static(delta_gb) or heat_tier(delta_gb) < PULSE_FROM:
        return ""
    return f"--pulse-dur: {pulse_duration(delta_gb):.2f}s"

def sign_class(delta):
    """Change Bytes is rendered in the default body color (no sign coloring)."""
    return ""

@app.route("/")
def index():
    """
    Render the stats history page.

    Only the first FIRST_PAGE_SIZE rows are rendered; the rest are pulled in on
    scroll via /api/rows. Rendering every row at once made the browser parse
    tens of thousands of <tr> elements and hold them all in memory.
    """
    dark = request.args.get('dark', '1') == '1'
    # One extra row so the last rendered row still gets its delta.
    # A placeholder whose fetch is still in flight is not a gap yet, so it is
    # hidden: the window simply has not landed. Rows that survived the retry
    # budget carry a real reason and are shown.
    entries = (Stats.query
               .filter(or_(Stats.fetch_error.is_(None),
                           Stats.fetch_error.notin_(IN_FLIGHT_MARKERS)))
               .order_by(Stats.timestamp.desc())
               .limit(FIRST_PAGE_SIZE + 1)
               .all())
    has_more = len(entries) > FIRST_PAGE_SIZE
    if has_more:
        entries = entries[:FIRST_PAGE_SIZE]

    rows = [_row_payload(e, entries[i+1] if i < len(entries) - 1 else None)
            for i, e in enumerate(entries)]

    nxt = get_next_quarter()
    return render_template_string(
        TEMPLATE,
        dark            = dark,
        rows            = rows,
        has_more        = has_more,
        next_cursor     = (entries[-1].timestamp.isoformat() if has_more and entries else ""),
        scroll_page_size= SCROLL_PAGE_SIZE,
        next_fetch_str  = nxt.strftime("%m/%d/%Y %I:%M %p"),
        next_fetch_ts   = int(nxt.timestamp() * 1000),
        # Plain Python functions are not visible to Jinja unless passed in.
        heat_class      = heat_class,
        sign_class      = sign_class,
        pulse_class     = pulse_class,
        pulse_style     = pulse_style,
        heat_bounds     = list(HEAT_BOUNDS),
        in_flight       = IN_FLIGHT,
        pulse_from      = PULSE_FROM,
        breathe_below_gb= BREATHE_BELOW,
        static_below    = STATIC_BELOW,
    )

@app.route("/api/host")
def api_host():
    """Which box is serving this page. Lets the footer name the host, the way
    the urwebdash sidebar shows the version."""
    return jsonify({
        "hostname": socket.gethostname(),
        "app":      "UrNetwork-Stats-Dashboard",
    })

@app.route("/api/rows")
def api_rows():
    """
    JSON endpoint for infinite scroll. Takes the timestamp cursor of the last
    row the client has and returns the next batch of older rows.

    Keyset pagination on the timestamp (not OFFSET) so rows appended by the
    15-minute scheduler can never shift or duplicate the client's position.
    """
    cursor = request.args.get('cursor', '')
    try:
        limit = min(max(int(request.args.get('limit', SCROLL_PAGE_SIZE)), 1), 10000)
    except ValueError:
        limit = SCROLL_PAGE_SIZE

    q = (Stats.query
         .filter(or_(Stats.fetch_error.is_(None),
                     Stats.fetch_error.notin_(IN_FLIGHT_MARKERS)))
         .order_by(Stats.timestamp.desc()))
    if cursor:
        try:
            cur_dt = datetime.datetime.fromisoformat(cursor)
        except ValueError:
            return jsonify({"error": "bad cursor"}), 400
        q = q.filter(Stats.timestamp < cur_dt)

    # +1 to detect whether more rows remain.
    entries = q.limit(limit + 1).all()
    has_more = len(entries) > limit
    if has_more:
        entries = entries[:limit]

    rows = [_row_payload(e, entries[i+1] if i < len(entries) - 1 else None)
            for i, e in enumerate(entries)]
    return jsonify({
        "rows":     rows,
        "has_more": has_more,
        "cursor":   (entries[-1].timestamp.isoformat() if has_more and entries else ""),
    })


@app.route("/trigger", methods=["POST"])
def trigger_fetch():
    """
    Manually fetch latest stats and redirect back to index.
    Flash an error on failure.
    """
    dark = request.args.get('dark', '0')
    try:
        token = login_check()
        stats = fetch_transfer_stats(token)
        entry = Stats(
            paid_bytes   = stats["paid_bytes"],
            paid_gb      = stats["paid_gb"],
            unpaid_bytes = stats["unpaid_bytes"],
            unpaid_gb    = stats["unpaid_gb"]
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Manual fetch aborted: {e}")
        flash("Unable to fetch stats; check logs.")
    return redirect(url_for('index', dark=dark))

@app.route("/clear", methods=["POST"])
def clear_db():
    """
    Delete all Stats records and redirect back to index.
    """
    dark = request.args.get('dark', '0')
    # Stats.query.delete()  # DISABLED - would clear all records
    db.session.commit()
    flash("All records cleared.")
    return redirect(url_for('index', dark=dark))

# ----------------------------------------
# Application Entry Point
# ----------------------------------------
def _migrate_stats_for_gaps():
    """
    Bring an existing `stats` table up to the gap-tracking schema.

    Two things are needed and neither is done by create_all():
      1. a `fetch_error` column to record why a reading is missing, and
      2. the value columns must allow NULL, because a placeholder row is
         written the moment a tick fires and is filled in only if the fetch
         later succeeds.

    SQLite cannot drop a NOT NULL constraint in place, so the table is
    rebuilt: copy the rows, drop, recreate, copy back. Done inside a
    transaction and only when the schema is actually behind, so it is a
    no-op on an up-to-date database.
    """
    with db.engine.connect() as conn:
        info = list(conn.execute(db.text("PRAGMA table_info(stats)")))
        if not info:
            return
        names = {r[1] for r in info}
        needs_column = "fetch_error" not in names
        notnull = {r[1] for r in info if r[3]}          # r[3] == notnull flag
        needs_nullable = bool(notnull & {"paid_bytes", "paid_gb",
                                         "unpaid_bytes", "unpaid_gb"})
        if not (needs_column or needs_nullable):
            return

        app.logger.info("Migrating stats table for gap tracking "
                        f"(add_column={needs_column}, relax_notnull={needs_nullable})")
        with db.engine.begin() as conn:
            cols = [r[1] for r in info]
            keep = [c for c in cols if c not in ("id",)]
            select = ", ".join(f'"{c}"' for c in keep)
            conn.execute(db.text("ALTER TABLE stats RENAME TO stats_old"))
            conn.execute(db.text("""
                CREATE TABLE stats (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    DATETIME    DEFAULT CURRENT_TIMESTAMP,
                    paid_bytes   BIGINT,
                    paid_gb      FLOAT,
                    unpaid_bytes BIGINT,
                    unpaid_gb    FLOAT,
                    fetch_error  VARCHAR(200)
                )"""))
            conn.execute(db.text(
                f"INSERT INTO stats ({select}) SELECT {select} FROM stats_old"))
            conn.execute(db.text("DROP TABLE stats_old"))
        app.logger.info("stats table migrated")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        _migrate_stats_for_gaps()
    scheduler.start()
    app.run(host="0.0.0.0", port=3000, debug=False)
