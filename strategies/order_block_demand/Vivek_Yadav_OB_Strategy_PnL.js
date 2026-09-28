/**
 * Vivek Yadav Order Block + Demand & Supply Zone Strategy with Exact PnL
 * ======================================================================
 * Platform / Engine: KLineChart / Custom Indicator Engine
 * Source: Advance Crypto Trader (Vivek Yadav)
 *
 * Rules:
 * 1. Fractal Swing Pivots:
 *    - pivotLen = 5 (evaluated at checkIdx = i - pivotLen, looking +/- 5 bars)
 * 2. Bullish BOS & Demand Zone Detection:
 *    - Break of Structure: cur.close > lastSwingHigh.val (with prev.close <= lastSwingHigh.val)
 *    - Origin: lowest candle (min low) between lastSwingHigh.idx + 1 and i - 1
 *    - Order Block: scan backward from origin up to 10 candles for the last RED candle (close < open)
 *    - OB range: full wick-to-wick (top = high, bottom = low)
 * 3. Bearish BOS & Supply Zone Detection:
 *    - Break of Structure: cur.close < lastSwingLow.val (with prev.close >= lastSwingLow.val)
 *    - Origin: highest candle (max high) between lastSwingLow.idx + 1 and i - 1
 *    - Order Block: scan backward from origin up to 10 candles for the last GREEN candle (close > open)
 *    - OB range: full wick-to-wick (top = high, bottom = low)
 * 4. Zone Testing, Mitigation & Invalidation:
 *    - Bullish:
 *      * Invalidation: candle closes below ob.bottom (cur.close < ob.bottom) -> zone deactivated
 *      * Retest Entry: candle touches zone (cur.low <= ob.top && cur.high >= ob.bottom)
 *        and closes GREEN (cur.close > cur.open) above ob.bottom
 *        -> entry = cur.close, sl = ob.bottom, tp = entry + 2.0 * (entry - sl) [1:2 RR]
 *    - Bearish:
 *      * Invalidation: candle closes above ob.top (cur.close > ob.top) -> zone deactivated
 *      * Retest Entry: candle touches zone (cur.high >= ob.bottom && cur.low <= ob.top)
 *        and closes RED (cur.close < cur.open) below ob.top
 *        -> entry = cur.close, sl = ob.top, tp = entry - 2.0 * (sl - entry) [1:2 RR]
 * 5. Trade Tracking:
 *    - 1:2 Risk to Reward exit tracking with conservative SL check.
 */

