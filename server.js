const express = require("express");
const dotenv  = require("dotenv");
const { Pool } = require("pg");
const {
  setConfig,
  placeMarketOrder: igPlaceMarketOrder,
  getOpenPositions,
  getCurrentPrice,
  modifyStopLevel,
  closePositionByDealId,
  searchMarkets,
  getMarketDetails,
  resolveEpic
} = require("./brokers/ig");

// ── IC Markets via MetaAPI ──────────────────────────────────────────────────
const {
  placeMarketOrder: icPlaceMarketOrder
} = require("./brokers/icmarkets");

dotenv.config();

const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

const PORT           = process.env.PORT           || 3000;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || "changeme";
const APP_NAME       = process.env.APP_NAME       || "H2 Webhook Bridge";
const EXECUTION_MODE = process.env.EXECUTION_MODE || "APPROVAL";
const DEFAULT_BROKER = process.env.DEFAULT_BROKER || "IG";

// ---------------------------------------------------------------------------
// BROKER ROUTER — decides which adapter to call based on broker name
// ---------------------------------------------------------------------------
async function routePlaceMarketOrder(brokerName, signal) {
  const b = String(brokerName || DEFAULT_BROKER).toUpperCase();
  if (b === "ICMARKETS" || b === "IC_MARKETS" || b === "IC") {
    console.log("[BrokerRouter] Routing to IC Markets (MetaAPI)");
    return await icPlaceMarketOrder(signal);
  }
  // Default: IG Markets
  console.log("[BrokerRouter] Routing to IG Markets");
  return await igPlaceMarketOrder(signal);
}

// ---------------------------------------------------------------------------
// POSTGRESQL CONNECTION
// ---------------------------------------------------------------------------
const db = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL && process.env.DATABASE_URL.includes("railway")
    ? { rejectUnauthorized: false }
    : false
});

// ---------------------------------------------------------------------------
// DATABASE INIT — creates tables on first boot
// ---------------------------------------------------------------------------
async function initDb() {
  await db.query(`
    CREATE TABLE IF NOT EXISTS settings (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS alerts (
      id          TEXT PRIMARY KEY,
      data        JSONB NOT NULL,
      received_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS trades (
      id         TEXT PRIMARY KEY,
      data       JSONB NOT NULL,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS executions (
      id         TEXT PRIMARY KEY,
      data       JSONB NOT NULL,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
  `);
  console.log("[DB] Tables ready");
}

// ---------------------------------------------------------------------------
// DB HELPERS
// ---------------------------------------------------------------------------
async function dbGetSetting(key, fallback) {
  const r = await db.query("SELECT value FROM settings WHERE key=$1", [key]);
  if (r.rows.length === 0) return fallback;
  const v = r.rows[0].value;
  const n = Number(v);
  return Number.isFinite(n) ? n : v;
}

async function dbSetSetting(key, value) {
  await db.query(
    "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
    [key, String(value)]
  );
}

async function dbGetAllSettings() {
  const r = await db.query("SELECT key, value FROM settings");
  const out = {};
  for (const row of r.rows) {
    const n = Number(row.value);
    out[row.key] = Number.isFinite(n) ? n : row.value;
  }
  return out;
}

async function dbInsertAlert(record) {
  await db.query(
    "INSERT INTO alerts(id, data) VALUES($1, $2) ON CONFLICT DO NOTHING",
    [record.id, JSON.stringify(record)]
  );
}

async function dbGetAlerts(limit = 50) {
  const r = await db.query(
    "SELECT data FROM alerts ORDER BY received_at DESC LIMIT $1", [limit]
  );
  return r.rows.map(x => x.data);
}

async function dbCountAlerts() {
  const r = await db.query("SELECT COUNT(*) FROM alerts");
  return Number(r.rows[0].count);
}

async function dbInsertTrade(record) {
  await db.query(
    "INSERT INTO trades(id, data) VALUES($1, $2) ON CONFLICT DO NOTHING",
    [record.id, JSON.stringify(record)]
  );
}

async function dbUpdateTrade(id, record) {
  await db.query(
    "UPDATE trades SET data=$2, updated_at=NOW() WHERE id=$1",
    [id, JSON.stringify(record)]
  );
}

async function dbGetTrades(limit = 50) {
  const r = await db.query(
    "SELECT data FROM trades ORDER BY created_at DESC LIMIT $1", [limit]
  );
  return r.rows.map(x => x.data);
}

async function dbGetTradeById(id) {
  const r = await db.query("SELECT data FROM trades WHERE id=$1", [id]);
  return r.rows.length > 0 ? r.rows[0].data : null;
}

async function dbCountTrades() {
  const r = await db.query("SELECT COUNT(*) FROM trades");
  return Number(r.rows[0].count);
}

async function dbGetActiveTrades() {
  const r = await db.query(`
    SELECT data FROM trades
    WHERE data->>'status' IN ('OPEN','OPEN / TRACKING')
       OR data->>'status' LIKE '%TP1%'
       OR data->>'status' LIKE '%TP2%'
       OR data->>'status' LIKE '%BREAKEVEN%'
    ORDER BY created_at DESC
  `);
  return r.rows.map(x => x.data);
}

async function dbInsertExecution(record) {
  await db.query(
    "INSERT INTO executions(id, data) VALUES($1, $2) ON CONFLICT DO NOTHING",
    [record.id, JSON.stringify(record)]
  );
}

async function dbUpdateExecution(id, record) {
  await db.query(
    "UPDATE executions SET data=$2, updated_at=NOW() WHERE id=$1",
    [id, JSON.stringify(record)]
  );
}

async function dbGetExecutions(limit = 50) {
  const r = await db.query(
    "SELECT data FROM executions ORDER BY created_at DESC LIMIT $1", [limit]
  );
  return r.rows.map(x => x.data);
}

async function dbGetExecutionById(id) {
  const r = await db.query("SELECT data FROM executions WHERE id=$1", [id]);
  return r.rows.length > 0 ? r.rows[0].data : null;
}

async function dbGetPendingExecutions() {
  const r = await db.query(`
    SELECT data FROM executions
    WHERE data->>'status' = 'PENDING APPROVAL'
    ORDER BY created_at DESC
  `);
  return r.rows.map(x => x.data);
}

async function dbCountExecutions() {
  const r = await db.query("SELECT COUNT(*) FROM executions");
  return Number(r.rows[0].count);
}

// ---------------------------------------------------------------------------
// SETTINGS CACHE
// ---------------------------------------------------------------------------
let settingsCache = {
  usdPerPip: 0.1, beAfterTp1: 1, autoCloseAtTp3: 1,
  tp1PartialPct: 0, tp2PartialPct: 0, autoPriceTrack: 1, pollIntervalSec: 60
};

async function loadSettingsCache() {
  const stored = await dbGetAllSettings();
  settingsCache = { ...settingsCache, ...stored };
}

async function loadBrokerConfig() {
  const s = settingsCache;
  const epics   = {};
  const tvMap   = {};

  for (const key of ["JP225","NAS100","DAX40","SP500","DOW","FTSE","AUS200"]) {
    const epicVal = await dbGetSetting(`epic_${key}`, "");
    if (epicVal) epics[key] = epicVal;

    const envEpic = process.env[`IG_EPIC_${key}`];
    if (envEpic && !epics[key]) epics[key] = envEpic;

    const tvVal = await dbGetSetting(`tv_${key}`, "");
    if (tvVal) {
      const aliases = tvVal.split(",").map(v => v.trim().toUpperCase()).filter(Boolean);
      for (const alias of aliases) {
        tvMap[alias] = key;
      }
    }
  }

  setConfig({
    baseUrl:      s.ig_base_url     || process.env.IG_BASE_URL     || "https://demo-api.ig.com/gateway/deal",
    apiKey:       s.ig_api_key      || process.env.IG_API_KEY      || "",
    identifier:   s.ig_identifier   || process.env.IG_IDENTIFIER   || "",
    password:     s.ig_password     || process.env.IG_PASSWORD     || "",
    accountMode:  s.ig_account_mode || process.env.IG_ACCOUNT_MODE || "DEMO",
    defaultSize:  Number(s.ig_default_size || process.env.IG_DEFAULT_SIZE || 1),
    currencyCode: s.ig_currency     || process.env.IG_CURRENCY_CODE || "USD",
    epics,
    tvMap
  });
  console.log("[Config] Broker config loaded. Epic keys:", Object.keys(epics).join(", ") || "none");
  console.log("[Config] TV symbol map:", JSON.stringify(tvMap));
  console.log("[Config] Active broker:", DEFAULT_BROKER);
}

