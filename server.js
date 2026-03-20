const express = require("express");
const dotenv = require("dotenv");
const sqlite3 = require("sqlite3").verbose();
const path = require("path");

dotenv.config();

const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

const PORT = process.env.PORT || 3000;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || "changeme";
const APP_NAME = process.env.APP_NAME || "H2 Webhook Bridge";

const dbPath = path.join(__dirname, "bridge.db");
const db = new sqlite3.Database(dbPath);

// -----------------------------------------------------------------------------
// DB HELPERS
// -----------------------------------------------------------------------------
function dbRun(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.run(sql, params, function (err) {
      if (err) reject(err);
      else resolve(this);
    });
  });
}

function dbGet(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.get(sql, params, (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
}

function dbAll(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.all(sql, params, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
}

// -----------------------------------------------------------------------------
// UTILITIES
// -----------------------------------------------------------------------------
function nowIso() {
  return new Date().toISOString();
}

function sanitizeString(value, fallback = "") {
  return typeof value === "string" ? value.trim() : fallback;
}

function sanitizeNumber(value, fallback = null) {
  if (value === null || value === undefined || value === "") return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function sanitizeBoolFromForm(value, fallback = 0) {
  if (value === undefined || value === null) return fallback;
  if (value === "on" || value === "true" || value === true || value === 1 || value === "1") return 1;
  return 0;
}

function formatNumber(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(decimals);
}

function calcPipDistance(a, b) {
  if (a === null || b === null || a === undefined || b === undefined) return null;
  return Math.abs(Number(a) - Number(b));
}

function calcMoney(distance, usdPerPip) {
  if (distance === null || usdPerPip === null || usdPerPip === undefined) return null;
  return Number(distance) * Number(usdPerPip);
}

async function getSetting(key, fallback) {
  const row = await dbGet(`SELECT value FROM settings WHERE key = ?`, [key]);
  return row ? row.value : fallback;
}

async function setSetting(key, value) {
  await dbRun(
    `INSERT INTO settings (key, value)
     VALUES (?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
    [key, String(value)]
  );
}

async function getUsdPerPip() {
  return Number(await getSetting("usdPerPip", "0.1"));
}

async function setUsdPerPip(value) {
  await setSetting("usdPerPip", value);
}

async function getManagerSettings() {
  return {
    usdPerPip: Number(await getSetting("usdPerPip", "0.1")),
    beAfterTp1: Number(await getSetting("beAfterTp1", "1")),
    autoCloseAtTp3: Number(await getSetting("autoCloseAtTp3", "1")),
    tp1PartialPct: Number(await getSetting("tp1PartialPct", "0")),
    tp2PartialPct: Number(await getSetting("tp2PartialPct", "0"))
  };
}

function typePill(type) {
  const t = String(type || "").toUpperCase();
  if (t === "LONG" || t === "BUY") return `<span class="pill pill-green">${t}</span>`;
  if (t === "SHORT" || t === "SELL") return `<span class="pill pill-red">${t}</span>`;
  if (t === "WAIT" || t === "DANGER") return `<span class="pill pill-yellow">${t}</span>`;
  return `<span class="pill pill-gray">${t || "UNKNOWN"}</span>`;
}

function statusPill(status) {
  const s = String(status || "").toUpperCase();
  if (s.includes("OPEN")) return `<span class="pill pill-green">${s}</span>`;
  if (s.includes("TP")) return `<span class="pill pill-blue">${s}</span>`;
  if (s.includes("BREAKEVEN")) return `<span class="pill pill-cyan">${s}</span>`;
  if (s.includes("CLOSED")) return `<span class="pill pill-gray">${s}</span>`;
  if (s.includes("STOP")) return `<span class="pill pill-red">${s}</span>`;
  return `<span class="pill pill-gray">${s || "UNKNOWN"}</span>`;
}

async function recalcTradeById(tradeId) {
  const t = await dbGet(`SELECT * FROM trades WHERE id = ?`, [tradeId]);
  if (!t) return;

  const usdPerPip = await getUsdPerPip();

  const riskPips = calcPipDistance(t.entry, t.currentSl);
  const tp1Pips = calcPipDistance(t.entry, t.tp1);
  const tp2Pips = calcPipDistance(t.entry, t.tp2);
  const tp3Pips = calcPipDistance(t.entry, t.tp3);

  await dbRun(
    `UPDATE trades
     SET usdPerPip = ?, riskPips = ?, tp1Pips = ?, tp2Pips = ?, tp3Pips = ?,
         riskUsd = ?, tp1Usd = ?, tp2Usd = ?, tp3Usd = ?
     WHERE id = ?`,
    [
      usdPerPip,
      riskPips,
      tp1Pips,
      tp2Pips,
      tp3Pips,
      calcMoney(riskPips, usdPerPip),
      calcMoney(tp1Pips, usdPerPip),
      calcMoney(tp2Pips, usdPerPip),
      calcMoney(tp3Pips, usdPerPip),
      tradeId
    ]
  );
}

async function recalcAllTrades() {
  const trades = await dbAll(`SELECT id FROM trades ORDER BY createdAt DESC`);
  for (const t of trades) {
    await recalcTradeById(t.id);
  }
}

async function applyTradeCheck(tradeId, currentPriceInput) {
  const trade = await dbGet(`SELECT * FROM trades WHERE id = ?`, [tradeId]);
  if (!trade) return;

  const currentPrice = sanitizeNumber(currentPriceInput);
  if (currentPrice === null) return;

  const manager = await getManagerSettings();

  let status = trade.status || "OPEN";
  let lastAction = `Checked price ${currentPrice}`;
  let currentSl = trade.currentSl;
  let tp1Hit = Number(trade.tp1Hit || 0);
  let tp2Hit = Number(trade.tp2Hit || 0);
  let tp3Hit = Number(trade.tp3Hit || 0);
  let breakEvenMoved = Number(trade.breakEvenMoved || 0);

  const type = String(trade.type || "").toUpperCase();

  if (type === "LONG") {
    if (status !== "CLOSED" && status !== "STOPPED OUT" && status !== "TP3 HIT / CLOSED") {
      if (!tp1Hit && currentPrice >= Number(trade.tp1)) {
        tp1Hit = 1;
        status = "TP1 HIT";
        lastAction = `TP1 hit at ${currentPrice}`;

        if (manager.tp1PartialPct > 0) {
          lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
        }

        if (!breakEvenMoved && manager.beAfterTp1) {
          currentSl = Number(trade.entry);
          breakEvenMoved = 1;
          status = "TP1 HIT / BREAKEVEN MOVED";
          lastAction = `TP1 hit at ${currentPrice} and SL moved to entry`;

          if (manager.tp1PartialPct > 0) {
            lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
          }
        }
      }

      if (!tp2Hit && currentPrice >= Number(trade.tp2)) {
        tp2Hit = 1;
        status = "TP2 HIT";
        lastAction = `TP2 hit at ${currentPrice}`;

        if (manager.tp2PartialPct > 0) {
          lastAction += ` | Partial close ${manager.tp2PartialPct}% placeholder`;
        }
      }

      if (!tp3Hit && currentPrice >= Number(trade.tp3)) {
        tp3Hit = 1;
        if (manager.autoCloseAtTp3) {
          status = "TP3 HIT / CLOSED";
          lastAction = `TP3 hit at ${currentPrice} and trade closed`;
        } else {
          status = "TP3 HIT";
          lastAction = `TP3 hit at ${currentPrice}`;
        }
      }

      if (status !== "TP3 HIT / CLOSED" && currentPrice <= Number(currentSl)) {
        status = "STOPPED OUT";
        lastAction = `Stopped out at ${currentPrice}`;
      }
    }
  }

  if (type === "SHORT") {
    if (status !== "CLOSED" && status !== "STOPPED OUT" && status !== "TP3 HIT / CLOSED") {
      if (!tp1Hit && currentPrice <= Number(trade.tp1)) {
        tp1Hit = 1;
        status = "TP1 HIT";
        lastAction = `TP1 hit at ${currentPrice}`;

        if (manager.tp1PartialPct > 0) {
          lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
        }

        if (!breakEvenMoved && manager.beAfterTp1) {
          currentSl = Number(trade.entry);
          breakEvenMoved = 1;
          status = "TP1 HIT / BREAKEVEN MOVED";
          lastAction = `TP1 hit at ${currentPrice} and SL moved to entry`;

          if (manager.tp1PartialPct > 0) {
            lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
          }
        }
      }

      if (!tp2Hit && currentPrice <= Number(trade.tp2)) {
        tp2Hit = 1;
        status = "TP2 HIT";
        lastAction = `TP2 hit at ${currentPrice}`;

        if (manager.tp2PartialPct > 0) {
          lastAction += ` | Partial close ${manager.tp2PartialPct}% placeholder`;
        }
      }

      if (!tp3Hit && currentPrice <= Number(trade.tp3)) {
        tp3Hit = 1;
        if (manager.autoCloseAtTp3) {
          status = "TP3 HIT / CLOSED";
          lastAction = `TP3 hit at ${currentPrice} and trade closed`;
        } else {
          status = "TP3 HIT";
          lastAction = `TP3 hit at ${currentPrice}`;
        }
      }

      if (status !== "TP3 HIT / CLOSED" && currentPrice >= Number(currentSl)) {
        status = "STOPPED OUT";
        lastAction = `Stopped out at ${currentPrice}`;
      }
    }
  }

  await dbRun(
    `UPDATE trades
     SET currentPrice = ?, currentSl = ?, tp1Hit = ?, tp2Hit = ?, tp3Hit = ?,
         breakEvenMoved = ?, status = ?, lastAction = ?
     WHERE id = ?`,
    [currentPrice, currentSl, tp1Hit, tp2Hit, tp3Hit, breakEvenMoved, status, lastAction, tradeId]
  );

  await recalcTradeById(tradeId);
}

function renderPage(title, content) {
  return `
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <title>${title}</title>
      <style>
        body {
          margin: 0;
          font-family: Arial, sans-serif;
          background: #0f1115;
          color: #e8ecf1;
        }
        .wrap {
          max-width: 1500px;
          margin: 0 auto;
          padding: 24px;
        }
        h1, h2 {
          margin-top: 0;
        }
        .topbar {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 16px;
          margin-bottom: 24px;
          flex-wrap: wrap;
        }
        .muted {
          color: #9aa4b2;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 16px;
          margin-bottom: 24px;
        }
        .card {
          background: #171a21;
          border: 1px solid #2a303a;
          border-radius: 14px;
          padding: 16px;
          box-shadow: 0 4px 18px rgba(0,0,0,0.25);
        }
        .big {
          font-size: 28px;
          font-weight: bold;
        }
        .label {
          color: #9aa4b2;
          font-size: 13px;
          margin-bottom: 8px;
        }
        .pill {
          display: inline-block;
          padding: 6px 10px;
          border-radius: 999px;
          font-size: 12px;
          font-weight: bold;
        }
        .pill-green { background: rgba(0, 180, 90, 0.18); color: #72f0ab; }
        .pill-red { background: rgba(220, 70, 70, 0.18); color: #ff9a9a; }
        .pill-yellow { background: rgba(220, 180, 70, 0.18); color: #ffd978; }
        .pill-gray { background: rgba(160, 160, 160, 0.15); color: #d3d7de; }
        .pill-blue { background: rgba(70, 120, 220, 0.18); color: #9fc2ff; }
        .pill-cyan { background: rgba(60, 190, 210, 0.18); color: #98f0ff; }
        .section {
          margin-bottom: 24px;
        }
        table {
          width: 100%;
          border-collapse: collapse;
          background: #171a21;
          border: 1px solid #2a303a;
          border-radius: 14px;
          overflow: hidden;
        }
        th, td {
          text-align: left;
          padding: 12px 10px;
          border-bottom: 1px solid #252b34;
          font-size: 14px;
          vertical-align: top;
        }
        th {
          background: #1d222c;
          color: #cfd7e3;
          font-size: 13px;
        }
        .trade-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
          gap: 16px;
        }
        .trade-card {
          background: #171a21;
          border: 1px solid #2a303a;
          border-radius: 14px;
          padding: 16px;
        }
        .trade-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
          margin-bottom: 12px;
        }
        .trade-symbol {
          font-size: 20px;
          font-weight: bold;
        }
        .trade-meta {
          color: #9aa4b2;
          font-size: 13px;
          margin-bottom: 14px;
        }
        .trade-stats {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 10px;
          margin-bottom: 14px;
        }
        .stat {
          background: #11151c;
          border: 1px solid #232a34;
          border-radius: 10px;
          padding: 10px;
        }
        .stat .k {
          color: #9aa4b2;
          font-size: 12px;
          margin-bottom: 4px;
        }
        .stat .v {
          font-weight: bold;
          font-size: 14px;
        }
        form {
          display: flex;
          gap: 10px;
          align-items: end;
          flex-wrap: wrap;
        }
        input[type="number"] {
          background: #0f1319;
          border: 1px solid #2a303a;
          color: #e8ecf1;
          border-radius: 10px;
          padding: 10px 12px;
          font-size: 14px;
          width: 140px;
        }
        .checkbox-row {
          display: flex;
          gap: 18px;
          flex-wrap: wrap;
          align-items: center;
          margin: 8px 0 14px;
        }
        .checkbox-row label {
          display: flex;
          align-items: center;
          gap: 6px;
          color: #dce3ed;
          font-size: 14px;
        }
        button {
          background: #3a7afe;
          color: white;
          border: 0;
          border-radius: 10px;
          padding: 10px 14px;
          font-size: 13px;
          cursor: pointer;
        }
        button:hover {
          background: #2f68d7;
        }
        .btn-row {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-top: 8px;
        }
        .btn-cyan { background: #0b8ea2; }
        .btn-red { background: #b43737; }
        .btn-gray { background: #555; }
        .btn-green { background: #0c8a54; }
        a {
          color: #8db8ff;
          text-decoration: none;
        }
        .nav {
          display: flex;
          gap: 14px;
          flex-wrap: wrap;
        }
        .success {
          color: #72f0ab;
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        ${content}
      </div>
    </body>
    </html>
  `;
}

// -----------------------------------------------------------------------------
// DB INIT / MIGRATION
// -----------------------------------------------------------------------------
async function initDb() {
  await dbRun(`
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT
    )
  `);

  await dbRun(`
    CREATE TABLE IF NOT EXISTS alerts (
      id TEXT PRIMARY KEY,
      receivedAt TEXT,
      bridge TEXT,
      mode TEXT,
      type TEXT,
      symbol TEXT,
      tf TEXT,
      entry REAL,
      sl REAL,
      tp1 REAL,
      tp2 REAL,
      tp3 REAL,
      confidence REAL,
      wa_route TEXT,
      broker_route TEXT,
      raw_json TEXT
    )
  `);

  await dbRun(`
    CREATE TABLE IF NOT EXISTS trades (
      id TEXT PRIMARY KEY,
      createdAt TEXT,
      bridge TEXT,
      mode TEXT,
      type TEXT,
      symbol TEXT,
      tf TEXT,
      entry REAL,
      sl REAL,
      tp1 REAL,
      tp2 REAL,
      tp3 REAL,
      confidence REAL,
      wa_route TEXT,
      broker_route TEXT,
      status TEXT,
      usdPerPip REAL,
      riskPips REAL,
      tp1Pips REAL,
      tp2Pips REAL,
      tp3Pips REAL,
      riskUsd REAL,
      tp1Usd REAL,
      tp2Usd REAL,
      tp3Usd REAL,
      originalSl REAL,
      currentSl REAL,
      tp1Hit INTEGER DEFAULT 0,
      tp2Hit INTEGER DEFAULT 0,
      tp3Hit INTEGER DEFAULT 0,
      breakEvenMoved INTEGER DEFAULT 0,
      lastAction TEXT,
      currentPrice REAL
    )
  `);

  const defaults = [
    ["usdPerPip", "0.1"],
    ["beAfterTp1", "1"],
    ["autoCloseAtTp3", "1"],
    ["tp1PartialPct", "0"],
    ["tp2PartialPct", "0"]
  ];

  for (const [key, value] of defaults) {
    const row = await dbGet(`SELECT value FROM settings WHERE key = ?`, [key]);
    if (!row) await setSetting(key, value);
  }

  const columnChecks = [
    { name: "originalSl", sql: `ALTER TABLE trades ADD COLUMN originalSl REAL` },
    { name: "currentSl", sql: `ALTER TABLE trades ADD COLUMN currentSl REAL` },
    { name: "tp1Hit", sql: `ALTER TABLE trades ADD COLUMN tp1Hit INTEGER DEFAULT 0` },
    { name: "tp2Hit", sql: `ALTER TABLE trades ADD COLUMN tp2Hit INTEGER DEFAULT 0` },
    { name: "tp3Hit", sql: `ALTER TABLE trades ADD COLUMN tp3Hit INTEGER DEFAULT 0` },
    { name: "breakEvenMoved", sql: `ALTER TABLE trades ADD COLUMN breakEvenMoved INTEGER DEFAULT 0` },
    { name: "lastAction", sql: `ALTER TABLE trades ADD COLUMN lastAction TEXT` },
    { name: "currentPrice", sql: `ALTER TABLE trades ADD COLUMN currentPrice REAL` }
  ];

  const cols = await dbAll(`PRAGMA table_info(trades)`);
  const existing = cols.map(c => c.name);

  for (const c of columnChecks) {
    if (!existing.includes(c.name)) {
      await dbRun(c.sql);
    }
  }

  await dbRun(`UPDATE trades SET originalSl = sl WHERE originalSl IS NULL`);
  await dbRun(`UPDATE trades SET currentSl = sl WHERE currentSl IS NULL`);
  await dbRun(`UPDATE trades SET lastAction = 'Trade created' WHERE lastAction IS NULL`);
}

// -----------------------------------------------------------------------------
// ROUTES
// -----------------------------------------------------------------------------
app.get("/health", async (req, res) => {
  const alertsCount = await dbGet(`SELECT COUNT(*) as count FROM alerts`);
  const tradesCount = await dbGet(`SELECT COUNT(*) as count FROM trades`);
  const manager = await getManagerSettings();

  res.json({
    ok: true,
    app: APP_NAME,
    time: nowIso(),
    alertsStored: alertsCount.count,
    tradesStored: tradesCount.count,
    ...manager
  });
});

app.get("/", async (req, res) => {
  const latestAlerts = await dbAll(`SELECT * FROM alerts ORDER BY receivedAt DESC LIMIT 10`);
  const latestTrades = await dbAll(`SELECT * FROM trades ORDER BY createdAt DESC LIMIT 12`);
  const alertsCount = await dbGet(`SELECT COUNT(*) as count FROM alerts`);
  const tradesCount = await dbGet(`SELECT COUNT(*) as count FROM trades`);
  const manager = await getManagerSettings();

  const alertsRows = latestAlerts.map((a) => `
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
    </tr>
  `).join("");

  const tradeCards = latestTrades.map((t) => `
    <div class="trade-card">
      <div class="trade-header">
        <div>
          <div class="trade-symbol">${t.symbol || "-"}</div>
          <div class="trade-meta">${t.createdAt} · TF ${t.tf || "-"}</div>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          ${typePill(t.type)}
          ${statusPill(t.status)}
        </div>
      </div>

      <div class="trade-stats">
        <div class="stat"><div class="k">Entry</div><div class="v">${formatNumber(t.entry, 2)}</div></div>
        <div class="stat"><div class="k">Original SL</div><div class="v">${formatNumber(t.originalSl, 2)}</div></div>

        <div class="stat"><div class="k">Current SL</div><div class="v">${formatNumber(t.currentSl, 2)}</div></div>
        <div class="stat"><div class="k">Current Price</div><div class="v">${formatNumber(t.currentPrice, 2)}</div></div>

        <div class="stat"><div class="k">TP1</div><div class="v">${formatNumber(t.tp1, 2)}</div></div>
        <div class="stat"><div class="k">TP2</div><div class="v">${formatNumber(t.tp2, 2)}</div></div>

        <div class="stat"><div class="k">TP3</div><div class="v">${formatNumber(t.tp3, 2)}</div></div>
        <div class="stat"><div class="k">Confidence</div><div class="v">${t.confidence ?? "-"}%</div></div>

        <div class="stat"><div class="k">USD per Pip</div><div class="v">$${formatNumber(t.usdPerPip, 2)}</div></div>
        <div class="stat"><div class="k">Risk (USD)</div><div class="v">$${formatNumber(t.riskUsd, 2)}</div></div>

        <div class="stat"><div class="k">TP1 Value</div><div class="v">$${formatNumber(t.tp1Usd, 2)}</div></div>
        <div class="stat"><div class="k">TP2 Value</div><div class="v">$${formatNumber(t.tp2Usd, 2)}</div></div>

        <div class="stat"><div class="k">TP3 Value</div><div class="v">$${formatNumber(t.tp3Usd, 2)}</div></div>
        <div class="stat"><div class="k">Break-even moved</div><div class="v">${t.breakEvenMoved ? "Yes" : "No"}</div></div>

        <div class="stat"><div class="k">TP Hits</div><div class="v">TP1:${t.tp1Hit ? "Y" : "N"} / TP2:${t.tp2Hit ? "Y" : "N"} / TP3:${t.tp3Hit ? "Y" : "N"}</div></div>
        <div class="stat"><div class="k">Last Action</div><div class="v">${t.lastAction || "-"}</div></div>
      </div>

      <div class="section card" style="margin-bottom:14px;">
        <div class="label">Manual Current Price Check</div>
        <form method="POST" action="/trade/${t.id}/check-price">
          <input type="number" name="currentPrice" step="0.01" placeholder="Enter current price" required />
          <button class="btn-green" type="submit">Check Trade</button>
        </form>
      </div>

      <div class="btn-row">
        <form method="POST" action="/trade/${t.id}/tp1-hit">
          <button type="submit">TP1 Hit</button>
        </form>
        <form method="POST" action="/trade/${t.id}/tp2-hit">
          <button type="submit">TP2 Hit</button>
        </form>
        <form method="POST" action="/trade/${t.id}/tp3-hit">
          <button type="submit">TP3 Hit</button>
        </form>
        <form method="POST" action="/trade/${t.id}/move-sl-be">
          <button class="btn-cyan" type="submit">Move SL to BE</button>
        </form>
        <form method="POST" action="/trade/${t.id}/close">
          <button class="btn-gray" type="submit">Close</button>
        </form>
        <form method="POST" action="/trade/${t.id}/stop">
          <button class="btn-red" type="submit">Stop Out</button>
        </form>
      </div>
    </div>
  `).join("");

  const html = renderPage(APP_NAME, `
    <div class="topbar">
      <div>
        <h1>${APP_NAME}</h1>
        <div class="muted">Phase C3 · configurable management rules</div>
      </div>
      <div class="nav">
        <a href="/">Dashboard</a>
        <a href="/alerts">Alerts JSON</a>
        <a href="/trades">Trades JSON</a>
        <a href="/health">Health</a>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">Bridge Status</div>
        <div class="big success">Online</div>
      </div>
      <div class="card">
        <div class="label">Alerts Stored</div>
        <div class="big">${alertsCount.count}</div>
      </div>
      <div class="card">
        <div class="label">Trades Stored</div>
        <div class="big">${tradesCount.count}</div>
      </div>
      <div class="card">
        <div class="label">USD per Pip</div>
        <div class="big">$${formatNumber(manager.usdPerPip, 2)}</div>
      </div>
    </div>

    <div class="section card">
      <h2>Trade Risk Setting</h2>
      <div class="muted" style="margin-bottom:12px;">
        Current assumption: <strong>1 point = 1 pip</strong>
      </div>
      <form method="POST" action="/settings/usd-per-pip">
        <div>
          <div class="label">USD value per pip</div>
          <input type="number" name="usdPerPip" min="0.01" step="0.01" value="${formatNumber(manager.usdPerPip, 2)}" required />
        </div>
        <button type="submit">Update Setting</button>
      </form>
    </div>

    <div class="section card">
      <h2>Management Rules</h2>
      <form method="POST" action="/settings/manager-rules">
        <div class="checkbox-row">
          <label><input type="checkbox" name="beAfterTp1" ${manager.beAfterTp1 ? "checked" : ""} /> Move SL to BE after TP1</label>
          <label><input type="checkbox" name="autoCloseAtTp3" ${manager.autoCloseAtTp3 ? "checked" : ""} /> Auto close at TP3</label>
        </div>
        <div style="display:flex; gap:10px; flex-wrap:wrap;">
          <div>
            <div class="label">TP1 partial close %</div>
            <input type="number" name="tp1PartialPct" min="0" max="100" step="1" value="${formatNumber(manager.tp1PartialPct, 0)}" />
          </div>
          <div>
            <div class="label">TP2 partial close %</div>
            <input type="number" name="tp2PartialPct" min="0" max="100" step="1" value="${formatNumber(manager.tp2PartialPct, 0)}" />
          </div>
        </div>
        <div style="margin-top:12px;">
          <button type="submit">Update Management Rules</button>
        </div>
      </form>
    </div>

    <div class="section">
      <h2>Latest Trades</h2>
      <div class="trade-grid">
        ${tradeCards || `<div class="card">No trades yet</div>`}
      </div>
    </div>

    <div class="section">
      <h2>Latest Alerts</h2>
      <table>
        <thead>
          <tr>
            <th>Received</th>
            <th>Type</th>
            <th>Symbol</th>
            <th>TF</th>
            <th>Entry</th>
            <th>SL</th>
            <th>TP1</th>
            <th>TP2</th>
            <th>TP3</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody>
          ${alertsRows || `<tr><td colspan="10">No alerts yet</td></tr>`}
        </tbody>
      </table>
    </div>
  `);

  res.send(html);
});

app.post("/settings/usd-per-pip", async (req, res) => {
  const value = sanitizeNumber(req.body.usdPerPip);
  if (value === null || value <= 0) {
    return res.status(400).send("Invalid usdPerPip value");
  }

  await setUsdPerPip(value);
  await recalcAllTrades();
  res.redirect("/");
});

app.post("/settings/manager-rules", async (req, res) => {
  const beAfterTp1 = sanitizeBoolFromForm(req.body.beAfterTp1, 0);
  const autoCloseAtTp3 = sanitizeBoolFromForm(req.body.autoCloseAtTp3, 0);
  const tp1PartialPct = Math.max(0, Math.min(100, sanitizeNumber(req.body.tp1PartialPct, 0)));
  const tp2PartialPct = Math.max(0, Math.min(100, sanitizeNumber(req.body.tp2PartialPct, 0)));

  await setSetting("beAfterTp1", beAfterTp1);
  await setSetting("autoCloseAtTp3", autoCloseAtTp3);
  await setSetting("tp1PartialPct", tp1PartialPct);
  await setSetting("tp2PartialPct", tp2PartialPct);

  res.redirect("/");
});

app.get("/alerts", async (req, res) => {
  const rows = await dbAll(`SELECT * FROM alerts ORDER BY receivedAt DESC`);
  const safeAlerts = rows.map((a) => {
    const raw = a.raw_json ? JSON.parse(a.raw_json) : null;
    return {
      ...a,
      raw: raw
        ? {
            ...raw,
            secret: raw.secret ? "***MASKED***" : undefined
          }
        : undefined,
      raw_json: undefined
    };
  });

  res.json(safeAlerts);
});

app.get("/trades", async (req, res) => {
  const rows = await dbAll(`SELECT * FROM trades ORDER BY createdAt DESC`);
  res.json(rows);
});

// -----------------------------------------------------------------------------
// MANUAL TRADE ACTIONS
// -----------------------------------------------------------------------------
app.post("/trade/:id/check-price", async (req, res) => {
  await applyTradeCheck(req.params.id, req.body.currentPrice);
  res.redirect("/");
});

app.post("/trade/:id/tp1-hit", async (req, res) => {
  const id = req.params.id;
  const trade = await dbGet(`SELECT * FROM trades WHERE id = ?`, [id]);
  if (!trade) return res.status(404).send("Trade not found");

  const manager = await getManagerSettings();

  let currentSl = trade.currentSl;
  let breakEvenMoved = trade.breakEvenMoved;
  let status = "TP1 HIT";
  let lastAction = "TP1 marked hit";

  if (manager.tp1PartialPct > 0) {
    lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
  }

  if (!trade.breakEvenMoved && manager.beAfterTp1) {
    currentSl = trade.entry;
    breakEvenMoved = 1;
    status = "TP1 HIT / BREAKEVEN MOVED";
    lastAction = "TP1 hit and SL moved to entry";

    if (manager.tp1PartialPct > 0) {
      lastAction += ` | Partial close ${manager.tp1PartialPct}% placeholder`;
    }
  }

  await dbRun(
    `UPDATE trades
     SET tp1Hit = 1, currentSl = ?, breakEvenMoved = ?, status = ?, lastAction = ?
     WHERE id = ?`,
    [currentSl, breakEvenMoved, status, lastAction, id]
  );

  await recalcTradeById(id);
  res.redirect("/");
});

app.post("/trade/:id/tp2-hit", async (req, res) => {
  const id = req.params.id;
  const manager = await getManagerSettings();

  let lastAction = "TP2 marked hit";
  if (manager.tp2PartialPct > 0) {
    lastAction += ` | Partial close ${manager.tp2PartialPct}% placeholder`;
  }

  await dbRun(
    `UPDATE trades SET tp2Hit = 1, status = ?, lastAction = ? WHERE id = ?`,
    ["TP2 HIT", lastAction, id]
  );
  await recalcTradeById(id);
  res.redirect("/");
});

app.post("/trade/:id/tp3-hit", async (req, res) => {
  const id = req.params.id;
  const manager = await getManagerSettings();

  const status = manager.autoCloseAtTp3 ? "TP3 HIT / CLOSED" : "TP3 HIT";
  const lastAction = manager.autoCloseAtTp3 ? "TP3 marked hit and trade closed" : "TP3 marked hit";

  await dbRun(
    `UPDATE trades SET tp3Hit = 1, status = ?, lastAction = ? WHERE id = ?`,
    [status, lastAction, id]
  );
  await recalcTradeById(id);
  res.redirect("/");
});

app.post("/trade/:id/move-sl-be", async (req, res) => {
  const id = req.params.id;
  const trade = await dbGet(`SELECT * FROM trades WHERE id = ?`, [id]);
  if (!trade) return res.status(404).send("Trade not found");

  await dbRun(
    `UPDATE trades
     SET currentSl = ?, breakEvenMoved = 1, status = ?, lastAction = ?
     WHERE id = ?`,
    [trade.entry, "BREAKEVEN MOVED", "SL manually moved to entry", id]
  );

  await recalcTradeById(id);
  res.redirect("/");
});

app.post("/trade/:id/close", async (req, res) => {
  await dbRun(
    `UPDATE trades SET status = ?, lastAction = ? WHERE id = ?`,
    ["CLOSED", "Trade manually closed", req.params.id]
  );
  await recalcTradeById(req.params.id);
  res.redirect("/");
});

app.post("/trade/:id/stop", async (req, res) => {
  await dbRun(
    `UPDATE trades SET status = ?, lastAction = ? WHERE id = ?`,
    ["STOPPED OUT", "Trade manually stopped out", req.params.id]
  );
  await recalcTradeById(req.params.id);
  res.redirect("/");
});

// -----------------------------------------------------------------------------
// WEBHOOK
// -----------------------------------------------------------------------------
app.post("/webhook/tradingview", async (req, res) => {
  try {
    const payload = req.body || {};
    const providedSecret = sanitizeString(payload.secret);

    if (providedSecret !== WEBHOOK_SECRET) {
      return res.status(401).json({
        ok: false,
        error: "Invalid secret"
      });
    }

    const alertRecord = {
      id: `alert_${Date.now()}`,
      receivedAt: nowIso(),
      bridge: sanitizeString(payload.bridge, "H2_MAIN_BRIDGE"),
      mode: sanitizeString(payload.mode, "Both"),
      type: sanitizeString(payload.type, "WAIT"),
      symbol: sanitizeString(payload.symbol, ""),
      tf: sanitizeString(payload.tf, ""),
      entry: sanitizeNumber(payload.entry),
      sl: sanitizeNumber(payload.sl),
      tp1: sanitizeNumber(payload.tp1),
      tp2: sanitizeNumber(payload.tp2),
      tp3: sanitizeNumber(payload.tp3),
      confidence: sanitizeNumber(payload.confidence),
      wa_route: sanitizeString(payload.wa_route, ""),
      broker_route: sanitizeString(payload.broker_route, ""),
      raw_json: JSON.stringify(payload)
    };

    await dbRun(
      `INSERT INTO alerts (
        id, receivedAt, bridge, mode, type, symbol, tf, entry, sl, tp1, tp2, tp3,
        confidence, wa_route, broker_route, raw_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        alertRecord.id,
        alertRecord.receivedAt,
        alertRecord.bridge,
        alertRecord.mode,
        alertRecord.type,
        alertRecord.symbol,
        alertRecord.tf,
        alertRecord.entry,
        alertRecord.sl,
        alertRecord.tp1,
        alertRecord.tp2,
        alertRecord.tp3,
        alertRecord.confidence,
        alertRecord.wa_route,
        alertRecord.broker_route,
        alertRecord.raw_json
      ]
    );

    if (alertRecord.type === "LONG" || alertRecord.type === "SHORT") {
      const usdPerPip = await getUsdPerPip();
      const entry = alertRecord.entry;
      const sl = alertRecord.sl;
      const tp1 = alertRecord.tp1;
      const tp2 = alertRecord.tp2;
      const tp3 = alertRecord.tp3;

      const riskPips = calcPipDistance(entry, sl);
      const tp1Pips = calcPipDistance(entry, tp1);
      const tp2Pips = calcPipDistance(entry, tp2);
      const tp3Pips = calcPipDistance(entry, tp3);

      const tradeRecord = {
        id: `trade_${Date.now()}`,
        createdAt: nowIso(),
        bridge: alertRecord.bridge,
        mode: alertRecord.mode,
        type: alertRecord.type,
        symbol: alertRecord.symbol,
        tf: alertRecord.tf,
        entry,
        sl,
        tp1,
        tp2,
        tp3,
        confidence: alertRecord.confidence,
        wa_route: alertRecord.wa_route,
        broker_route: alertRecord.broker_route,
        status: "OPEN",
        usdPerPip,
        riskPips,
        tp1Pips,
        tp2Pips,
        tp3Pips,
        riskUsd: calcMoney(riskPips, usdPerPip),
        tp1Usd: calcMoney(tp1Pips, usdPerPip),
        tp2Usd: calcMoney(tp2Pips, usdPerPip),
        tp3Usd: calcMoney(tp3Pips, usdPerPip),
        originalSl: sl,
        currentSl: sl,
        tp1Hit: 0,
        tp2Hit: 0,
        tp3Hit: 0,
        breakEvenMoved: 0,
        lastAction: "Trade created",
        currentPrice: null
      };

      await dbRun(
        `INSERT INTO trades (
          id, createdAt, bridge, mode, type, symbol, tf, entry, sl, tp1, tp2, tp3,
          confidence, wa_route, broker_route, status, usdPerPip,
          riskPips, tp1Pips, tp2Pips, tp3Pips, riskUsd, tp1Usd, tp2Usd, tp3Usd,
          originalSl, currentSl, tp1Hit, tp2Hit, tp3Hit, breakEvenMoved, lastAction, currentPrice
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          tradeRecord.id,
          tradeRecord.createdAt,
          tradeRecord.bridge,
          tradeRecord.mode,
          tradeRecord.type,
          tradeRecord.symbol,
          tradeRecord.tf,
          tradeRecord.entry,
          tradeRecord.sl,
          tradeRecord.tp1,
          tradeRecord.tp2,
          tradeRecord.tp3,
          tradeRecord.confidence,
          tradeRecord.wa_route,
          tradeRecord.broker_route,
          tradeRecord.status,
          tradeRecord.usdPerPip,
          tradeRecord.riskPips,
          tradeRecord.tp1Pips,
          tradeRecord.tp2Pips,
          tradeRecord.tp3Pips,
          tradeRecord.riskUsd,
          tradeRecord.tp1Usd,
          tradeRecord.tp2Usd,
          tradeRecord.tp3Usd,
          tradeRecord.originalSl,
          tradeRecord.currentSl,
          tradeRecord.tp1Hit,
          tradeRecord.tp2Hit,
          tradeRecord.tp3Hit,
          tradeRecord.breakEvenMoved,
          tradeRecord.lastAction,
          tradeRecord.currentPrice
        ]
      );
    }

    console.log("Webhook received:", JSON.stringify(alertRecord, null, 2));

    return res.json({
      ok: true,
      message: "Webhook received",
      alertId: alertRecord.id
    });
  } catch (error) {
    console.error("Webhook error:", error);
    return res.status(500).json({
      ok: false,
      error: "Server error"
    });
  }
});

// -----------------------------------------------------------------------------
// STARTUP
// -----------------------------------------------------------------------------
(async () => {
  try {
    await initDb();
    await recalcAllTrades();

    app.listen(PORT, () => {
      console.log(`${APP_NAME} listening on port ${PORT}`);
      console.log(`SQLite DB: ${dbPath}`);
    });
  } catch (error) {
    console.error("Startup error:", error);
    process.exit(1);
  }
})();