return {
  name: 'Vivek_Yadav_OB_Strategy_PnL',
  shortName: 'Vivek SMC + PnL',

  calc(dataList, indicator) {
    const pivotLen = 5; // Lookback window for Swing High/Low
    const n = dataList.length;
    
    // Engine requirement: calc MUST return an array matching dataList length
    const results = new Array(n).fill({}); 

    const bullOBs = [];
    const bearOBs = [];
    const drawnZones = [];
    const drawnTrades = [];
    const activeTrades = [];

    let lastSwingHigh = null;
    let lastSwingLow = null;

    // Helper functions
    const isRed = (i) => dataList[i].close < dataList[i].open;
    const isGreen = (i) => dataList[i].close > dataList[i].open;

    for (let i = pivotLen; i < n; i++) {
      const cur = dataList[i];
      const prev = dataList[i - 1];

      // 1. Detect Swing Pivots
      const checkIdx = i - pivotLen;
      let isSH = true, isSL = true;
      for (let j = checkIdx - pivotLen; j <= checkIdx + pivotLen; j++) {
        if (j < 0 || j >= n) continue;
        if (j !== checkIdx) {
          if (dataList[j].high > dataList[checkIdx].high) isSH = false;
          if (dataList[j].low < dataList[checkIdx].low) isSL = false;
        }
      }
      if (isSH) lastSwingHigh = { idx: checkIdx, val: dataList[checkIdx].high };
      if (isSL) lastSwingLow = { idx: checkIdx, val: dataList[checkIdx].low };

      // 2. Bullish BOS & Demand Zone Detection
      if (lastSwingHigh && prev.close <= lastSwingHigh.val && cur.close > lastSwingHigh.val) {
        let originIdx = lastSwingHigh.idx;
        let minVal = dataList[originIdx].low;
        for (let j = lastSwingHigh.idx + 1; j < i; j++) {
          if (dataList[j].low < minVal) {
            minVal = dataList[j].low;
            originIdx = j;
          }
        }
        
        let obIdx = originIdx;
        while (obIdx >= Math.max(0, originIdx - 10) && !isRed(obIdx)) obIdx--;
        if (obIdx < 0 || !isRed(obIdx)) obIdx = originIdx;

        const newOB = {
          type: 'bull',
          startIdx: obIdx,
          endIdx: i,
          top: dataList[obIdx].high,
          bottom: dataList[obIdx].low,
          active: true
        };
        bullOBs.push(newOB);
        drawnZones.push(newOB);
        lastSwingHigh = null;
      }

      // 3. Bearish BOS & Supply Zone Detection
      if (lastSwingLow && prev.close >= lastSwingLow.val && cur.close < lastSwingLow.val) {
        let originIdx = lastSwingLow.idx;
        let maxVal = dataList[originIdx].high;
        for (let j = lastSwingLow.idx + 1; j < i; j++) {
          if (dataList[j].high > maxVal) {
            maxVal = dataList[j].high;
            originIdx = j;
          }
        }
        
        let obIdx = originIdx;
        while (obIdx >= Math.max(0, originIdx - 10) && !isGreen(obIdx)) obIdx--;
        if (obIdx < 0 || !isGreen(obIdx)) obIdx = originIdx;

        const newOB = {
          type: 'bear',
          startIdx: obIdx,
          endIdx: i,
          top: dataList[obIdx].high,
          bottom: dataList[obIdx].low,
          active: true
        };
        bearOBs.push(newOB);
        drawnZones.push(newOB);
        lastSwingLow = null;
      }

      // 4. Test Zones for Mitigation, Invalidation & Trade Entry
      for (let b = 0; b < bullOBs.length; b++) {
        const ob = bullOBs[b];
        if (!ob.active) continue;
        ob.endIdx = i;

        if (cur.close < ob.bottom) {
          ob.active = false;
        } else if (cur.low <= ob.top && cur.high >= ob.bottom) {
          if (isGreen(i) && cur.close > ob.bottom) {
            ob.active = false;
            const entry = cur.close;
            const sl = ob.bottom;
            const tp = entry + 2.0 * (entry - sl);
            const trade = { type: 'long', startIdx: i, endIdx: i, entry, sl, tp, active: true };
            activeTrades.push(trade);
            drawnTrades.push(trade);
          }
        }
      }

      for (let b = 0; b < bearOBs.length; b++) {
        const ob = bearOBs[b];
        if (!ob.active) continue;
        ob.endIdx = i;

        if (cur.close > ob.top) {
          ob.active = false;
        } else if (cur.high >= ob.bottom && cur.low <= ob.top) {
          if (isRed(i) && cur.close < ob.top) {
            ob.active = false;
            const entry = cur.close;
            const sl = ob.top;
            const tp = entry - 2.0 * (sl - entry);
            const trade = { type: 'short', startIdx: i, endIdx: i, entry, sl, tp, active: true };
            activeTrades.push(trade);
            drawnTrades.push(trade);
          }
        }
      }

      // 5. Track Executed Trades & Calculate Exact PnL
      for (let t = 0; t < activeTrades.length; t++) {
        const trade = activeTrades[t];
        if (!trade.active) continue;
        trade.endIdx = i;

        // Conservative Check: Did it hit SL first?
        if (trade.type === 'long') {
          if (cur.low <= trade.sl) {
            trade.active = false;
            trade.outcome = 'loss';
            trade.exitPrice = trade.sl;
          } else if (cur.high >= trade.tp) {
            trade.active = false;
            trade.outcome = 'win';
            trade.exitPrice = trade.tp;
          }
          
          if (!trade.active) { // Trade just closed
            trade.pnlPts = trade.exitPrice - trade.entry;
            trade.pnlPct = (trade.pnlPts / trade.entry) * 100;
          }

        } else if (trade.type === 'short') {
          if (cur.high >= trade.sl) {
            trade.active = false;
            trade.outcome = 'loss';
            trade.exitPrice = trade.sl;
          } else if (cur.low <= trade.tp) {
            trade.active = false;
            trade.outcome = 'win';
            trade.exitPrice = trade.tp;
          }
          
          if (!trade.active) { // Trade just closed
            trade.pnlPts = trade.entry - trade.exitPrice;
            trade.pnlPct = (trade.pnlPts / trade.entry) * 100;
          }
        }
      }
    }

    indicator._zones = drawnZones;
    indicator._trades = drawnTrades;
    return results;
  },

  draw(args) {
    const { ctx, xAxis, yAxis, indicator, visibleRange } = args;
    if (!ctx || !xAxis || !yAxis || !indicator) return;

    const zones = indicator._zones || [];
    const trades = indicator._trades || [];

    const minX = visibleRange ? visibleRange.from : 0;
    const maxX = visibleRange ? visibleRange.to : 999999;
    const isVisible = (start, end) => (start <= maxX && end >= minX);

    // Render Demand & Supply Zones
    for (const z of zones) {
      if (!isVisible(z.startIdx, z.endIdx)) continue;
      const x1 = xAxis.convertToPixel(z.startIdx);
      const x2 = xAxis.convertToPixel(z.endIdx);
      const pyTop = yAxis.convertToPixel(Math.max(z.top, z.bottom));
      const pyBot = yAxis.convertToPixel(Math.min(z.top, z.bottom));
      const yMin = Math.min(pyTop, pyBot);
      const height = Math.abs(pyBot - pyTop);
      const width = Math.max(x2 - x1, 4); 

      ctx.save();
      if (z.type === 'bull') {
        ctx.fillStyle = 'rgba(0, 230, 118, 0.16)';
        ctx.strokeStyle = '#00E676';
      } else {
        ctx.fillStyle = 'rgba(255, 23, 68, 0.16)';
        ctx.strokeStyle = '#FF1744';
      }
      ctx.fillRect(x1, yMin, width, height);
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.strokeRect(x1, yMin, width, height);
      ctx.restore();
    }

    // Render Executed Trades & PnL Labels
    for (const t of trades) {
      if (!isVisible(t.startIdx, t.endIdx)) continue;
      const x1 = xAxis.convertToPixel(t.startIdx);
      const x2 = xAxis.convertToPixel(t.endIdx);
      const yEntry = yAxis.convertToPixel(t.entry);
      const yTP = yAxis.convertToPixel(t.tp);
      const ySL = yAxis.convertToPixel(t.sl);
      const width = Math.max(x2 - x1, 4);

      ctx.save();
      
      // 1. Profit Box (Green)
      const pyMinTP = Math.min(yEntry, yTP);
      const pHeightTP = Math.abs(yTP - yEntry);
      ctx.fillStyle = 'rgba(0, 230, 118, 0.28)';
      ctx.fillRect(x1, pyMinTP, width, pHeightTP);
      ctx.strokeStyle = '#00E676';
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(x1, pyMinTP, width, pHeightTP);

      // 2. Risk Box (Red)
      const pyMinSL = Math.min(yEntry, ySL);
      const pHeightSL = Math.abs(ySL - yEntry);
      ctx.fillStyle = 'rgba(255, 23, 68, 0.28)';
      ctx.fillRect(x1, pyMinSL, width, pHeightSL);
      ctx.strokeStyle = '#FF1744';
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(x1, pyMinSL, width, pHeightSL);

      // 3. Lines
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)'; ctx.moveTo(x1, yEntry); ctx.lineTo(x2, yEntry); ctx.stroke();
      ctx.beginPath(); ctx.strokeStyle = '#00E676'; ctx.setLineDash([3, 3]); ctx.moveTo(x1, yTP); ctx.lineTo(x2, yTP); ctx.stroke();
      ctx.beginPath(); ctx.strokeStyle = '#FF1744'; ctx.setLineDash([3, 3]); ctx.moveTo(x1, ySL); ctx.lineTo(x2, ySL); ctx.stroke();
      
      ctx.restore();

      // 4. Draw Outcome Label (WIN / LOSS + PnL)
      if (t.outcome) {
        ctx.save();
        const isWin = t.outcome === 'win';
        
        // Clean numeric formatting 
        const ptsStr = parseFloat(t.pnlPts.toFixed(4)).toString();
        const pctStr = t.pnlPct.toFixed(2);
        
        // Label Text Example: "WIN +450.5 (+2.00%)"
        const pnlText = `${isWin ? 'WIN' : 'LOSS'} ${t.pnlPts > 0 ? '+' : ''}${ptsStr} (${t.pnlPct > 0 ? '+' : ''}${pctStr}%)`;
        
        const pyExit = yAxis.convertToPixel(t.exitPrice);
        ctx.font = 'bold 12px sans-serif';
        const textW = ctx.measureText(pnlText).width;
        
        const padX = 6, padY = 6;
        const boxW = textW + padX * 2;
        const boxH = 14 + padY * 2;
        
        // Offset slightly to the right of the exit candle
        const labelX = x2 + 6; 
        const labelY = pyExit - boxH / 2;

        // Label Background
        ctx.fillStyle = isWin ? 'rgba(0, 200, 83, 0.9)' : 'rgba(213, 0, 0, 0.9)';
        ctx.beginPath();
        if (ctx.roundRect) {
          ctx.roundRect(labelX, labelY, boxW, boxH, 4);
        } else {
          ctx.fillRect(labelX, labelY, boxW, boxH);
        }
        ctx.fill();

        // Label Text
        ctx.fillStyle = '#ffffff';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(pnlText, labelX + padX, labelY + boxH / 2);
        ctx.restore();
      }
    }
  }
};
