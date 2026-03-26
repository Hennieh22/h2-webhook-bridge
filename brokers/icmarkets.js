'use strict';

require('dotenv').config();

// ─────────────────────────────────────────────────────────────
//  MetaAPI REST adapter for IC Markets MT5
//  Uses MetaAPI cloud REST API — no SDK needed, pure HTTP calls
// ─────────────────────────────────────────────────────────────

const METAAPI_TOKEN      = process.env.METAAPI_TOKEN      || '';
const METAAPI_ACCOUNT_ID = process.env.METAAPI_ACCOUNT_ID || '';
const METAAPI_REGION     = process.env.METAAPI_REGION     || 'london';

// MetaAPI REST base URL — uses the region you are hosted in
const BASE_URL = `https://mt-client-api-v1.${METAAPI_REGION}.agiliumtrade.ai`;

// ─── Symbol map ───────────────────────────────────────────────
// IC Markets MT5 uses different symbol names from TradingView
// Check your MT5 Market Watch window for exact names
function resolveSymbol(symbol) {
  const map = {
    JP225:  process.env.MT5_SYMBOL_JP225  || 'JP225Cash',
    NAS100: process.env.MT5_SYMBOL_NAS100 || 'NAS100',
    DAX40:  process.env.MT5_SYMBOL_DAX40  || 'GER40',
    SP500:  process.env.MT5_SYMBOL_SP500  || 'US500',
    DOW:    process.env.MT5_SYMBOL_DOW    || 'US30',
    FTSE:   process.env.MT5_SYMBOL_FTSE   || 'UK100',
    AUS200: process.env.MT5_SYMBOL_AUS200 || 'AUS200',
  };
  return map[symbol] || symbol;
}

// ─── Core HTTP helper ─────────────────────────────────────────
async function metaApiRequest(method, path, body = null) {
  const url = `${BASE_URL}${path}`;
  const options = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'auth-token': METAAPI_TOKEN,
    },
  };
  if (body) options.body = JSON.stringify(body);

  console.log(`[ICMarkets] ${method} ${path}`);

  const res = await fetch(url, options);
  const text = await res.text();

  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }

  console.log(`[ICMarkets] Response ${res.status}:`, JSON.stringify(data).substring(0, 300));

  if (!res.ok) {
    throw new Error(`MetaAPI error ${res.status}: ${JSON.stringify(data)}`);
  }

  return data;
}

// ─── placeMarketOrder ─────────────────────────────────────────
// Called by server.js when a trade signal arrives
// signal: { type:'LONG'|'SHORT', symbol:'JP225', brokerSize:1, sl, tp1, entry }
async function placeMarketOrder(signal) {
  const mtSymbol = resolveSymbol(signal.symbol);
  const actionType = signal.type === 'LONG' ? 'ORDER_TYPE_BUY' : 'ORDER_TYPE_SELL';
  const volume = parseFloat(signal.brokerSize) || 1;

  console.log(`[ICMarkets] Placing ${actionType} on ${mtSymbol} size=${volume}`);

  // Bare minimum order — no SL, no TP, no comment.
  // MT5 / MetaAPI rejects orders with invalid or missing stop levels.
  // Bridge manages SL/TP tracking internally.
  const orderBody = {
    actionType,
    symbol:  mtSymbol,
    volume,
  };

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    orderBody
  );

  // MetaAPI returns orderId / positionId on success.
  // On MT5 rejection it returns HTTP 200 with a stringCode like TRADE_RETCODE_INVALID_STOPS.
  // We treat anything without an orderId/positionId as a failure.
  const positionId = result.positionId || null;
  const orderId    = result.orderId    || null;
  const retCode    = result.stringCode || null;

  console.log(`[ICMarkets] Trade placed — positionId: ${positionId}  orderId: ${orderId}  retCode: ${retCode}`);

  // If MT5 rejected the order, throw so the bridge marks it FAILED
  if (!positionId && !orderId) {
    throw new Error(`MT5 rejected order: ${retCode || 'unknown'} — ${result.message || JSON.stringify(result)}`);
  }

  return {
    success:    true,
    positionId,
    orderId,
    dealRef:    positionId || orderId,
    raw:        result,
  };
}

// ─── getOpenPositions ─────────────────────────────────────────
// Returns all currently open positions on the MT5 account
async function getOpenPositions() {
  const result = await metaApiRequest(
    'GET',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/positions`
  );
  return result; // array of position objects
}

// ─── modifyPosition ───────────────────────────────────────────
// Moves the stop loss on an open position (used for BE move)
// positionId: the MT5 position ID
// newStopLoss: the new SL price
async function modifyPosition(positionId, newStopLoss) {
  console.log(`[ICMarkets] Modifying position ${positionId} — new SL: ${newStopLoss}`);

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    {
      actionType: 'POSITION_MODIFY',
      positionId: String(positionId),
      stopLoss:   parseFloat(newStopLoss),
    }
  );

  return { success: true, raw: result };
}

// ─── closePosition ────────────────────────────────────────────
// Closes an open position by its MT5 position ID
async function closePosition(positionId) {
  console.log(`[ICMarkets] Closing position ${positionId}`);

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    {
      actionType: 'POSITION_CLOSE_ID',
      positionId: String(positionId),
    }
  );

  return { success: true, raw: result };
}

// ─── getAccountInfo ───────────────────────────────────────────
// Returns account balance and basic info — used for health checks
async function getAccountInfo() {
  const result = await metaApiRequest(
    'GET',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/account-information`
  );
  return result;
}

// ─── searchSymbols ────────────────────────────────────────────
// Searches for a symbol by name — helps find correct MT5 symbol names
async function searchSymbols(query) {
  try {
    const result = await metaApiRequest(
      'GET',
      `/users/current/accounts/${METAAPI_ACCOUNT_ID}/symbols`
    );
    // Filter to symbols matching the query
    if (Array.isArray(result)) {
      return result.filter(s =>
        (typeof s === 'string' ? s : s.symbol || '')
          .toLowerCase()
          .includes(query.toLowerCase())
      );
    }
    return result;
  } catch (err) {
    console.error('[ICMarkets] searchSymbols error:', err.message);
    throw err;
  }
}

module.exports = {
  placeMarketOrder,
  getOpenPositions,
  modifyPosition,
  closePosition,
  getAccountInfo,
  searchSymbols,
};