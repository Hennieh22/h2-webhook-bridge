'use strict';

require('dotenv').config();

const METAAPI_TOKEN      = process.env.METAAPI_TOKEN      || '';
const METAAPI_ACCOUNT_ID = process.env.METAAPI_ACCOUNT_ID || '';
const METAAPI_REGION     = process.env.METAAPI_REGION     || 'london';

const BASE_URL = `https://mt-client-api-v1.${METAAPI_REGION}.agiliumtrade.ai`;

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
  if (body) console.log(`[ICMarkets] Body: ${JSON.stringify(body)}`);

  const res  = await fetch(url, options);
  const text = await res.text();

  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }

  console.log(`[ICMarkets] Response ${res.status}: ${JSON.stringify(data).substring(0, 400)}`);

  if (!res.ok) {
    throw new Error(`MetaAPI error ${res.status}: ${JSON.stringify(data)}`);
  }

  return data;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Read back a single position to verify SL/TP were set
async function verifyPosition(positionId) {
  try {
    const positions = await metaApiRequest(
      'GET',
      `/users/current/accounts/${METAAPI_ACCOUNT_ID}/positions`
    );
    if (!Array.isArray(positions)) return null;
    const pos = positions.find(p => String(p.id) === String(positionId));
    if (pos) {
      console.log(`[ICMarkets] Position ${positionId} — SL: ${pos.stopLoss ?? 'none'}  TP: ${pos.takeProfit ?? 'none'}  Price: ${pos.currentPrice}`);
    } else {
      console.log(`[ICMarkets] Position ${positionId} not found in open positions`);
    }
    return pos;
  } catch (err) {
    console.log(`[ICMarkets] verifyPosition error: ${err.message}`);
    return null;
  }
}

