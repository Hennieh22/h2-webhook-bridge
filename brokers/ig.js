// ---------------------------------------------------------------------------
// brokers/ig.js  —  IG Markets adapter
// Credentials come from igConfig (set by server.js from DB settings)
// Falls back to process.env if igConfig values are empty
// ---------------------------------------------------------------------------

let igConfig = {
  baseUrl:      "",
  apiKey:       "",
  identifier:   "",
  password:     "",
  accountMode:  "",
  defaultSize:  1,
  currencyCode: "USD",
  epics: {},     // { JP225: "IX.D.NIKKEI.IFM.IP", ... }
  tvMap: {}      // { NI225: "JP225", JPN225: "JP225", ... } — TradingView ticker to internal key
};

// Called by server.js after loading settings from DB
function setConfig(config) {
  igConfig = { ...igConfig, ...config };
}

function cfg(key, envKey, fallback = "") {
  return igConfig[key] || process.env[envKey] || fallback;
}

function resolveEpic(symbol) {
  const s       = String(symbol || "").toUpperCase();
  const epicMap = igConfig.epics || {};
  const tvMap   = igConfig.tvMap || {};

  // Step 1: Check if TradingView sent a known ticker alias (e.g. "NI225" → "JP225")
  if (tvMap[s]) {
    const key = tvMap[s];
    return epicMap[key] || process.env[`IG_EPIC_${key}`] || "";
  }

  // Step 2: Try direct epic map lookup (e.g. symbol = "JP225" is already the internal key)
  if (epicMap[s]) return epicMap[s];

  // Step 3: Built-in fallback aliases for common variations
  const aliases = {
    JP225:    ["JP225","JAPAN225","NIKKEI","JPN225","NI225"],
    NAS100:   ["NAS100","TECH100","US100","NASDAQ","NDX","NQ1!"],
    DAX40:    ["DAX40","GER40","DAX","DEU40","FDAX"],
    SP500:    ["SP500","US500","SPX","SPX500","ES1!"],
    DOW:      ["DOW","WALLST","US30","DJI","YM1!"],
    FTSE:     ["FTSE","UK100","UKX"],
    AUS200:   ["AUS200","ASX200","AS51"]
  };

  for (const [key, aliasList] of Object.entries(aliases)) {
    if (aliasList.includes(s)) {
      return epicMap[key] || process.env[`IG_EPIC_${key}`] || "";
    }
  }

  // Step 4: Try env variable directly
  return process.env[`IG_EPIC_${s}`] || "";
}

async function igFetch(path, options = {}, session = null, version = "2") {
  const baseUrl = cfg("baseUrl", "IG_BASE_URL", "https://demo-api.ig.com/gateway/deal");
  const apiKey  = cfg("apiKey",  "IG_API_KEY",  "");

  const headers = {
    Accept:           "application/json; charset=UTF-8",
    "Content-Type":   "application/json; charset=UTF-8",
    "X-IG-API-KEY":   apiKey,
    Version:          version,
    ...(options.headers || {})
  };
  if (session?.cst)           headers.CST                = session.cst;
  if (session?.securityToken) headers["X-SECURITY-TOKEN"] = session.securityToken;

  const res = await fetch(`${baseUrl}${path}`, {
    method:  options.method || "GET",
    headers,
    body:    options.body ? JSON.stringify(options.body) : undefined
  });

  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

  if (!res.ok) throw new Error(`IG API error ${res.status}: ${JSON.stringify(data)}`);
  return { status: res.status, data, headers: res.headers };
}

async function createSession() {
  const apiKey     = cfg("apiKey",     "IG_API_KEY",     "");
  const identifier = cfg("identifier", "IG_IDENTIFIER",  "");
  const password   = cfg("password",   "IG_PASSWORD",    "");
  const accMode    = cfg("accountMode","IG_ACCOUNT_MODE","DEMO");

  if (!apiKey || !identifier || !password)
    throw new Error("Missing IG credentials — set them in Settings > Broker Setup");

  const result = await igFetch("/session", {
    method: "POST",
    body:   { identifier, password }
  }, null, "2");

  const cst           = result.headers.get("CST");
  const securityToken = result.headers.get("X-SECURITY-TOKEN");
  if (!cst || !securityToken) throw new Error("IG session created but tokens missing");

  return { cst, securityToken, accountMode: accMode, accountInfo: result.data || {} };
}

function sideToDirection(type) {
  const t = String(type || "").toUpperCase();
  if (t === "LONG"  || t === "BUY")  return "BUY";
  if (t === "SHORT" || t === "SELL") return "SELL";
  throw new Error(`Unsupported trade type: ${type}`);
}