async function saveSetting(key, value) {
  settingsCache[key] = value;
  await dbSetSetting(key, value);
}

function getSetting(key, fallback) {
  return settingsCache[key] !== undefined ? settingsCache[key] : fallback;
}

function getManagerSettings() {
  return {
    usdPerPip:       Number(getSetting("usdPerPip", 0.1)),
    beAfterTp1:      Number(getSetting("beAfterTp1", 1)),
    autoCloseAtTp3:  Number(getSetting("autoCloseAtTp3", 1)),
    tp1PartialPct:   Number(getSetting("tp1PartialPct", 0)),
    tp2PartialPct:   Number(getSetting("tp2PartialPct", 0)),
    autoPriceTrack:  Number(getSetting("autoPriceTrack", 1)),
    pollIntervalSec: Number(getSetting("pollIntervalSec", 60))
  };
}

// ---------------------------------------------------------------------------
// UTILITIES
// ---------------------------------------------------------------------------
function nowIso() { return new Date().toISOString(); }
function sanitizeString(v, fb = "") { return typeof v === "string" ? v.trim() : fb; }
function sanitizeNumber(v, fb = null) {
  if (v === null || v === undefined || v === "") return fb;
  const n = Number(v);
  return Number.isFinite(n) ? n : fb;
}
function sanitizeBool(v, fb = 0) {
  if (v === undefined || v === null) return fb;
  if (v === "on" || v === "true" || v === true || v === 1 || v === "1") return 1;
  return 0;
}
function formatNumber(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return Number(v).toFixed(d);
}
function calcPipDist(a, b) {
  if (a == null || b == null) return null;
  return Math.abs(Number(a) - Number(b));
}
function calcMoney(dist, upp) {
  if (dist == null || upp == null) return null;
  return Number(dist) * Number(upp);
}
function recalcTrade(t) {
  const upp    = Number(getSetting("usdPerPip", 0.1));
  t.usdPerPip  = upp;
  t.riskPips   = calcPipDist(t.entry, t.currentSl);
  t.tp1Pips    = calcPipDist(t.entry, t.tp1);
  t.tp2Pips    = calcPipDist(t.entry, t.tp2);
  t.tp3Pips    = calcPipDist(t.entry, t.tp3);
  t.riskUsd    = calcMoney(t.riskPips, upp);
  t.tp1Usd     = calcMoney(t.tp1Pips,  upp);
  t.tp2Usd     = calcMoney(t.tp2Pips,  upp);
  t.tp3Usd     = calcMoney(t.tp3Pips,  upp);
  return t;
}
function typePill(type) {
  const t = String(type || "").toUpperCase();
  if (t === "LONG"  || t === "BUY")  return `<span class="pill pill-green">${t}</span>`;
  if (t === "SHORT" || t === "SELL") return `<span class="pill pill-red">${t}</span>`;
  if (t === "WAIT"  || t === "DANGER") return `<span class="pill pill-yellow">${t}</span>`;
  return `<span class="pill pill-gray">${t || "UNKNOWN"}</span>`;
}
function statusPill(s) {
  const v = String(s || "").toUpperCase();
  if (v.includes("OPEN"))      return `<span class="pill pill-green">${v}</span>`;
  if (v.includes("TP"))        return `<span class="pill pill-blue">${v}</span>`;
  if (v.includes("BREAKEVEN")) return `<span class="pill pill-cyan">${v}</span>`;
  if (v.includes("CLOSED"))    return `<span class="pill pill-gray">${v}</span>`;
  if (v.includes("STOP"))      return `<span class="pill pill-red">${v}</span>`;
  if (v.includes("PENDING"))   return `<span class="pill pill-yellow">${v}</span>`;
  if (v.includes("SENT"))      return `<span class="pill pill-green">${v}</span>`;
  if (v.includes("FAILED"))    return `<span class="pill pill-red">${v}</span>`;
  if (v.includes("CANCELLED")) return `<span class="pill pill-gray">${v}</span>`;
  if (v.includes("TRACKING"))  return `<span class="pill pill-cyan">${v}</span>`;
  return `<span class="pill pill-gray">${v || "UNKNOWN"}</span>`;
}

// ---------------------------------------------------------------------------
// TRADE CHECK LOGIC
// ---------------------------------------------------------------------------
async function applyTradeCheck(tradeId, currentPriceInput, source = "manual") {
  const trade = await dbGetTradeById(tradeId);
  if (!trade) return;

  const currentPrice = sanitizeNumber(currentPriceInput);
  if (currentPrice === null) return;

  const manager      = getManagerSettings();
  let status         = trade.status || "OPEN";
  let currentSl      = trade.currentSl;
  let tp1Hit         = Number(trade.tp1Hit || 0);
  let tp2Hit         = Number(trade.tp2Hit || 0);
  let tp3Hit         = Number(trade.tp3Hit || 0);
  let breakEvenMoved = Number(trade.breakEvenMoved || 0);
  let lastAction     = `[${source}] Checked price ${currentPrice}`;
  const type         = String(trade.type || "").toUpperCase();

  if (status === "CLOSED" || status === "STOPPED OUT" || status === "TP3 HIT / CLOSED") return;

  if (type === "LONG") {
    if (!tp1Hit && currentPrice >= Number(trade.tp1)) {
      tp1Hit = 1; status = "TP1 HIT";
      lastAction = `[${source}] TP1 hit at ${currentPrice}`;
      if (!breakEvenMoved && manager.beAfterTp1) {
        currentSl = Number(trade.entry); breakEvenMoved = 1;
        status = "TP1 HIT / BREAKEVEN MOVED";
        lastAction = `[${source}] TP1 hit at ${currentPrice} — SL moved to entry`;
      }
    }
    if (!tp2Hit && currentPrice >= Number(trade.tp2)) {
      tp2Hit = 1; status = "TP2 HIT";
      lastAction = `[${source}] TP2 hit at ${currentPrice}`;
    }
    if (!tp3Hit && currentPrice >= Number(trade.tp3)) {
      tp3Hit = 1;
      status     = manager.autoCloseAtTp3 ? "TP3 HIT / CLOSED" : "TP3 HIT";
      lastAction = manager.autoCloseAtTp3
        ? `[${source}] TP3 hit at ${currentPrice} — trade closed`
        : `[${source}] TP3 hit at ${currentPrice}`;
    }
    if (status !== "TP3 HIT / CLOSED" && currentPrice <= Number(currentSl)) {
      status = "STOPPED OUT";
      lastAction = `[${source}] Stopped out at ${currentPrice}`;
    }
  }

  if (type === "SHORT") {
    if (!tp1Hit && currentPrice <= Number(trade.tp1)) {
      tp1Hit = 1; status = "TP1 HIT";
      lastAction = `[${source}] TP1 hit at ${currentPrice}`;
      if (!breakEvenMoved && manager.beAfterTp1) {
        currentSl = Number(trade.entry); breakEvenMoved = 1;
        status = "TP1 HIT / BREAKEVEN MOVED";
        lastAction = `[${source}] TP1 hit at ${currentPrice} — SL moved to entry`;
      }
    }
    if (!tp2Hit && currentPrice <= Number(trade.tp2)) {
      tp2Hit = 1; status = "TP2 HIT";
      lastAction = `[${source}] TP2 hit at ${currentPrice}`;
    }
    if (!tp3Hit && currentPrice <= Number(trade.tp3)) {
      tp3Hit = 1;
      status     = manager.autoCloseAtTp3 ? "TP3 HIT / CLOSED" : "TP3 HIT";
      lastAction = manager.autoCloseAtTp3
        ? `[${source}] TP3 hit at ${currentPrice} — trade closed`
        : `[${source}] TP3 hit at ${currentPrice}`;
    }
    if (status !== "TP3 HIT / CLOSED" && currentPrice >= Number(currentSl)) {
      status = "STOPPED OUT";
      lastAction = `[${source}] Stopped out at ${currentPrice}`;
    }
  }

  const updated = {
    ...trade,
    currentPrice, currentSl, tp1Hit, tp2Hit, tp3Hit,
    breakEvenMoved, status, lastAction,
    lastCheckedAt: nowIso()
  };
  recalcTrade(updated);
  await dbUpdateTrade(tradeId, updated);
}

