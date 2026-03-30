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

// Read back a single position to verify SL/TP were actually set
async function verifyPosition(positionId) {
  try {
    const positions = await metaApiRequest(
      'GET',
      `/users/current/accounts/${METAAPI_ACCOUNT_ID}/positions`
    );
    if (!Array.isArray(positions)) return null;
    const pos = positions.find(p => String(p.id) === String(positionId));
    if (pos) {
      console.log(`[ICMarkets] Position ${positionId} verified — SL: ${pos.stopLoss ?? 'none'}  TP: ${pos.takeProfit ?? 'none'}  Price: ${pos.currentPrice}`);
    } else {
      console.log(`[ICMarkets] Position ${positionId} not found in open positions`);
    }
    return pos;
  } catch (err) {
    console.log(`[ICMarkets] verifyPosition error: ${err.message}`);
    return null;
  }
}

async function setSlTp(positionId, stopLoss, takeProfit, retries = 4, delayMs = 2000) {
  const sl = parseFloat(stopLoss);
  const tp = parseFloat(takeProfit);

  const hasSL = sl && sl > 0;
  const hasTP = tp && tp > 0;

  if (!hasSL && !hasTP) {
    console.log('[ICMarkets] setSlTp — no valid SL or TP provided, skipping');
    return { success: true, skipped: true };
  }

  // Try both string and numeric positionId — MetaAPI behaviour varies by account type
  const modBody = {
    actionType: 'POSITION_MODIFY',
    positionId: String(positionId),  // string first
  };
  if (hasSL) modBody.stopLoss   = sl;
  if (hasTP) modBody.takeProfit = tp;

  console.log(`[ICMarkets] Setting SL/TP on position ${positionId} — SL: ${hasSL ? sl : 'none'}  TP: ${hasTP ? tp : 'none'}`);

  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      await sleep(delayMs * attempt);

      // On attempt 2+, try with numeric positionId
      if (attempt === 2) {
        modBody.positionId = Number(positionId);
        console.log(`[ICMarkets] Attempt ${attempt}: switching to numeric positionId`);
      }

      const result = await metaApiRequest(
        'POST',
        `/users/current/accounts/${METAAPI_ACCOUNT_ID}/trade`,
        modBody
      );

      const code = result.stringCode || '';
      console.log(`[ICMarkets] setSlTp attempt ${attempt} code: ${code}`);

      if (code === 'TRADE_RETCODE_DONE' || code === 'TRADE_RETCODE_NO_CHANGES') {
        // Verify the stops actually landed by reading back the position
        await sleep(1000);
        const pos = await verifyPosition(positionId);

        if (pos) {
          const slOk = !hasSL || (pos.stopLoss && Math.abs(pos.stopLoss - sl) < 1);
          const tpOk = !hasTP || (pos.takeProfit && Math.abs(pos.takeProfit - tp) < 1);

          if (slOk && tpOk) {
            console.log(`[ICMarkets] ✅ SL/TP CONFIRMED in MT5 — SL: ${pos.stopLoss}  TP: ${pos.takeProfit}`);
            return { success: true, verified: true, raw: result };
          } else {
            console.log(`[ICMarkets] ⚠️ MetaAPI said DONE but stops NOT in MT5 — SL: ${pos.stopLoss}  TP: ${pos.takeProfit}`);
            console.log(`[ICMarkets] Expected SL: ${sl}  TP: ${tp}`);
            // Try again on next attempt
          }
        }
      }

      if (code === 'TRADE_RETCODE_INVALID_STOPS') {
        console.log(`[ICMarkets] INVALID_STOPS — SL: ${sl}  TP: ${tp} — check min stop distance`);
        return { success: false, reason: 'INVALID_STOPS', raw: result };
      }

    } catch (err) {
      console.log(`[ICMarkets] setSlTp attempt ${attempt} error: ${err.message}`);
      if (attempt === retries) {
        return { success: false, reason: err.message };
      }
    }
  }

  return { success: false, reason: 'max retries — stops not confirmed in MT5' };
}

async function placeMarketOrder(signal) {
  const mtSymbol   = resolveSymbol(signal.symbol);
  const actionType = signal.type === 'LONG' ? 'ORDER_TYPE_BUY' : 'ORDER_TYPE_SELL';
  const volume     = parseFloat(signal.brokerSize) || 1;

  console.log(`[ICMarkets] Placing ${actionType} on ${mtSymbol} size=${volume}`);

  const orderBody = { actionType, symbol: mtSymbol, volume };

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

  // Verify the position exists before trying to modify it
  console.log(`[ICMarkets] Waiting for position to register in MT5...`);
  await sleep(3000);
  const posCheck = await verifyPosition(positionId);
  if (!posCheck) {
    console.log(`[ICMarkets] Position not yet visible — will still attempt SL/TP set`);
  }

  let slTpResult = { success: false, skipped: true };

  if (positionId) {
    const sl = parseFloat(signal.sl)  || null;
    const tp = parseFloat(signal.tp1) || null;

    slTpResult = await setSlTp(positionId, sl, tp);

    if (!slTpResult.success && !slTpResult.skipped) {
      console.log(`[ICMarkets] ⚠️ SL/TP not confirmed — reason: ${slTpResult.reason}`);
      console.log(`[ICMarkets] Trade is open at broker. Bridge tracks SL/TP internally.`);
    }
  }

  return {
    success:    true,
    positionId,
    orderId,
    dealRef:    positionId || orderId,
    slTpSet:    slTpResult.success === true && !slTpResult.skipped,
    slTpReason: slTpResult.reason || null,
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