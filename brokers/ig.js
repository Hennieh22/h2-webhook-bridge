const IG_BASE_URL = process.env.IG_BASE_URL || "https://demo-api.ig.com/gateway/deal";
const IG_API_KEY = process.env.IG_API_KEY || "";
const IG_IDENTIFIER = process.env.IG_IDENTIFIER || "";
const IG_PASSWORD = process.env.IG_PASSWORD || "";
const IG_ACCOUNT_MODE = process.env.IG_ACCOUNT_MODE || "DEMO";
const IG_DEFAULT_SIZE = Number(process.env.IG_DEFAULT_SIZE || "1");
const IG_CURRENCY_CODE = process.env.IG_CURRENCY_CODE || "USD";

// You should set these in Railway Variables.
// Example:
// IG_EPIC_JP225=YOUR_IG_EPIC_FOR_JP225
// IG_EPIC_NAS100=YOUR_IG_EPIC_FOR_NAS100
// IG_EPIC_DAX40=YOUR_IG_EPIC_FOR_DAX40
function resolveEpic(symbol) {
  const s = String(symbol || "").toUpperCase();

  const map = {
    JP225: process.env.IG_EPIC_JP225 || "",
    JAPAN225: process.env.IG_EPIC_JP225 || "",
    NIKKEI: process.env.IG_EPIC_JP225 || "",
    NAS100: process.env.IG_EPIC_NAS100 || "",
    TECH100: process.env.IG_EPIC_NAS100 || "",
    US100: process.env.IG_EPIC_NAS100 || "",
    DAX40: process.env.IG_EPIC_DAX40 || "",
    GER40: process.env.IG_EPIC_DAX40 || ""
  };

  return map[s] || "";
}

async function igFetch(path, options = {}, session = null, version = "2") {
  const headers = {
    Accept: "application/json; charset=UTF-8",
    "Content-Type": "application/json; charset=UTF-8",
    "X-IG-API-KEY": IG_API_KEY,
    Version: version,
    ...(options.headers || {})
  };

  if (session?.cst) headers.CST = session.cst;
  if (session?.securityToken) headers["X-SECURITY-TOKEN"] = session.securityToken;

  const res = await fetch(`${IG_BASE_URL}${path}`, {
    method: options.method || "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined
  });

  const text = await res.text();
  let data = null;

  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }

  if (!res.ok) {
    throw new Error(`IG API error ${res.status}: ${JSON.stringify(data)}`);
  }

  return {
    status: res.status,
    data,
    headers: res.headers
  };
}

async function createSession() {
  if (!IG_API_KEY || !IG_IDENTIFIER || !IG_PASSWORD) {
    throw new Error("Missing IG credentials in environment variables");
  }

  const result = await igFetch(
    "/session",
    {
      method: "POST",
      body: {
        identifier: IG_IDENTIFIER,
        password: IG_PASSWORD
      }
    },
    null,
    "2"
  );

  const cst = result.headers.get("CST");
  const securityToken = result.headers.get("X-SECURITY-TOKEN");

  if (!cst || !securityToken) {
    throw new Error("IG session created but CST / X-SECURITY-TOKEN missing");
  }

  return {
    cst,
    securityToken,
    accountMode: IG_ACCOUNT_MODE,
    accountInfo: result.data || {}
  };
}

function sideToDirection(type) {
  const t = String(type || "").toUpperCase();
  if (t === "LONG" || t === "BUY") return "BUY";
  if (t === "SHORT" || t === "SELL") return "SELL";
  throw new Error(`Unsupported trade type: ${type}`);
}

async function placeMarketOrder(signal) {
  const epic = resolveEpic(signal.symbol);
  if (!epic) {
    throw new Error(`No IG epic configured for symbol: ${signal.symbol}`);
  }

  const direction = sideToDirection(signal.type);
  const session = await createSession();

  // First safe version:
  // - market entry
  // - stopLevel sent to broker
  // - TP1/TP2/TP3 kept in bridge for later management
  const size = Number(signal.brokerSize || IG_DEFAULT_SIZE);
  const stopLevel = Number(signal.sl);

  if (!Number.isFinite(size) || size <= 0) {
    throw new Error("Invalid IG order size");
  }

  if (!Number.isFinite(stopLevel)) {
    throw new Error("Invalid stopLevel");
  }

  const body = {
    epic,
    direction,
    size,
    orderType: "MARKET",
    forceOpen: true,
    guaranteedStop: false,
    currencyCode: IG_CURRENCY_CODE,
    stopLevel
  };

  const result = await igFetch(
    "/positions/otc",
    {
      method: "POST",
      body
    },
    session,
    "2"
  );

  return {
    ok: true,
    broker: "IG",
    accountMode: IG_ACCOUNT_MODE,
    epic,
    direction,
    size,
    stopLevel,
    response: result.data
  };
}

async function closePositionByDealId(dealId, direction, size) {
  const session = await createSession();

  const closeDirection = String(direction).toUpperCase() === "BUY" ? "SELL" : "BUY";

  const result = await igFetch(
    "/positions/otc",
    {
      method: "POST",
      headers: {
        "_method": "DELETE"
      },
      body: {
        dealId,
        direction: closeDirection,
        size: Number(size),
        orderType: "MARKET",
        timeInForce: "EXECUTE_AND_ELIMINATE"
      }
    },
    session,
    "1"
  );

  return {
    ok: true,
    broker: "IG",
    response: result.data
  };
}

module.exports = {
  createSession,
  placeMarketOrder,
  closePositionByDealId,
  resolveEpic
};