// ---------------------------------------------------------------------------
// AUTO PRICE TRACKER
// ---------------------------------------------------------------------------
let priceTrackerInterval = null;
let lastPollTime         = null;
let lastPollStatus       = "Never polled";

async function runPriceTracker() {
  try {
    lastPollTime   = nowIso();
    lastPollStatus = "Polling...";
    const manager  = getManagerSettings();

    const activeTrades = await dbGetActiveTrades();
    if (activeTrades.length === 0) {
      lastPollStatus = `No active trades — last checked ${lastPollTime}`;
      return;
    }

    const symbols    = [...new Set(activeTrades.map(t => t.symbol))];
    let updatedCount = 0, errorCount = 0;

    for (const symbol of symbols) {
      try {
        const epic = resolveEpic(symbol);
        if (!epic) { console.log(`[AutoTracker] No epic for ${symbol}`); continue; }

        const priceData = await getCurrentPrice(epic);
        if (priceData.status === "CLOSED" || priceData.status === "OFFLINE") {
          console.log(`[AutoTracker] ${symbol} market ${priceData.status}`); continue;
        }

        const price = priceData.mid;
        const tradesForSymbol = activeTrades.filter(t => t.symbol === symbol);

        for (const trade of tradesForSymbol) {
          const before = trade.status;
          await applyTradeCheck(trade.id, price, "auto");

          const updated = await dbGetTradeById(trade.id);
          if (updated) {
            updated.livePrice = price;
            updated.liveBid   = priceData.bid;
            updated.liveOffer = priceData.offer;
            await dbUpdateTrade(trade.id, updated);
          }
          updatedCount++;

          if (manager.beAfterTp1 && updated && updated.breakEvenMoved && !updated.beMovedOnBroker) {
            const allExecs = await dbGetExecutions(100);
            const exec = allExecs.find(e => e.tradeId === trade.id && e.status === "SENT");
            if (exec && exec.brokerResponse && exec.brokerResponse.dealId) {
              try {
                await modifyStopLevel(exec.brokerResponse.dealId, updated.entry);
                updated.beMovedOnBroker = true;
                updated.lastAction += " | SL moved to entry on IG";
                await dbUpdateTrade(trade.id, updated);
                console.log(`[AutoTracker] Moved SL to BE on IG for deal ${exec.brokerResponse.dealId}`);
              } catch (beErr) {
                console.log(`[AutoTracker] Could not move SL on IG: ${beErr.message}`);
              }
            }
          }

          if (updated && updated.status !== before) {
            console.log(`[AutoTracker] ${trade.id}: ${before} → ${updated.status}`);
          }
        }
      } catch (symErr) {
        console.log(`[AutoTracker] Error for ${symbol}: ${symErr.message}`);
        errorCount++;
      }
    }
    lastPollStatus = `OK — updated ${updatedCount} trade(s), ${errorCount} error(s) — ${lastPollTime}`;
  } catch (err) {
    lastPollStatus = `ERROR: ${err.message} — ${nowIso()}`;
    console.error("[AutoTracker] Poll error:", err.message);
  }
}

function startPriceTracker() {
  if (priceTrackerInterval) { clearInterval(priceTrackerInterval); priceTrackerInterval = null; }
  const ms = Math.max(30, Number(getSetting("pollIntervalSec", 60))) * 1000;
  priceTrackerInterval = setInterval(runPriceTracker, ms);
  console.log(`[AutoTracker] Started — polling every ${ms / 1000}s`);
}

function stopPriceTracker() {
  if (priceTrackerInterval) {
    clearInterval(priceTrackerInterval); priceTrackerInterval = null;
    console.log("[AutoTracker] Stopped");
  }
}

