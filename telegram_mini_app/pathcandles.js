/**
 * 決済リザルトの path(実 tick 列)をローソク足にリサンプルする。
 *
 * リザルトの正本データは価格の点列で OHLC を持たない。表示専用に
 * 点列を K 個のバケツへ等分し、各バケツの 始値/高値/安値/終値 を取る。
 * 新しい価格を発明しない(全て path に実在した値)。点が少なすぎて
 * ローソクにならない場合(両端しか無い記録など)は null を返し、
 * 呼び出し側は従来のトレイル描画に落ちる。
 */
export function candlesFromPath(path, maxCandles = 26) {
  if (!Array.isArray(path)) return null;
  const values = path.map(Number).filter(Number.isFinite);
  if (values.length < 8) return null;
  const count = Math.max(6, Math.min(maxCandles, Math.floor(values.length / 3)));
  const candles = [];
  for (let k = 0; k < count; k += 1) {
    const start = Math.floor((k * values.length) / count);
    const end = Math.max(start + 1, Math.floor(((k + 1) * values.length) / count));
    const bucket = values.slice(start, end);
    candles.push({
      o: bucket[0],
      h: Math.max(...bucket),
      l: Math.min(...bucket),
      c: bucket[bucket.length - 1],
    });
  }
  return candles;
}
