const express = require("express");
const dotenv = require("dotenv");
const { placeMarketOrder, searchMarkets, getMarketDetails } = require("./brokers/ig");

dotenv.config();

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || "changeme";
const APP_NAME = process.env.APP_NAME || "H2 Webhook Bridge";

// EXECUTION MODE
const EXECUTION_MODE = process.env.EXECUTION_MODE || "APPROVAL";
const DEFAULT_BROKER = process.env.DEFAULT_BROKER || "IG";

// MEMORY STORAGE
let trades = [];
let executions = [];

function now() {
  return new Date().toISOString();
}

// -----------------------------------------------------------------------------
// DASHBOARD
// -----------------------------------------------------------------------------
app.get("/", (req, res) => {
  res.send(`
    <h1>${APP_NAME}</h1>
    <h3>Phase C4.1 · IG Demo Approval Execution</h3>

    <p><b>Execution Mode:</b> ${EXECUTION_MODE}</p>

    <h2>Trades (${trades.length})</h2>
    <pre>${JSON.stringify(trades.slice(-5), null, 2)}</pre>

    <h2>Executions (${executions.length})</h2>
    <pre>${JSON.stringify(executions.slice(-5), null, 2)}</pre>

    <h2>Pending Approvals</h2>
    ${
      executions
        .filter(e => e.status === "PENDING")
        .map(e => `
          <form method="POST" action="/execute/${e.id}">
            <button type="submit">EXECUTE ${e.symbol} ${e.type}</button>
          </form>
        `)
        .join("") || "<p>No pending trades</p>"
    }
  `);
});

// -----------------------------------------------------------------------------
// WEBHOOK
// -----------------------------------------------------------------------------
app.post("/webhook/tradingview", (req, res) => {
  const data = req.body;

  if (data.secret !== WEBHOOK_SECRET) {
    return res.status(401).json({ error: "Invalid secret" });
  }

  const trade = {
    id: "trade_" + Date.now(),
    createdAt: now(),
    ...data
  };

  trades.push(trade);

  const execution = {
    id: "exec_" + Date.now(),
    tradeId: trade.id,
    symbol: trade.symbol,
    type: trade.type,
    size: 1,
    status: EXECUTION_MODE === "AUTO" ? "EXECUTING" : "PENDING",
    createdAt: now()
  };

  executions.push(execution);

  console.log("NEW TRADE:", trade);

  if (EXECUTION_MODE === "AUTO") {
    executeTrade(execution);
  }

  res.json({ ok: true });
});
app.get("/ig/search", async (req, res) => {
  try {
    const term = String(req.query.term || "").trim();
    if (!term) {
      return res.status(400).json({ ok: false, error: "Missing ?term=" });
    }

    const data = await searchMarkets(term);
    res.json({ ok: true, term, data });
  } catch (error) {
    res.status(500).json({
      ok: false,
      error: error.message || "IG search failed"
    });
  }
});

app.get("/ig/market/:epic", async (req, res) => {
  try {
    const epic = req.params.epic;
    const data = await getMarketDetails(epic);
    res.json({ ok: true, epic, data });
  } catch (error) {
    res.status(500).json({
      ok: false,
      error: error.message || "IG market lookup failed"
    });
  }
});

// -----------------------------------------------------------------------------
// MANUAL EXECUTION (APPROVAL MODE)
// -----------------------------------------------------------------------------
app.post("/execute/:id", async (req, res) => {
  const exec = executions.find(e => e.id === req.params.id);
  if (!exec) return res.send("Not found");

  await executeTrade(exec);

  res.redirect("/");
});

// -----------------------------------------------------------------------------
// EXECUTION ENGINE
// -----------------------------------------------------------------------------
async function executeTrade(exec) {
  try {
    exec.status = "EXECUTING";

    const result = await placeMarketOrder({
      epic: process.env.IG_EPIC_JP225,
      direction: exec.type === "LONG" ? "BUY" : "SELL",
      size: exec.size
    });

    exec.status = "EXECUTED";
    exec.response = result;

    console.log("EXECUTED:", result);

  } catch (err) {
    exec.status = "FAILED";
    exec.error = err.message;

    console.error("EXECUTION FAILED:", err.message);
  }
}

// -----------------------------------------------------------------------------
// HEALTH
// -----------------------------------------------------------------------------
app.get("/health", (req, res) => {
  res.json({
    ok: true,
    mode: EXECUTION_MODE,
    trades: trades.length,
    executions: executions.length
  });
});

// -----------------------------------------------------------------------------
// START
// -----------------------------------------------------------------------------
app.listen(PORT, () => {
  console.log(`${APP_NAME} running on port ${PORT}`);
});