// ---------------------------------------------------------------------------
// PAGE RENDERER
// ---------------------------------------------------------------------------
function renderPage(title, content) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${title}</title>
  <style>
    body { margin:0; font-family:Arial,sans-serif; background:#0f1115; color:#e8ecf1; }
    .wrap { max-width:1500px; margin:0 auto; padding:24px; }
    h1,h2 { margin-top:0; }
    .topbar { display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:24px; flex-wrap:wrap; }
    .muted { color:#9aa4b2; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }
    .card { background:#171a21; border:1px solid #2a303a; border-radius:14px; padding:16px; box-shadow:0 4px 18px rgba(0,0,0,.25); }
    .big { font-size:28px; font-weight:bold; }
    .label { color:#9aa4b2; font-size:13px; margin-bottom:8px; }
    .pill { display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:bold; }
    .pill-green  { background:rgba(0,180,90,.18);    color:#72f0ab; }
    .pill-red    { background:rgba(220,70,70,.18);   color:#ff9a9a; }
    .pill-yellow { background:rgba(220,180,70,.18);  color:#ffd978; }
    .pill-gray   { background:rgba(160,160,160,.15); color:#d3d7de; }
    .pill-blue   { background:rgba(70,120,220,.18);  color:#9fc2ff; }
    .pill-cyan   { background:rgba(60,190,210,.18);  color:#98f0ff; }
    .section { margin-bottom:24px; }
    table { width:100%; border-collapse:collapse; background:#171a21; border:1px solid #2a303a; border-radius:14px; overflow:hidden; }
    th,td { text-align:left; padding:12px 10px; border-bottom:1px solid #252b34; font-size:14px; vertical-align:top; }
    th { background:#1d222c; color:#cfd7e3; font-size:13px; }
    .trade-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:16px; }
    .trade-card { background:#171a21; border:1px solid #2a303a; border-radius:14px; padding:16px; }
    .trade-header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }
    .trade-symbol { font-size:20px; font-weight:bold; }
    .trade-meta { color:#9aa4b2; font-size:13px; margin-bottom:14px; }
    .trade-stats { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
    .stat { background:#11151c; border:1px solid #232a34; border-radius:10px; padding:10px; }
    .stat .k { color:#9aa4b2; font-size:12px; margin-bottom:4px; }
    .stat .v { font-weight:bold; font-size:14px; }
    .live-price { background:#0c2a1a; border:2px solid #0c8a54; border-radius:10px; padding:10px; margin-bottom:14px; }
    .live-price .lp-label { color:#72f0ab; font-size:12px; font-weight:bold; margin-bottom:4px; }
    .live-price .lp-value { font-size:24px; font-weight:bold; color:#72f0ab; }
    .live-price .lp-meta  { color:#9aa4b2; font-size:12px; margin-top:4px; }
    .tracker-bar { background:#111620; border:1px solid #2a303a; border-radius:10px; padding:12px 16px; margin-bottom:20px; display:flex; gap:20px; flex-wrap:wrap; align-items:center; }
    .db-badge { background:rgba(60,190,210,.15); border:1px solid #0b8ea2; color:#98f0ff; border-radius:8px; padding:4px 10px; font-size:12px; font-weight:bold; }
    .broker-badge { background:rgba(220,180,70,.15); border:1px solid #c75000; color:#ffd978; border-radius:8px; padding:4px 10px; font-size:12px; font-weight:bold; }
    form { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
    input[type="number"] { background:#0f1319; border:1px solid #2a303a; color:#e8ecf1; border-radius:10px; padding:10px 12px; font-size:14px; width:140px; }
    .checkbox-row { display:flex; gap:18px; flex-wrap:wrap; align-items:center; margin:8px 0 14px; }
    .checkbox-row label { display:flex; align-items:center; gap:6px; color:#dce3ed; font-size:14px; }
    button { background:#3a7afe; color:white; border:0; border-radius:10px; padding:10px 14px; font-size:13px; cursor:pointer; }
    button:hover { background:#2f68d7; }
    .btn-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    .btn-cyan  { background:#0b8ea2; }
    .btn-red   { background:#b43737; }
    .btn-gray  { background:#555; }
    .btn-green { background:#0c8a54; }
    a { color:#8db8ff; text-decoration:none; }
    .nav { display:flex; gap:14px; flex-wrap:wrap; }
    .success { color:#72f0ab; }
    .tracker-on  { color:#72f0ab; font-weight:bold; }
    .tracker-off { color:#ff9a9a; font-weight:bold; }
  </style>
</head>
<body>
  <div class="wrap">${content}</div>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// DASHBOARD
// ---------------------------------------------------------------------------
app.get("/", async (req, res) => {
  try {
    const [latestAlerts, latestTrades, latestExecutions,
           pendingExecs, countA, countT, countE] = await Promise.all([
      dbGetAlerts(10),
      dbGetTrades(12),
      dbGetExecutions(10),
      dbGetPendingExecutions(),
      dbCountAlerts(),
      dbCountTrades(),
      dbCountExecutions()
    ]);

    const manager        = getManagerSettings();
    const trackerRunning = priceTrackerInterval !== null;

    const alertsRows = latestAlerts.map(a => `
      <tr>
        <td>${a.receivedAt}</td>
        <td>${typePill(a.type)}</td>
        <td>${a.symbol}</td>
        <td>${a.tf}</td>
        <td>${formatNumber(a.entry, 2)}</td>
        <td>${formatNumber(a.sl, 2)}</td>
        <td>${formatNumber(a.tp1, 2)}</td>
        <td>${formatNumber(a.tp2, 2)}</td>
        <td>${formatNumber(a.tp3, 2)}</td>
        <td>${a.confidence ?? "-"}</td>
      </tr>`).join("");

    const execCards = (pendingExecs.length > 0 ? pendingExecs : latestExecutions.slice(0, 6)).map(e => `
      <div class="trade-card">
        <div class="trade-header">
          <div>
            <div class="trade-symbol">${e.symbol || "-"}</div>
            <div class="trade-meta">${e.createdAt} · ${e.broker} · ${e.accountMode}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            ${typePill(e.type)}${statusPill(e.status)}
          </div>
        </div>
        <div class="trade-stats">
          <div class="stat"><div class="k">Strategy</div><div class="v">${e.strategyId || "-"}</div></div>
          <div class="stat"><div class="k">Size</div><div class="v">${e.brokerSize || "-"}</div></div>
          <div class="stat"><div class="k">Entry</div><div class="v">${formatNumber(e.entry, 2)}</div></div>
          <div class="stat"><div class="k">SL</div><div class="v">${formatNumber(e.sl, 2)}</div></div>
          <div class="stat"><div class="k">TP1</div><div class="v">${formatNumber(e.tp1, 2)}</div></div>
          <div class="stat"><div class="k">Status</div><div class="v">${e.status}</div></div>
          <div class="stat" style="grid-column:span 2"><div class="k">Last Action</div><div class="v">${e.lastAction || "-"}</div></div>
        </div>
        ${e.status === "PENDING APPROVAL" ? `
          <div class="btn-row">
            <form method="POST" action="/execution/${e.id}/approve">
              <button class="btn-green" type="submit">✅ Approve Trade</button>
            </form>
            <form method="POST" action="/execution/${e.id}/cancel">
              <button class="btn-red" type="submit">Cancel</button>
            </form>
          </div>` : ""}
      </div>`).join("");

    const tradeCards = latestTrades.map(t => {
      const hasLive = t.livePrice != null;
      return `
      <div class="trade-card">
        <div class="trade-header">
          <div>
            <div class="trade-symbol">${t.symbol || "-"}</div>
            <div class="trade-meta">${t.createdAt} · TF ${t.tf || "-"} · ${t.strategyId || "-"}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            ${typePill(t.type)}${statusPill(t.status)}
          </div>
        </div>
        ${hasLive ? `
          <div class="live-price">
            <div class="lp-label">🔴 LIVE PRICE (Auto Tracked)</div>
            <div class="lp-value">${formatNumber(t.livePrice, 2)}</div>
            <div class="lp-meta">Bid: ${formatNumber(t.liveBid, 2)} · Offer: ${formatNumber(t.liveOffer, 2)} · ${t.lastCheckedAt || "-"}</div>
          </div>` : ""}
        <div class="trade-stats">
          <div class="stat"><div class="k">Entry</div><div class="v">${formatNumber(t.entry, 2)}</div></div>
          <div class="stat"><div class="k">Current SL</div><div class="v">${formatNumber(t.currentSl, 2)}</div></div>
          <div class="stat"><div class="k">Current Price</div><div class="v">${hasLive ? formatNumber(t.livePrice, 2) + " 🔴" : formatNumber(t.currentPrice, 2)}</div></div>
          <div class="stat"><div class="k">TP1</div><div class="v">${formatNumber(t.tp1, 2)}</div></div>
          <div class="stat"><div class="k">TP2</div><div class="v">${formatNumber(t.tp2, 2)}</div></div>
          <div class="stat"><div class="k">TP3</div><div class="v">${formatNumber(t.tp3, 2)}</div></div>
          <div class="stat"><div class="k">Risk USD</div><div class="v">$${formatNumber(t.riskUsd, 2)}</div></div>
          <div class="stat"><div class="k">TP1 Value</div><div class="v">$${formatNumber(t.tp1Usd, 2)}</div></div>
          <div class="stat"><div class="k">Break-even</div><div class="v">${t.breakEvenMoved ? "✅ Moved" : "Not moved"}</div></div>
          <div class="stat"><div class="k">TP Hits</div><div class="v">TP1:${t.tp1Hit ? "✅" : "—"} TP2:${t.tp2Hit ? "✅" : "—"} TP3:${t.tp3Hit ? "✅" : "—"}</div></div>
          <div class="stat" style="grid-column:span 2"><div class="k">Last Action</div><div class="v">${t.lastAction || "-"}</div></div>
        </div>
        <div class="section card" style="margin-bottom:14px;">
          <div class="label">Manual Price Check</div>
          <form method="POST" action="/trade/${t.id}/check-price">
            <input type="number" name="currentPrice" step="0.01" placeholder="Enter current price" required />
            <button class="btn-green" type="submit">Check Trade</button>
          </form>
        </div>
        <div class="btn-row">
          <form method="POST" action="/trade/${t.id}/tp1-hit"><button type="submit">TP1 Hit</button></form>
          <form method="POST" action="/trade/${t.id}/tp2-hit"><button type="submit">TP2 Hit</button></form>
          <form method="POST" action="/trade/${t.id}/tp3-hit"><button type="submit">TP3 Hit</button></form>
          <form method="POST" action="/trade/${t.id}/move-sl-be"><button class="btn-cyan" type="submit">Move SL to BE</button></form>
          <form method="POST" action="/trade/${t.id}/close"><button class="btn-gray" type="submit">Close</button></form>
          <form method="POST" action="/trade/${t.id}/stop"><button class="btn-red" type="submit">Stop Out</button></form>
        </div>
      </div>`;
    }).join("");

    const html = renderPage(APP_NAME, `
      <div class="topbar">
        <div>
          <h1>${APP_NAME} <span class="db-badge">💾 PostgreSQL</span> <span class="broker-badge">📡 ${DEFAULT_BROKER}</span></h1>
          <div class="muted">Phase D1 · Persistent Database + Auto Price Tracking</div>
        </div>
        <div class="nav">
          <a href="/">Dashboard</a>
          <a href="/settings" style="color:#ffd978;font-weight:bold;">⚙ Settings</a>
          <a href="/alerts">Alerts JSON</a>
          <a href="/trades">Trades JSON</a>
          <a href="/executions">Executions JSON</a>
          <a href="/health">Health</a>
        </div>
      </div>

      <div class="tracker-bar">
        <div>
          <span class="label">Auto Tracker: </span>
          <span class="${trackerRunning ? "tracker-on" : "tracker-off"}">${trackerRunning ? "🟢 RUNNING" : "🔴 STOPPED"}</span>
        </div>
        <div class="muted" style="font-size:13px;">Last poll: ${lastPollStatus}</div>
        <div style="margin-left:auto;display:flex;gap:8px;">
          <form method="POST" action="/tracker/poll-now">
            <button type="submit" class="btn-cyan">Poll Now</button>
          </form>
          ${trackerRunning
            ? `<form method="POST" action="/tracker/stop"><button type="submit" class="btn-red">Stop Tracker</button></form>`
            : `<form method="POST" action="/tracker/start"><button type="submit" class="btn-green">Start Tracker</button></form>`}
        </div>
      </div>

      <div class="grid">
        <div class="card"><div class="label">Bridge Status</div><div class="big success">Online</div></div>
        <div class="card"><div class="label">Active Broker</div><div class="big" style="color:#ffd978;">${DEFAULT_BROKER}</div></div>
        <div class="card"><div class="label">Alerts Stored</div><div class="big">${countA}</div></div>
        <div class="card"><div class="label">Trades Stored</div><div class="big">${countT}</div></div>
        <div class="card"><div class="label">Executions Stored</div><div class="big">${countE}</div></div>
      </div>

      <div class="section card">
        <h2>Trade Risk Setting</h2>
        <form method="POST" action="/settings/usd-per-pip">
          <div><div class="label">USD value per pip</div>
          <input type="number" name="usdPerPip" min="0.01" step="0.01" value="${formatNumber(manager.usdPerPip, 2)}" required /></div>
          <button type="submit">Update</button>
        </form>
      </div>

      <div class="section card">
        <h2>Management Rules</h2>
        <form method="POST" action="/settings/manager-rules">
          <div class="checkbox-row">
            <label><input type="checkbox" name="beAfterTp1" ${manager.beAfterTp1 ? "checked" : ""} /> Move SL to BE after TP1</label>
            <label><input type="checkbox" name="autoCloseAtTp3" ${manager.autoCloseAtTp3 ? "checked" : ""} /> Auto close at TP3</label>
            <label><input type="checkbox" name="autoPriceTrack" ${manager.autoPriceTrack ? "checked" : ""} /> Auto Price Tracking ON</label>
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <div><div class="label">TP1 partial %</div>
            <input type="number" name="tp1PartialPct" min="0" max="100" step="1" value="${formatNumber(manager.tp1PartialPct, 0)}" /></div>
            <div><div class="label">TP2 partial %</div>
            <input type="number" name="tp2PartialPct" min="0" max="100" step="1" value="${formatNumber(manager.tp2PartialPct, 0)}" /></div>
            <div><div class="label">Poll interval (sec, min 30)</div>
            <input type="number" name="pollIntervalSec" min="30" max="300" step="10" value="${manager.pollIntervalSec}" /></div>
          </div>
          <div style="margin-top:12px;"><button type="submit">Update Rules</button></div>
        </form>
      </div>

      <div class="section">
        <h2>Pending Broker Executions</h2>
        <div class="trade-grid">${execCards || `<div class="card">No pending executions</div>`}</div>
      </div>

      <div class="section">
        <h2>Latest Trades</h2>
        <div class="trade-grid">${tradeCards || `<div class="card">No trades yet</div>`}</div>
      </div>

      <div class="section">
        <h2>Latest Alerts</h2>
        <table>
          <thead><tr>
            <th>Received</th><th>Type</th><th>Symbol</th><th>TF</th>
            <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Confidence</th>
          </tr></thead>
          <tbody>${alertsRows || `<tr><td colspan="10">No alerts yet</td></tr>`}</tbody>
        </table>
      </div>`);

    res.send(html);
  } catch (err) {
    console.error("Dashboard error:", err);
    res.status(500).send("Dashboard error: " + err.message);
  }
});

// ---------------------------------------------------------------------------
// TRACKER ROUTES
// ---------------------------------------------------------------------------
app.post("/tracker/start", async (req, res) => {
  await saveSetting("autoPriceTrack", 1);
  startPriceTracker();
  res.redirect("/");
});

app.post("/tracker/stop", async (req, res) => {
  await saveSetting("autoPriceTrack", 0);
  stopPriceTracker();
  res.redirect("/");
});

app.post("/tracker/poll-now", async (req, res) => {
  await runPriceTracker();
  res.redirect("/");
});

// ---------------------------------------------------------------------------
// SETTINGS ROUTES
// ---------------------------------------------------------------------------
app.post("/settings/usd-per-pip", async (req, res) => {
  const value = sanitizeNumber(req.body.usdPerPip);
  if (value === null || value <= 0) return res.status(400).send("Invalid value");
  await saveSetting("usdPerPip", value);
  res.redirect("/");
});

app.post("/settings/manager-rules", async (req, res) => {
  await saveSetting("beAfterTp1",     sanitizeBool(req.body.beAfterTp1, 0));
  await saveSetting("autoCloseAtTp3", sanitizeBool(req.body.autoCloseAtTp3, 0));
  await saveSetting("autoPriceTrack", sanitizeBool(req.body.autoPriceTrack, 0));
  await saveSetting("tp1PartialPct",  Math.max(0, Math.min(100, sanitizeNumber(req.body.tp1PartialPct, 0))));
  await saveSetting("tp2PartialPct",  Math.max(0, Math.min(100, sanitizeNumber(req.body.tp2PartialPct, 0))));
  await saveSetting("pollIntervalSec", Math.max(30, sanitizeNumber(req.body.pollIntervalSec, 60)));

  if (Number(getSetting("autoPriceTrack", 1)) === 1) startPriceTracker();
  else stopPriceTracker();
  res.redirect("/");
});

// ---------------------------------------------------------------------------
// JSON ROUTES
// ---------------------------------------------------------------------------
app.get("/alerts", async (req, res) => {
  const data = await dbGetAlerts(200);
  res.json(data.map(a => ({ ...a, raw_json: undefined })));
});

app.get("/trades", async (req, res) => {
  res.json(await dbGetTrades(200));
});

app.get("/executions", async (req, res) => {
  res.json(await dbGetExecutions(200));
});

// ---------------------------------------------------------------------------
// IG LOOKUP (still available for market search)
// ---------------------------------------------------------------------------
app.get("/ig/search", async (req, res) => {
  try {
    const term = String(req.query.term || "").trim();
    if (!term) return res.status(400).json({ ok: false, error: "Missing ?term=" });
    res.json({ ok: true, term, data: await searchMarkets(term) });
  } catch (err) { res.status(500).json({ ok: false, error: err.message }); }
});

app.get("/ig/market/:epic", async (req, res) => {
  try {
    res.json({ ok: true, epic: req.params.epic, data: await getMarketDetails(req.params.epic) });
  } catch (err) { res.status(500).json({ ok: false, error: err.message }); }
});

// ---------------------------------------------------------------------------
// MANUAL TRADE ACTIONS
// ---------------------------------------------------------------------------
app.post("/trade/:id/check-price", async (req, res) => {
  await applyTradeCheck(req.params.id, req.body.currentPrice, "manual");
  res.redirect("/");
});

app.post("/trade/:id/tp1-hit", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  const m = getManagerSettings();
  t.tp1Hit = 1; t.status = "TP1 HIT"; t.lastAction = "TP1 marked hit";
  if (!t.breakEvenMoved && m.beAfterTp1) {
    t.currentSl = t.entry; t.breakEvenMoved = 1;
    t.status = "TP1 HIT / BREAKEVEN MOVED"; t.lastAction = "TP1 hit — SL moved to entry";
  }
  recalcTrade(t); await dbUpdateTrade(t.id, t); res.redirect("/");
});

app.post("/trade/:id/tp2-hit", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  t.tp2Hit = 1; t.status = "TP2 HIT"; t.lastAction = "TP2 marked hit";
  recalcTrade(t); await dbUpdateTrade(t.id, t); res.redirect("/");
});

app.post("/trade/:id/tp3-hit", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  const m = getManagerSettings();
  t.tp3Hit = 1;
  t.status = m.autoCloseAtTp3 ? "TP3 HIT / CLOSED" : "TP3 HIT";
  t.lastAction = m.autoCloseAtTp3 ? "TP3 hit — trade closed" : "TP3 marked hit";
  recalcTrade(t); await dbUpdateTrade(t.id, t); res.redirect("/");
});

app.post("/trade/:id/move-sl-be", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  t.currentSl = t.entry; t.breakEvenMoved = 1;
  t.status = "BREAKEVEN MOVED"; t.lastAction = "SL manually moved to entry";
  recalcTrade(t); await dbUpdateTrade(t.id, t); res.redirect("/");
});

app.post("/trade/:id/close", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  t.status = "CLOSED"; t.lastAction = "Trade manually closed";
  await dbUpdateTrade(t.id, t); res.redirect("/");
});

app.post("/trade/:id/stop", async (req, res) => {
  const t = await dbGetTradeById(req.params.id);
  if (!t) return res.status(404).send("Trade not found");
  t.status = "STOPPED OUT"; t.lastAction = "Trade manually stopped out";
  await dbUpdateTrade(t.id, t); res.redirect("/");
});

// ---------------------------------------------------------------------------
// EXECUTION ROUTES
// ---------------------------------------------------------------------------
app.post("/execution/:id/approve", async (req, res) => {
  const exec = await dbGetExecutionById(req.params.id);
  if (!exec) return res.status(404).send("Execution not found");
  if (exec.status === "SENT") return res.redirect("/");

  const brokerName = exec.broker || DEFAULT_BROKER;

  try {
    exec.lastAction = `Sending to ${brokerName}...`;

    const result = await routePlaceMarketOrder(brokerName, {
      symbol:     exec.symbol,
      type:       exec.type,
      sl:         exec.sl,
      tp1:        exec.tp1,
      brokerSize: exec.brokerSize,
      strategyId: exec.strategyId
    });

    exec.status         = "SENT";
    exec.lastAction     = `Sent to ${brokerName} successfully`;
    exec.brokerResponse = result || null;
    exec.sentAt         = nowIso();

    // Link to trade
    const allTrades = await dbGetTrades(50);
    const linked = allTrades.find(t =>
      t.symbol === exec.symbol && t.type === exec.type &&
      (Date.now() - new Date(t.createdAt).getTime()) < 300000
    );
    if (linked) {
      exec.tradeId = linked.id;
      linked.status = "OPEN / TRACKING";
      linked.lastAction = `Approved — sent to ${brokerName}`;
      await dbUpdateTrade(linked.id, linked);
    }
  } catch (err) {
    exec.status     = "FAILED";
    exec.lastAction = err.message || "Broker execution failed";
    console.error(`[Approve] ${brokerName} error:`, err.message);
  }

  await dbUpdateExecution(exec.id, exec);
  res.redirect("/");
});

app.post("/execution/:id/cancel", async (req, res) => {
  const exec = await dbGetExecutionById(req.params.id);
  if (!exec) return res.status(404).send("Execution not found");
  exec.status = "CANCELLED"; exec.lastAction = "Execution manually cancelled";
  await dbUpdateExecution(exec.id, exec); res.redirect("/");
});

// ---------------------------------------------------------------------------
// WEBHOOK
// ---------------------------------------------------------------------------
app.post("/webhook/tradingview", async (req, res) => {
  try {
    const payload = req.body || {};
    if (sanitizeString(payload.secret) !== WEBHOOK_SECRET)
      return res.status(401).json({ ok: false, error: "Invalid secret" });

    // Determine broker — signal can override, else use DEFAULT_BROKER
    const brokerFromSignal = sanitizeString(payload.broker, "").toUpperCase();
    const activeBroker     = brokerFromSignal || DEFAULT_BROKER;

    const alertRecord = {
      id:          `alert_${Date.now()}`,
      receivedAt:  nowIso(),
      type:        sanitizeString(payload.type, "WAIT"),
      symbol:      sanitizeString(payload.symbol, ""),
      tf:          sanitizeString(payload.tf, ""),
      entry:       sanitizeNumber(payload.entry),
      sl:          sanitizeNumber(payload.sl),
      tp1:         sanitizeNumber(payload.tp1),
      tp2:         sanitizeNumber(payload.tp2),
      tp3:         sanitizeNumber(payload.tp3),
      confidence:  sanitizeNumber(payload.confidence),
      strategyId:  sanitizeString(payload.strategyId, "H2_DEFAULT"),
      broker:      activeBroker,
      accountMode: sanitizeString(payload.accountMode, "DEMO"),
      brokerSize:  sanitizeNumber(payload.brokerSize, null),
      raw_json:    JSON.stringify(payload)
    };

    await dbInsertAlert(alertRecord);

    const tradeType = String(alertRecord.type).toUpperCase();
    if (tradeType === "LONG" || tradeType === "SHORT") {
      const upp = Number(getSetting("usdPerPip", 0.1));
      const { entry, sl, tp1, tp2, tp3 } = alertRecord;
      const tradeId = `trade_${Date.now()}`;
      const trade = {
        id: tradeId, createdAt: nowIso(),
        type: alertRecord.type, symbol: alertRecord.symbol, tf: alertRecord.tf,
        entry, sl, tp1, tp2, tp3, confidence: alertRecord.confidence,
        strategyId: alertRecord.strategyId, status: "OPEN",
        broker: activeBroker,
        usdPerPip: upp,
        riskPips: calcPipDist(entry, sl),
        tp1Pips:  calcPipDist(entry, tp1),
        tp2Pips:  calcPipDist(entry, tp2),
        tp3Pips:  calcPipDist(entry, tp3),
        riskUsd:  calcMoney(calcPipDist(entry, sl), upp),
        tp1Usd:   calcMoney(calcPipDist(entry, tp1), upp),
        tp2Usd:   calcMoney(calcPipDist(entry, tp2), upp),
        tp3Usd:   calcMoney(calcPipDist(entry, tp3), upp),
        originalSl: sl, currentSl: sl,
        tp1Hit: 0, tp2Hit: 0, tp3Hit: 0, breakEvenMoved: 0,
        lastAction: "Trade created — waiting for execution",
        currentPrice: null, livePrice: null
      };
      await dbInsertTrade(trade);

      const execRecord = {
        id: `exec_${Date.now()}`, tradeId, createdAt: nowIso(),
        status:      EXECUTION_MODE === "AUTO" ? "QUEUED" : "PENDING APPROVAL",
        lastAction:  EXECUTION_MODE === "AUTO" ? "Queued for auto execution" : "Waiting for approval",
        strategyId:  alertRecord.strategyId,
        broker:      activeBroker,
        accountMode: alertRecord.accountMode,
        type:        alertRecord.type,
        symbol:      alertRecord.symbol,
        tf:          alertRecord.tf,
        entry, sl, tp1, tp2, tp3,
        confidence:  alertRecord.confidence,
        brokerSize:  alertRecord.brokerSize
      };
      await dbInsertExecution(execRecord);

      // ── AUTO MODE: fire immediately
      if (EXECUTION_MODE === "AUTO") {
        try {
          console.log(`[AUTO] Firing trade immediately — broker: ${activeBroker} symbol: ${execRecord.symbol} type: ${execRecord.type}`);

          const result = await routePlaceMarketOrder(activeBroker, {
            symbol:     execRecord.symbol,
            type:       execRecord.type,
            sl:         execRecord.sl,
            tp1:        execRecord.tp1,
            brokerSize: execRecord.brokerSize,
            strategyId: execRecord.strategyId
          });

          const ref = result.dealRef || result.positionId || result.orderId || "n/a";
          await dbUpdateExecution(execRecord.id, {
            ...execRecord,
            status:         "EXECUTED",
            lastAction:     `AUTO executed via ${activeBroker} — ref: ${ref}`,
            brokerResponse: result,
            dealRef:        ref
          });
          await dbUpdateTrade(tradeId, {
            ...trade,
            status:     "OPEN / TRACKING",
            lastAction: `AUTO executed via ${activeBroker} — ref: ${ref}`
          });
          console.log(`[AUTO] Trade executed — ref: ${ref}`);
        } catch (autoErr) {
          console.error("[AUTO] Execution failed:", autoErr.message);
          await dbUpdateExecution(execRecord.id, {
            ...execRecord,
            status:     "FAILED",
            lastAction: `AUTO execution failed: ${autoErr.message}`
          });
          await dbUpdateTrade(tradeId, {
            ...trade,
            lastAction: `AUTO execution FAILED: ${autoErr.message}`
          });
        }
      }
    }

    console.log("Webhook:", alertRecord.type, alertRecord.symbol, alertRecord.entry, "→", activeBroker);
    return res.json({ ok: true, message: "Webhook received", alertId: alertRecord.id });
  } catch (err) {
    console.error("Webhook error:", err);
    return res.status(500).json({ ok: false, error: "Server error" });
  }
});

// ---------------------------------------------------------------------------
// HEALTH
// ---------------------------------------------------------------------------
app.get("/health", async (req, res) => {
  try {
    const [cA, cT, cE] = await Promise.all([dbCountAlerts(), dbCountTrades(), dbCountExecutions()]);
    const manager = getManagerSettings();
    res.json({
      ok: true, app: APP_NAME, phase: "D1",
      database: "PostgreSQL — persistent",
      time: nowIso(),
      alertsStored: cA, tradesStored: cT, executionsStored: cE,
      executionMode: EXECUTION_MODE,
      defaultBroker: DEFAULT_BROKER,
      autoTracker: priceTrackerInterval ? "running" : "stopped",
      lastPollStatus, lastPollTime,
      ...manager
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ---------------------------------------------------------------------------
// SETTINGS PAGE
// ---------------------------------------------------------------------------
app.get("/settings", async (req, res) => {
  try {
    const s   = settingsCache;
    const msg = req.query.msg || "";
    const err = req.query.err || "";

    const epics = {};
    const epicSizes = {};
    for (const key of ["JP225","NAS100","DAX40","SP500","DOW","FTSE","AUS200"]) {
      epics[key]          = await dbGetSetting(`epic_${key}`, "");
      epicSizes[key]      = await dbGetSetting(`size_${key}`, 1);
      epics["tv_" + key]  = await dbGetSetting(`tv_${key}`, "");
    }

    const accountMode  = s.ig_account_mode || process.env.IG_ACCOUNT_MODE || "DEMO";
    const execMode     = process.env.EXECUTION_MODE || "APPROVAL";
    const igApiKey     = s.ig_api_key     ? "••••••••" + String(s.ig_api_key).slice(-6)     : (process.env.IG_API_KEY     ? "••••••••" + process.env.IG_API_KEY.slice(-6)     : "");
    const igIdentifier = s.ig_identifier  || process.env.IG_IDENTIFIER  || "";
    const igBaseUrl    = s.ig_base_url    || process.env.IG_BASE_URL    || "https://demo-api.ig.com/gateway/deal";

    const isLive = accountMode === "LIVE";
    const isAuto = execMode === "AUTO";

    const html = renderPage(APP_NAME + " · Settings", `
      <div class="topbar">
        <div>
          <h1>⚙ Settings</h1>
          <div class="muted">Phase C4.4 — Configure your system without touching code</div>
        </div>
        <div class="nav">
          <a href="/">← Dashboard</a>
          <a href="/settings" style="color:#ffd978;font-weight:bold;">⚙ Settings</a>
          <a href="/health">Health</a>
        </div>
      </div>

      ${msg ? `<div style="background:rgba(0,180,90,.15);border:1px solid #0c8a54;border-radius:10px;padding:12px 16px;margin-bottom:20px;color:#72f0ab;font-weight:bold;">✅ ${msg}</div>` : ""}
      ${err ? `<div style="background:rgba(220,70,70,.15);border:1px solid #b43737;border-radius:10px;padding:12px 16px;margin-bottom:20px;color:#ff9a9a;font-weight:bold;">❌ ${err}</div>` : ""}

      <!-- ACTIVE BROKER DISPLAY -->
      <div class="section card" style="margin-bottom:20px;">
        <h2>📡 Active Broker</h2>
        <div style="font-size:24px;font-weight:bold;color:#ffd978;">${DEFAULT_BROKER}</div>
        <div class="muted" style="margin-top:8px;font-size:13px;">To change broker: go to Railway → Variables → change DEFAULT_BROKER to IG or ICMARKETS → Update Variables</div>
      </div>

      <!-- EXECUTION MODE -->
      <div class="section card" style="margin-bottom:20px;">
        <h2>🚦 Execution Mode</h2>
        <p class="muted" style="margin:0 0 16px;">Controls whether trades fire automatically or wait for your approval on the dashboard.</p>
        <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;">
          <div style="background:${isAuto ? "#1a2a1a" : "#0c2a1a"};border:2px solid ${isAuto ? "#555" : "#0c8a54"};border-radius:12px;padding:16px 24px;flex:1;min-width:200px;">
            <div style="font-size:18px;font-weight:bold;color:${isAuto ? "#9aa4b2" : "#72f0ab"};">✅ APPROVAL MODE</div>
            <div style="color:#9aa4b2;font-size:13px;margin-top:6px;">Every signal appears on dashboard. You click Approve before it goes to broker. Safest option.</div>
            ${!isAuto ? `<div style="color:#72f0ab;font-size:12px;font-weight:bold;margin-top:8px;">← CURRENTLY ACTIVE</div>` : ""}
          </div>
          <div style="background:${isAuto ? "#2a1a0a" : "#1a1a1a"};border:2px solid ${isAuto ? "#c75000" : "#555"};border-radius:12px;padding:16px 24px;flex:1;min-width:200px;">
            <div style="font-size:18px;font-weight:bold;color:${isAuto ? "#ffd978" : "#9aa4b2"};">⚡ AUTO MODE</div>
            <div style="color:#9aa4b2;font-size:13px;margin-top:6px;">Trades fire the instant a signal arrives. No approval needed. Only use after extensive demo testing.</div>
            ${isAuto ? `<div style="color:#ffd978;font-size:12px;font-weight:bold;margin-top:8px;">← CURRENTLY ACTIVE</div>` : ""}
          </div>
        </div>
        <div style="background:rgba(220,70,70,.1);border:1px solid #b43737;border-radius:10px;padding:12px 16px;margin:16px 0;color:#ff9a9a;font-size:13px;">
          ⚠ <strong>Important:</strong> Execution mode is controlled by the EXECUTION_MODE variable in Railway. To change it: Railway → Variables → set EXECUTION_MODE to APPROVAL or AUTO.
        </div>
        <div style="color:#9aa4b2;font-size:13px;">Current mode: <strong style="color:#e8ecf1;">${execMode}</strong></div>
      </div>

      <!-- BROKER SETUP (IG) -->
      <div class="section card" style="margin-bottom:20px;">
        <h2>🔑 Broker Setup — IG Markets</h2>
        <p class="muted" style="margin:0 0 16px;">IG API credentials. Leave a field blank to keep the current value.</p>
        <form method="POST" action="/settings/broker">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:16px;">
            <div>
              <div class="label">Account Mode</div>
              <div style="display:flex;gap:8px;margin-top:4px;">
                <button type="submit" name="accountMode" value="DEMO"
                  style="flex:1;background:${!isLive ? "#0c8a54" : "#2a303a"};border:2px solid ${!isLive ? "#0c8a54" : "#3a4555"};color:white;border-radius:10px;padding:10px;cursor:pointer;font-weight:bold;">
                  ${!isLive ? "✅ " : ""}DEMO
                </button>
                <button type="submit" name="accountMode" value="LIVE"
                  style="flex:1;background:${isLive ? "#b43737" : "#2a303a"};border:2px solid ${isLive ? "#b43737" : "#3a4555"};color:white;border-radius:10px;padding:10px;cursor:pointer;font-weight:bold;">
                  ${isLive ? "🔴 " : ""}LIVE
                </button>
              </div>
              <div style="color:#9aa4b2;font-size:12px;margin-top:6px;">Current: <strong style="color:${isLive ? "#ff9a9a" : "#72f0ab"}">${accountMode}</strong></div>
            </div>
            <div>
              <div class="label">IG API Key</div>
              <input type="password" name="ig_api_key" placeholder="Leave blank to keep — ${igApiKey || "not set"}" style="width:100%;box-sizing:border-box;" />
            </div>
            <div>
              <div class="label">IG Username</div>
              <input type="text" name="ig_identifier" value="${igIdentifier}" autocomplete="off" style="width:100%;box-sizing:border-box;" />
            </div>
            <div>
              <div class="label">IG Password</div>
              <input type="password" name="ig_password" placeholder="Leave blank to keep current" style="width:100%;box-sizing:border-box;" />
            </div>
          </div>
          <div style="background:#0f1319;border:1px solid #2a303a;border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#9aa4b2;">
            API URL: <strong style="color:#e8ecf1;">${igBaseUrl}</strong>
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <button type="submit" name="action" value="save" class="btn-green">Save IG Settings</button>
            <button type="submit" name="action" value="test" class="btn-cyan">Test IG Connection</button>
          </div>
        </form>
      </div>

      <!-- INSTRUMENTS -->
      <div class="section card" style="margin-bottom:20px;">
        <h2>📊 Instruments</h2>
        <p class="muted" style="margin:0 0 4px;">Map each market to its IG epic code and TradingView ticker.</p>
        <form method="POST" action="/settings/instruments">
          <div style="overflow-x:auto;">
            <table style="min-width:750px;">
              <thead><tr>
                <th>Market</th>
                <th>TradingView Symbol</th>
                <th>IG Epic Code</th>
                <th>Default Size</th>
                <th>Status</th>
              </tr></thead>
              <tbody>
                ${[
                  {key:"JP225",  label:"Japan 225 (Nikkei)",  epicHint:"IX.D.NIKKEI.IFM.IP",  tvHint:"JP225, NI225"},
                  {key:"NAS100", label:"Nasdaq 100",          epicHint:"IX.D.NASDAQ.IFD.IP",  tvHint:"NAS100, NDX"},
                  {key:"DAX40",  label:"Germany 40 (DAX)",   epicHint:"IX.D.DAX.IFD.IP",     tvHint:"DAX, GER40"},
                  {key:"SP500",  label:"US 500 (S&P)",       epicHint:"IX.D.SPTRD.IFD.IP",   tvHint:"SPX500, US500"},
                  {key:"DOW",    label:"Wall Street (Dow)",  epicHint:"IX.D.DOW.IFD.IP",     tvHint:"US30, DJI"},
                  {key:"FTSE",   label:"UK 100 (FTSE)",      epicHint:"IX.D.FTSE.IFD.IP",    tvHint:"UK100, FTSE"},
                  {key:"AUS200", label:"Australia 200",      epicHint:"IX.D.ASX.IFD.IP",     tvHint:"AUS200, ASX200"}
                ].map(inst => {
                  const tvSym = epics["tv_" + inst.key] || "";
                  return `
                  <tr>
                    <td style="font-weight:bold;">${inst.label}</td>
                    <td><input type="text" name="tv_${inst.key}" value="${tvSym}" placeholder="${inst.tvHint}" style="width:160px;" /></td>
                    <td><input type="text" name="epic_${inst.key}" value="${epics[inst.key]}" placeholder="${inst.epicHint}" style="width:200px;" /></td>
                    <td><input type="number" name="size_${inst.key}" value="${epicSizes[inst.key] || 1}" min="0.1" step="0.1" style="width:80px;" /></td>
                    <td>${epics[inst.key] ? '<span class="pill pill-green">✅ Set</span>' : '<span class="pill pill-gray">Not set</span>'}</td>
                  </tr>`}).join("")}
              </tbody>
            </table>
          </div>
          <div style="margin-top:16px;">
            <button type="submit" class="btn-green">Save Instruments</button>
          </div>
        </form>
      </div>

      <!-- RISK MANAGEMENT -->
      <div class="section card" style="margin-bottom:20px;">
        <h2>💰 Risk Management</h2>
        <form method="POST" action="/settings/risk">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:16px;">
            <div>
              <div class="label">USD value per pip</div>
              <input type="number" name="usdPerPip" min="0.01" step="0.01" value="${settingsCache.usdPerPip || 0.1}" style="width:100%;box-sizing:border-box;" />
            </div>
            <div>
              <div class="label">Default broker size</div>
              <input type="number" name="defaultSize" min="0.1" step="0.1" value="${settingsCache.ig_default_size || 1}" style="width:100%;box-sizing:border-box;" />
            </div>
            <div>
              <div class="label">Auto price poll interval (seconds)</div>
              <input type="number" name="pollIntervalSec" min="30" max="300" step="10" value="${settingsCache.pollIntervalSec || 60}" style="width:100%;box-sizing:border-box;" />
            </div>
          </div>
          <div class="checkbox-row">
            <label><input type="checkbox" name="beAfterTp1" ${settingsCache.beAfterTp1 ? "checked" : ""} /> Move SL to breakeven after TP1 hit</label>
            <label><input type="checkbox" name="autoCloseAtTp3" ${settingsCache.autoCloseAtTp3 ? "checked" : ""} /> Auto close trade at TP3</label>
            <label><input type="checkbox" name="autoPriceTrack" ${settingsCache.autoPriceTrack ? "checked" : ""} /> Auto price tracking ON</label>
          </div>
          <button type="submit" class="btn-green">Save Risk Settings</button>
        </form>
      </div>

      <!-- WEBHOOK INFO -->
      <div class="section card">
        <h2>🔗 Webhook & System Info</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;">
          <div class="stat">
            <div class="k">Webhook URL</div>
            <div class="v" style="font-size:12px;word-break:break-all;">https://h2-webhook-bridge-production.up.railway.app/webhook/tradingview</div>
          </div>
          <div class="stat">
            <div class="k">Webhook Secret</div>
            <div class="v">${process.env.WEBHOOK_SECRET ? "••••••••" + process.env.WEBHOOK_SECRET.slice(-4) : "Not set"}</div>
          </div>
          <div class="stat">
            <div class="k">Execution Mode</div>
            <div class="v" style="color:${isAuto ? "#ffd978" : "#72f0ab"};">${execMode}</div>
          </div>
          <div class="stat">
            <div class="k">Active Broker</div>
            <div class="v" style="color:#ffd978;">${DEFAULT_BROKER}</div>
          </div>
          <div class="stat">
            <div class="k">Database</div>
            <div class="v" style="color:#98f0ff;">PostgreSQL — persistent</div>
          </div>
        </div>
      </div>
    `);
    res.send(html);
  } catch (err) {
    console.error("Settings page error:", err);
    res.status(500).send("Settings error: " + err.message);
  }
});

app.post("/settings/broker", async (req, res) => {
  try {
    const action = req.body.action || "save";
    const accountMode = req.body.accountMode || settingsCache.ig_account_mode || "DEMO";
    const baseUrl = accountMode === "LIVE"
      ? "https://api.ig.com/gateway/deal"
      : "https://demo-api.ig.com/gateway/deal";

    await saveSetting("ig_account_mode", accountMode);
    await saveSetting("ig_base_url", baseUrl);

    if (req.body.ig_api_key && req.body.ig_api_key.trim())
      await saveSetting("ig_api_key", req.body.ig_api_key.trim());
    if (req.body.ig_identifier && req.body.ig_identifier.trim())
      await saveSetting("ig_identifier", req.body.ig_identifier.trim());
    if (req.body.ig_password && req.body.ig_password.trim())
      await saveSetting("ig_password", req.body.ig_password.trim());

    await loadBrokerConfig();

    if (action === "test") {
      try {
        const { createSession } = require("./brokers/ig");
        await createSession();
        res.redirect("/settings?msg=IG+connection+successful+—+" + accountMode + "+account+connected");
      } catch (testErr) {
        res.redirect("/settings?err=IG+connection+failed:+" + encodeURIComponent(testErr.message));
      }
    } else {
      res.redirect("/settings?msg=Broker+settings+saved+successfully");
    }
  } catch (err) {
    res.redirect("/settings?err=" + encodeURIComponent(err.message));
  }
});

app.post("/settings/instruments", async (req, res) => {
  try {
    const keys = ["JP225","NAS100","DAX40","SP500","DOW","FTSE","AUS200"];
    for (const key of keys) {
      if (req.body[`epic_${key}`] !== undefined)
        await saveSetting(`epic_${key}`, req.body[`epic_${key}`].trim());
      if (req.body[`size_${key}`] !== undefined)
        await saveSetting(`size_${key}`, Number(req.body[`size_${key}`]) || 1);
      if (req.body[`tv_${key}`] !== undefined)
        await saveSetting(`tv_${key}`, req.body[`tv_${key}`].trim().toUpperCase());
    }
    await loadBrokerConfig();
    res.redirect("/settings?msg=Instruments+saved+successfully");
  } catch (err) {
    res.redirect("/settings?err=" + encodeURIComponent(err.message));
  }
});

app.post("/settings/risk", async (req, res) => {
  try {
    await saveSetting("usdPerPip",       Math.max(0.001, Number(req.body.usdPerPip) || 0.1));
    await saveSetting("ig_default_size", Math.max(0.1, Number(req.body.defaultSize) || 1));
    await saveSetting("pollIntervalSec", Math.max(30, Number(req.body.pollIntervalSec) || 60));
    await saveSetting("beAfterTp1",      req.body.beAfterTp1     ? 1 : 0);
    await saveSetting("autoCloseAtTp3",  req.body.autoCloseAtTp3 ? 1 : 0);
    await saveSetting("autoPriceTrack",  req.body.autoPriceTrack  ? 1 : 0);

    await loadBrokerConfig();
    if (Number(getSetting("autoPriceTrack", 1)) === 1) startPriceTracker();
    else stopPriceTracker();

    res.redirect("/settings?msg=Risk+settings+saved+successfully");
  } catch (err) {
    res.redirect("/settings?err=" + encodeURIComponent(err.message));
  }
});

// ---------------------------------------------------------------------------
// STARTUP
// ---------------------------------------------------------------------------
async function start() {
  try {
    await initDb();
    await loadSettingsCache();
    await loadBrokerConfig();
    console.log("[DB] Settings loaded from PostgreSQL");
    console.log("[Broker] Active broker:", DEFAULT_BROKER);

    if (Number(getSetting("autoPriceTrack", 1)) === 1) startPriceTracker();

    app.listen(PORT, () => {
      console.log(`${APP_NAME} listening on port ${PORT}`);
      console.log(`Database: PostgreSQL`);
      console.log(`Broker: ${DEFAULT_BROKER}`);
      console.log(`Execution mode: ${EXECUTION_MODE}`);
      console.log(`Auto tracker: ${priceTrackerInterval ? "started" : "stopped"}`);
    });
  } catch (err) {
    console.error("Startup error:", err);
    process.exit(1);
  }
}

start();