// Fallback SL/TP setter — used only if initial order did not include them
async function setSlTp(positionId, stopLoss, takeProfit, retries = 4, delayMs = 2000) {
  const sl = parseFloat(stopLoss);
  const tp = parseFloat(takeProfit);

  const hasSL = Number.isFinite(sl) && sl > 0;
  const hasTP = Number.isFinite(tp) && tp > 0;

  if (!hasSL && !hasTP) {
    console.log('[ICMarkets] setSlTp — no valid SL or TP, skipping');
    return { success: true, skipped: true };
  }

  console.log(`[ICMarkets] setSlTp fallback — positionId: ${positionId}  SL: ${hasSL ? sl : 'none'}  TP: ${hasTP ? tp : 'none'}`);

  // Try string positionId first, then numeric on retry
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      await sleep(delayMs * attempt);

      const modBody = {
        actionType: 'POSITION_MODIFY',
        positionId: attempt <= 2 ? String(positionId) : Number(positionId),
      };
      if (hasSL) modBody.stopLoss   = sl;
      if (hasTP) modBody.takeProfit = tp;

      console.log(`[ICMarkets] setSlTp attempt ${attempt} — positionId type: ${typeof modBody.positionId}`);

      const result = await metaApiRequest(
        'POST',
        `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
        modBody
      );

      const code = result.stringCode || '';
      console.log(`[ICMarkets] setSlTp attempt ${attempt} code: ${code}`);

      if (code === 'TRADE_RETCODE_DONE' || code === 'TRADE_RETCODE_NO_CHANGES') {
        await sleep(1500);
        const pos = await verifyPosition(positionId);
        if (pos) {
          // Use tolerance of 10 — indices like JP225 may round stops to nearest tick
          const TOLERANCE = 10;
          const slOk = !hasSL || (pos.stopLoss  != null && Math.abs(pos.stopLoss  - sl) <= TOLERANCE);
          const tpOk = !hasTP || (pos.takeProfit != null && Math.abs(pos.takeProfit - tp) <= TOLERANCE);
          if (slOk && tpOk) {
            console.log(`[ICMarkets] SL/TP confirmed — SL: ${pos.stopLoss}  TP: ${pos.takeProfit}`);
            return { success: true, verified: true, raw: result };
          }
          console.log(`[ICMarkets] MetaAPI DONE but stops not matching — SL in MT5: ${pos.stopLoss}  expected: ${sl}  TP in MT5: ${pos.takeProfit}  expected: ${tp}`);
        }
      }

      if (code === 'TRADE_RETCODE_INVALID_STOPS') {
        console.log(`[ICMarkets] INVALID_STOPS — SL: ${sl}  TP: ${tp}`);
        return { success: false, reason: 'INVALID_STOPS', raw: result };
      }

    } catch (err) {
      console.log(`[ICMarkets] setSlTp attempt ${attempt} error: ${err.message}`);
      if (attempt === retries) {
        return { success: false, reason: err.message };
      }
    }
  }

  return { success: false, reason: 'max retries — stops not confirmed' };
}

async function placeMarketOrder(signal) {
  const mtSymbol   = resolveSymbol(signal.symbol);
  const actionType = signal.type === 'LONG' ? 'ORDER_TYPE_BUY' : 'ORDER_TYPE_SELL';
  const volume     = parseFloat(signal.brokerSize) || 1;

  const sl = parseFloat(signal.sl)  || null;
  const tp = parseFloat(signal.tp1) || parseFloat(signal.tp) || null;

  console.log(`[ICMarkets] Placing ${actionType} on ${mtSymbol}  size=${volume}  SL=${sl}  TP=${tp}`);

  // ── Include SL/TP directly in the initial order body ──────────────────
  // This is the most reliable method — atomic order+stops in one call.
  // MetaAPI supports stopLoss/takeProfit on ORDER_TYPE_BUY and ORDER_TYPE_SELL.
  const orderBody = { actionType, symbol: mtSymbol, volume };
  if (sl && sl > 0) orderBody.stopLoss   = sl;
  if (tp && tp > 0) orderBody.takeProfit = tp;

  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    orderBody
  );

  const positionId = result.positionId || null;
  const orderId    = result.orderId    || null;
  const retCode    = result.stringCode || null;

  console.log(`[ICMarkets] Order result — positionId: ${positionId}  orderId: ${orderId}  code: ${retCode}`);

  if (!positionId && !orderId) {
    throw new Error(`MT5 rejected order: ${retCode || 'unknown'} — ${result.message || JSON.stringify(result)}`);
  }

  // ── Verify position registered and stops are set ───────────────────────
  await sleep(3000);
  const posCheck = await verifyPosition(positionId);

  let slTpResult = { success: true, skipped: false, note: 'included in initial order' };

  // If verification shows SL/TP missing despite being in the order,
  // fall back to explicit POSITION_MODIFY
  if (posCheck && (sl || tp)) {
    const TOLERANCE = 10;
    const slMissing = sl && (posCheck.stopLoss  == null || Math.abs(posCheck.stopLoss  - sl) > TOLERANCE);
    const tpMissing = tp && (posCheck.takeProfit == null || Math.abs(posCheck.takeProfit - tp) > TOLERANCE);

    if (slMissing || tpMissing) {
      console.log(`[ICMarkets] SL/TP not in initial order result — falling back to POSITION_MODIFY`);
      console.log(`[ICMarkets] Current SL: ${posCheck.stopLoss}  Current TP: ${posCheck.takeProfit}`);
      slTpResult = await setSlTp(positionId, sl, tp);
    } else {
      console.log(`[ICMarkets] SL/TP confirmed in initial order — SL: ${posCheck.stopLoss}  TP: ${posCheck.takeProfit}`);
    }
  } else if (!posCheck && positionId && (sl || tp)) {
    console.log(`[ICMarkets] Position not visible yet — attempting POSITION_MODIFY as fallback`);
    slTpResult = await setSlTp(positionId, sl, tp);
  }

  if (slTpResult.success === false) {
    console.log(`[ICMarkets] WARNING: SL/TP not confirmed — reason: ${slTpResult.reason}`);
    console.log(`[ICMarkets] Trade is open. Bridge tracks SL/TP internally.`);
  }

  return {
    success:    true,
    positionId,
    orderId,
    dealRef:    positionId || orderId,
    slTpSet:    slTpResult.success === true,
    slTpReason: slTpResult.reason || slTpResult.note || null,
    raw:        result,
  };
}

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

async function closePosition(positionId) {
  console.log(`[ICMarkets] Closing position ${positionId}`);
  const result = await metaApiRequest(
    'POST',
    `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
    { actionType: 'POSITION_CLOSE_ID', positionId: String(positionId) }
  );
  return { success: true, raw: result };
}

async function getOpenPositions() {
  return await metaApiRequest('GET', `/users/current/accounts/${METAAPI_ACCOUNT_ID}/positions`);
}

async function getAccountInfo() {
  return await metaApiRequest('GET', `/users/current/accounts/${METAAPI_ACCOUNT_ID}/account-information`);
}

async function searchSymbols(query) {
  try {
    const result = await metaApiRequest('GET', `/users/current/accounts/${METAAPI_ACCOUNT_ID}/symbols`);
    if (Array.isArray(result)) {
      return result.filter(s =>
        (typeof s === 'string' ? s : s.symbol || '').toLowerCase().includes(query.toLowerCase())
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