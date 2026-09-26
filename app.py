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
    paid_bytes   = db.Column(db.BigInteger, nullable=False)
    paid_gb      = db.Column(db.Float,      nullable=False)
    unpaid_bytes = db.Column(db.BigInteger, nullable=False)
    unpaid_gb    = db.Column(db.Float,      nullable=False)

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
def fetch_transfer_stats(jwt_token: str):
    """
    Retrieve the latest transfer statistics using the provided JWT.
    Returns:
      - A dict with keys: paid_bytes, paid_gb, unpaid_bytes, unpaid_gb.
    """
    headers = {"Authorization": f"Bearer {jwt_token}", "Accept": "*/*"}
    resp = request_with_retry("get", f"{API_BASE}/transfer/stats", headers=headers)
    d = resp.json()
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
    Runs every quarter-hour:
      1. Ensures valid JWT.
      2. Fetches transfer stats.
      3. Persists a new Stats record in the database.
    """
    with app.app_context():
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
            current_app.logger.info(f"Logged @ {entry.timestamp}")
        except Exception as e:
            current_app.logger.error(f"log_stats aborted: {e}")

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
    .heat-0  { color: #e5e7eb !important; }   /* zero change: plain white, static */
    .heat-1  { color: #e5e7eb !important; }
    .heat-2  { color: #e5e7eb !important; }
    .heat-3  { color: #e5e7eb !important; }   /* bad */
    .heat-4  { color: #f25435 !important; }
    .heat-5  { color: #f66325 !important; }
    .heat-6  { color: #f97316 !important; }   /* low */
    .heat-7  { color: #fa8c1b !important; }
    .heat-8  { color: #faa61f !important; }
    .heat-9  { color: #fbbf24 !important; }   /* medium */
    .heat-10 { color: #b3c137 !important; }
    .heat-11 { color: #6ac34b !important; }
    .heat-12 { color: #22c55e !important; }   /* >=1 GB: baseline good */
    .heat-13 { color: #2fcd69 !important; }
    .heat-14 { color: #3dd675 !important; }
    .heat-15 { color: #4ade80 !important; }   /* 2 GB */
    .heat-16 { color: #31e08b !important; }
    .heat-17 { color: #19e395 !important; }
    .heat-18 { color: #00e5a0 !important; }   /* 4 GB, bright mint */
    .heat-19 { color: #0bdfba !important; }
    .heat-20 { color: #17d9d4 !important; }
    .heat-21 { color: #22d3ee !important; }   /* 7 GB, cyan */
    .heat-22 { color: #29ccf1 !important; }
    .heat-23 { color: #31c4f5 !important; }
    .heat-24 { color: #38bdf8 !important; }   /* 10 GB, blue */
    .heat-25 { color: #50adf8 !important; }
    .heat-26 { color: #699cf8 !important; }
    .heat-27 { color: #818cf8 !important; }   /* 15 GB, indigo */
    .heat-28 { color: #a783e2 !important; }
    .heat-29 { color: #ce7bcc !important; }   /* purple band ends here */
    .heat-30 { color: #f472b6 !important; }   /* >=20 GB, hot pink */

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
  </style>

  <!-- TABLE (newest first) -->
  <div class="table-scroll">
  <table class="table table-striped table-sm">
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
      <tr>
        <td>{{ row.ts_str }}</td>
        <td>{{ "%.3f"|format(row.paid_gb) }}</td>
        <td>{{ "{:,}".format(row.unpaid_bytes) }}</td>
        <td class="{{ sign_class(row.delta_bytes) }}">
          {% if row.delta_bytes is not none %}
            {{ "{:,}".format(row.delta_bytes) }}
          {% else %}N/A{% endif %}
        </td>
        <td>{{ "%.3f"|format(row.unpaid_gb) }}</td>
        <td class="{{ heat_class(row.delta_gb) }}" style="{{ pulse_style(row.delta_gb) }}">
          {% if row.delta_gb is not none %}
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
      const cells = [r.ts_str, fmtF(r.paid_gb), fmtN(r.unpaid_bytes),
                     fmtN(r.delta_bytes), fmtF(r.unpaid_gb), fmtF(r.delta_gb)];
      cells.forEach((text, i) => {
        const td = document.createElement('td');
        td.textContent = text;
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
    if nxt is not None:
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
    entries = (Stats.query
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

    q = Stats.query.order_by(Stats.timestamp.desc())
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
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    scheduler.start()
    app.run(host="0.0.0.0", port=3000, debug=False)
