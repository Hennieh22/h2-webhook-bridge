'use strict';

require('dotenv').config();

// ─────────────────────────────────────────────────────────────
//  MetaAPI REST adapter for IC Markets MT5
//  Uses MetaAPI cloud REST API — no SDK needed, pure HTTP calls
// ─────────────────────────────────────────────────────────────

const METAAPI_TOKEN      = process.env.METAAPI_TOKEN      || '';
const METAAPI_ACCOUNT_ID = process.env.METAAPI_ACCOUNT_ID || '';
const METAAPI_REGION     = process.env.METAAPI_REGION     || 'london';

const BASE_URL = `https://mt-client-api-v1.${METAAPI_REGION}.agiliumtrade.ai`;

// ─── Symbol map ───────────────────────────────────────────────
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

  const res  = await fetch(url, options);
  const text = await res.text();

  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }

  console.log(`[ICMarkets] Response ${res.status}:`, JSON.stringify(data).substring(0, 300));

  if (!res.ok) {
    throw new Error(`MetaAPI error ${res.status}: ${JSON.stringify(data)}`);
  }

  return data;
}

// ─── sleep helper ─────────────────────────────────────────────
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ─── setSlTp ─────────────────────────────────────────────────
// Adds SL and TP to an open position.
// MT5 needs the position to be fully registered before we can modify it,
// so we retry a few times with a short delay if the first attempt fails.
async function setSlTp(positionId, stopLoss, takeProfit, retries = 4, delayMs = 1500) {
  const sl = parseFloat(stopLoss);
  const tp = parseFloat(takeProfit);

  // Only set valid numbers — skip zeros and nulls
  const hasSL = sl && sl > 0;
  const hasTP = tp && tp > 0;

  if (!hasSL && !hasTP) {
    console.log('[ICMarkets] setSlTp — no valid SL or TP provided, skipping modify');
    return { success: true, skipped: true };
  }

  // positionId must be numeric for MetaAPI POSITION_MODIFY
  const numericPosId = Number(positionId);

  const modBody = {
    actionType: 'POSITION_MODIFY',
    positionId: numericPosId,
  };
  if (hasSL) modBody.stopLoss   = sl;
  if (hasTP) modBody.takeProfit = tp;

  console.log(`[ICMarkets] Setting SL/TP on position ${positionId} — SL: ${hasSL ? sl : 'none'}  TP: ${hasTP ? tp : 'none'}`);
  console.log(`[ICMarkets] Modify body: ${JSON.stringify(modBody)}`);

  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      // Delay increases with each attempt — gives MT5 more time to register position
      await sleep(delayMs * attempt);

      const result = await metaApiRequest(
        'POST',
        `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
        modBody
      );

      const code = result.stringCode || '';
      console.log(`[ICMarkets] setSlTp attempt ${attempt} full response: ${JSON.stringify(result)}`);

      if (code === 'TRADE_RETCODE_DONE') {
        console.log(`[ICMarkets] SL/TP confirmed on attempt ${attempt} — SL: ${sl}  TP: ${tp}`);
        return { success: true, raw: result };
      }

      if (code === 'TRADE_RETCODE_NO_CHANGES') {
        console.log(`[ICMarkets] SL/TP already set to these values`);
        return { success: true, raw: result };
      }

      if (code === 'TRADE_RETCODE_INVALID_STOPS') {
        console.log(`[ICMarkets] SL/TP rejected — INVALID_STOPS. SL: ${sl} TP: ${tp}`);
        return { success: false, reason: 'INVALID_STOPS', raw: result };
      }

      console.log(`[ICMarkets] setSlTp attempt ${attempt} — unexpected code: ${code}`);

    } catch (err) {
      console.log(`[ICMarkets] setSlTp attempt ${attempt} failed: ${err.message}`);
      if (attempt === retries) {
        console.log('[ICMarkets] All SL/TP set attempts failed — trade is open but without SL/TP on broker');
        return { success: false, reason: err.message };
      }
    }
  }

  return { success: false, reason: 'max retries reached' };
}

// ─── placeMarketOrder ─────────────────────────────────────────
// Places the order then immediately sets SL and TP on the open position.
// signal: { type, symbol, brokerSize, sl, tp1, tp2, entry }
async function placeMarketOrder(signal) {
  const mtSymbol   = resolveSymbol(signal.symbol);
  const actionType = signal.type === 'LONG' ? 'ORDER_TYPE_BUY' : 'ORDER_TYPE_SELL';
  const volume     = parseFloat(signal.brokerSize) || 1;

  console.log(`[ICMarkets] Placing ${actionType} on ${mtSymbol} size=${volume}`);

  // Step 1: Place bare market order — no SL/TP in initial order
  // MT5 rejects orders with SL/TP when the price is not yet known at fill time
  const orderBody = {
    actionType,
    symbol: mtSymbol,
    volume,
  };

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    orderBody
  );

  const positionId = result.positionId || null;
  const orderId    = result.orderId    || null;
  const retCode    = result.stringCode || null;

  console.log(`[ICMarkets] Trade placed — positionId: ${positionId}  orderId: ${orderId}  retCode: ${retCode}`);

  if (!positionId && !orderId) {
    throw new Error(`MT5 rejected order: ${retCode || 'unknown'} — ${result.message || JSON.stringify(result)}`);
  }

  // Step 2: Set SL and TP on the now-open position
  // Use tp1 as the TP on the broker — this is the structural first target
  // The bridge tracks tp2 and tp3 internally
  let slTpResult = { success: false, skipped: true };

  if (positionId) {
    const sl = parseFloat(signal.sl)  || null;
    const tp = parseFloat(signal.tp1) || null;

    slTpResult = await setSlTp(positionId, sl, tp);

    if (slTpResult.success && !slTpResult.skipped) {
      console.log(`[ICMarkets] SL/TP confirmed on MT5 — SL: ${sl}  TP1: ${tp}`);
    } else if (!slTpResult.success) {
      console.log(`[ICMarkets] Warning: SL/TP not set on MT5 — reason: ${slTpResult.reason}`);
      console.log(`[ICMarkets] Trade is open. Bridge will track SL/TP internally.`);
    }
  } else {
    console.log('[ICMarkets] No positionId returned — cannot set SL/TP (order may be pending)');
  }

  return {
    success:       true,
    positionId,
    orderId,
    dealRef:       positionId || orderId,
    slTpSet:       slTpResult.success && !slTpResult.skipped,
    slTpReason:    slTpResult.reason  || null,
    raw:           result,
  };
}

// ─── modifyPosition ───────────────────────────────────────────
// Moves the stop loss on an open position (used for BE move)
async function modifyPosition(positionId, newStopLoss, newTakeProfit = null) {
  console.log(`[ICMarkets] Modifying position ${positionId} — SL: ${newStopLoss}  TP: ${newTakeProfit || 'unchanged'}`);

  const body = {
    actionType: 'POSITION_MODIFY',
    positionId: String(positionId),
    stopLoss:   parseFloat(newStopLoss),
  };

  if (newTakeProfit && parseFloat(newTakeProfit) > 0) {
    body.takeProfit = parseFloat(newTakeProfit);
  }

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    body
  );

  return { success: true, raw: result };
}

// ─── closePosition ────────────────────────────────────────────
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

// ─── getOpenPositions ─────────────────────────────────────────
async function getOpenPositions() {
  return await metaApiRequest(
    'GET',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/positions`
  );
}

// ─── getAccountInfo ───────────────────────────────────────────
async function getAccountInfo() {
  return await metaApiRequest(
    'GET',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/account-information`
  );
}

// ─── searchSymbols ────────────────────────────────────────────
async function searchSymbols(query) {
  try {
    const result = await metaApiRequest(
      'GET',
      `/users/current/accounts/${METAAPI_ACCOUNT_ID}/symbols`
    );
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