async function placeMarketOrder(signal) {
  const epic = signal.epic || resolveEpic(signal.symbol);
  if (!epic) throw new Error(`No IG epic configured for symbol: ${signal.symbol || "UNKNOWN"} — add it in Settings > Instruments`);

  const direction = signal.direction || sideToDirection(signal.type);
  const session   = await createSession();
  const defSize   = Number(igConfig.defaultSize || process.env.IG_DEFAULT_SIZE || 1);
  const size      = Number(signal.brokerSize || signal.size || defSize);
  const currency  = cfg("currencyCode", "IG_CURRENCY_CODE", "USD");

  if (!Number.isFinite(size) || size <= 0) throw new Error("Invalid IG order size");

  // ── Step 1: Check market status before placing order
  try {
    const marketData = await igFetch(`/markets/${encodeURIComponent(epic)}`, { method: "GET" }, session, "3");
    const status = marketData.data?.snapshot?.marketStatus;
    if (status && status !== "TRADEABLE") {
      throw new Error(`Market is not tradeable right now — status: ${status}. JP225 trades 2am-4:30am and 5:30am-8:30am SA time.`);
    }
  } catch (mktErr) {
    // If status check itself fails (e.g. epic not found) throw a clear error
    if (mktErr.message.includes("not tradeable") || mktErr.message.includes("SA time")) throw mktErr;
    console.warn("[IG] Market status check failed, proceeding with order:", mktErr.message);
  }

  // ── Step 2: Place the order — returns dealReference
  const result = await igFetch("/positions/otc", {
    method: "POST",
    body: { epic, expiry: "DFB", direction, size, orderType: "MARKET", forceOpen: true, guaranteedStop: false, currencyCode: currency }
  }, session, "2");

  const dealRef = result.data?.dealReference;
  if (!dealRef) throw new Error(`IG did not return a dealReference — response: ${JSON.stringify(result.data)}`);

  // ── Step 3: Confirm the deal — check if IG actually accepted or rejected it
  // Wait 500ms for IG to process the deal before confirming
  await new Promise(resolve => setTimeout(resolve, 500));

  const confirm = await igFetch(`/confirms/${dealRef}`, { method: "GET" }, session, "1");
  const dealStatus    = confirm.data?.dealStatus;   // "ACCEPTED" or "REJECTED"
  const rejectReason  = confirm.data?.rejectReason; // e.g. "MARKET_CLOSED"
  const dealId        = confirm.data?.dealId;

  if (dealStatus === "REJECTED") {
    const reason   = rejectReason || "UNKNOWN";
    const fullResp = JSON.stringify(confirm.data || {});
    let friendly   = reason;
    if (reason === "MARKET_CLOSED"        || reason.includes("CLOSED"))       friendly = `Market is closed (${reason})`;
    else if (reason.includes("INSUFFICIENT"))                                  friendly = `Insufficient funds or margin (${reason})`;
    else if (reason.includes("SIZE")      || reason.includes("MINIMUM"))      friendly = `Invalid size (${reason}) — minimum size may not be 1`;
    else if (reason.includes("FORCE_OPEN"))                                    friendly = `Force open not allowed (${reason})`;
    else if (reason.includes("EXPIRY"))                                        friendly = `Expiry issue (${reason})`;
    else if (reason.includes("CURRENCY"))                                      friendly = `Currency mismatch (${reason})`;
    else if (reason.includes("ATTACHED"))                                      friendly = `Attached order error (${reason})`;
    else if (reason === "UNKNOWN")                                             friendly = `Unknown rejection — full IG response: ${fullResp}`;
    throw new Error(`IG rejected: ${friendly}`);
  }

  console.log(`[IG] Order ACCEPTED — dealRef: ${dealRef}, dealId: ${dealId}`);
  return {
    ok: true, broker: "IG",
    accountMode: igConfig.accountMode || "DEMO",
    epic, direction, size,
    dealReference: dealRef,
    dealId,
    dealStatus,
    response: confirm.data
  };
}

async function getOpenPositions() {
  const session = await createSession();
  const result  = await igFetch("/positions/otc", { method: "GET" }, session, "2");
  return result.data?.positions || [];
}

async function getCurrentPrice(epic) {
  const session  = await createSession();
  const result   = await igFetch(`/markets/${encodeURIComponent(epic)}`, { method: "GET" }, session, "3");
  const snapshot = result.data?.snapshot;
  if (!snapshot) throw new Error(`No snapshot data for epic ${epic}`);
  const bid = Number(snapshot.bid), offer = Number(snapshot.offer);
  return { bid, offer, mid: (bid + offer) / 2, status: snapshot.marketStatus };
}

async function modifyStopLevel(dealId, newStopLevel) {
  const session = await createSession();
  const result  = await igFetch(`/positions/otc/${dealId}`, {
    method: "PUT",
    body: { stopLevel: newStopLevel, trailingStop: false, limitLevel: null, trailingStopDistance: null, trailingStopIncrement: null }
  }, session, "2");
  return { ok: true, response: result.data };
}

async function closePositionByDealId(dealId, direction, size) {
  const session        = await createSession();
  const closeDirection = String(direction).toUpperCase() === "BUY" ? "SELL" : "BUY";
  const result = await igFetch("/positions/otc", {
    method: "POST",
    headers: { "_method": "DELETE" },
    body: { dealId, direction: closeDirection, size: Number(size), orderType: "MARKET", timeInForce: "EXECUTE_AND_ELIMINATE" }
  }, session, "1");
  return { ok: true, broker: "IG", response: result.data };
}

async function searchMarkets(term) {
  const session = await createSession();
  const result  = await igFetch(`/markets?searchTerm=${encodeURIComponent(term)}`, { method: "GET" }, session, "1");
  return result.data;
}

async function getMarketDetails(epic) {
  const session = await createSession();
  const result  = await igFetch(`/markets/${encodeURIComponent(epic)}`, { method: "GET" }, session, "3");
  return result.data;
}

module.exports = {
  setConfig, resolveEpic, createSession,
  placeMarketOrder, getOpenPositions, getCurrentPrice,
  modifyStopLevel, closePositionByDealId,
  searchMarkets, getMarketDetails
};