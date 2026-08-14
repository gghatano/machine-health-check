// マシンメトリクス ダッシュボード
//
// Google スプレッドシート(metrics シート)を gviz の CSV 出力として直接読み、
// ブラウザだけで描画する。サーバも認証も無し(共有設定が「リンクを知っている全員が閲覧可」であること)。
//
// 設計上の要点: 「値が 0」と「記録が無い(欠測)」を決して混同しない。
// 欠測は 0 で補完せず、線を途切れさせたうえで網掛けで示す。

const DEFAULTS = {
  sheetId: '126zgKBmQb1BsbEXgZ26Yg6Z4jmmCcCpvPRmN5zkK3Rk',
  sheetName: 'metrics',
  intervalMin: 30,      // 収集間隔(systemd timer)
  gapFactor: 1.5,       // これを超える間隔が空いたら欠測とみなす
  warnAfterMin: 45,     // 最新データがこれ以上古い: 遅延
  errorAfterMin: 90,    // 最新データがこれ以上古い: 停止の可能性
  refreshMs: 5 * 60 * 1000,
  tableRowCap: 500,
};

const CONFIG = { ...DEFAULTS, ...(window.MHC_CONFIG || {}) };

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

const INTERVAL_MS = CONFIG.intervalMin * MIN;
const GAP_MS = INTERVAL_MS * CONFIG.gapFactor;

const RANGES = {
  '24h': { label: '24時間', ms: DAY },
  '7d': { label: '1週間', ms: 7 * DAY },
  '30d': { label: '1ヶ月', ms: 30 * DAY },
};

// 数値として扱う列(それ以外は文字列のまま)
const NUMERIC_COLUMNS = new Set([
  'uptime_sec', 'cpu_percent', 'load_1m', 'load_5m', 'load_15m',
  'memory_used_gb', 'memory_total_gb', 'memory_percent',
  'swap_used_gb', 'swap_total_gb', 'swap_percent',
  'disk_used_gb', 'disk_total_gb', 'disk_percent',
  'bytes_recv', 'bytes_sent', 'recv_bytes_delta', 'sent_bytes_delta',
  'recv_mbps', 'sent_mbps', 'packets_recv', 'packets_sent',
  'errors_in', 'errors_out', 'drops_in', 'drops_out',
]);

const CHART_SPECS = [
  {
    id: 'cpu',
    title: 'CPU使用率',
    unit: '%',
    yMin: 0, yMax: 100,
    decimals: 1,
    series: [{ key: 'cpu_percent', label: 'CPU', slot: 1 }],
  },
  {
    id: 'load',
    title: 'Load Average',
    unit: '',
    yMin: 0,
    decimals: 2,
    series: [
      { key: 'load_1m', label: '1分', slot: 1 },
      { key: 'load_5m', label: '5分', slot: 2 },
      { key: 'load_15m', label: '15分', slot: 3 },
    ],
  },
  {
    id: 'memory',
    title: 'メモリ使用率',
    unit: '%',
    yMin: 0, yMax: 100,
    decimals: 1,
    series: [{ key: 'memory_percent', label: 'メモリ', slot: 1 }],
    note: (r) => `${fmtNum(r.memory_used_gb, 1)} / ${fmtNum(r.memory_total_gb, 1)} GB`,
  },
  {
    id: 'swap',
    title: 'Swap使用率',
    unit: '%',
    yMin: 0, yMax: 100,
    decimals: 1,
    series: [{ key: 'swap_percent', label: 'Swap', slot: 1 }],
    note: (r) => `${fmtNum(r.swap_used_gb, 1)} / ${fmtNum(r.swap_total_gb, 1)} GB`,
  },
  {
    id: 'disk',
    title: 'ディスク使用率',
    unit: '%',
    yMin: 0, yMax: 100,
    decimals: 1,
    series: [{ key: 'disk_percent', label: 'ディスク', slot: 1 }],
    note: (r) => `${fmtNum(r.disk_used_gb, 1)} / ${fmtNum(r.disk_total_gb, 1)} GB`,
  },
  {
    id: 'net_rate',
    title: 'ネットワーク速度（平均）',
    unit: 'Mbps',
    yMin: 0,
    decimals: 3,
    series: [
      { key: 'recv_mbps', label: '受信', slot: 1 },
      { key: 'sent_mbps', label: '送信', slot: 2 },
    ],
  },
  {
    id: 'net_volume',
    title: `ネットワーク転送量（${CONFIG.intervalMin}分あたり）`,
    unit: 'MB',
    yMin: 0,
    decimals: 1,
    // 欠測明けの1点は「欠測していた期間の合計」になるため、この系列では描かない
    skipAfterGap: true,
    staticNote: '欠測直後の点は、欠測期間ぶんの合計になるため描いていません。',
    series: [
      { key: 'recv_mb', label: '受信', slot: 1 },
      { key: 'sent_mb', label: '送信', slot: 2 },
    ],
  },
];

const TILE_SPECS = [
  { key: 'cpu_percent', label: 'CPU', unit: '%', decimals: 1 },
  { key: 'memory_percent', label: 'メモリ', unit: '%', decimals: 1, note: (r) => `${fmtNum(r.memory_used_gb, 1)} / ${fmtNum(r.memory_total_gb, 1)} GB` },
  { key: 'disk_percent', label: 'ディスク', unit: '%', decimals: 1, note: (r) => `${fmtNum(r.disk_used_gb, 1)} / ${fmtNum(r.disk_total_gb, 1)} GB` },
  { key: 'load_1m', label: 'Load (1分)', unit: '', decimals: 2 },
  { key: 'recv_mbps', label: '受信', unit: 'Mbps', decimals: 3 },
  { key: 'sent_mbps', label: '送信', unit: 'Mbps', decimals: 3 },
];

const TABLE_COLUMNS = [
  { key: 'cpu_percent', label: 'CPU %', decimals: 1 },
  { key: 'load_1m', label: 'Load 1分', decimals: 2 },
  { key: 'load_5m', label: 'Load 5分', decimals: 2 },
  { key: 'memory_percent', label: 'メモリ %', decimals: 1 },
  { key: 'swap_percent', label: 'Swap %', decimals: 1 },
  { key: 'disk_percent', label: 'ディスク %', decimals: 1 },
  { key: 'recv_mbps', label: '受信 Mbps', decimals: 3 },
  { key: 'sent_mbps', label: '送信 Mbps', decimals: 3 },
  { key: 'recv_mb', label: '受信 MB', decimals: 1 },
  { key: 'sent_mb', label: '送信 MB', decimals: 1 },
];

// ------------------------------------------------------------------
// 汎用ユーティリティ
// ------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

function fmtNum(value, decimals = 1) {
  if (value == null || !Number.isFinite(value)) return '—';
  return value.toLocaleString('ja-JP', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtDateTime(t) {
  const d = new Date(t);
  return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function fmtTimeOnly(t) {
  const d = new Date(t);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function fmtMonthDay(t) {
  const d = new Date(t);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

function fmtDuration(ms) {
  const totalMin = Math.round(ms / MIN);
  if (totalMin < 60) return `${totalMin}分`;
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  if (hours < 24) return minutes ? `${hours}時間${minutes}分` : `${hours}時間`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}日${restHours}時間` : `${days}日`;
}

// ------------------------------------------------------------------
// CSV 取得とパース
// ------------------------------------------------------------------

function csvUrl() {
  if (CONFIG.csvUrl) return `${CONFIG.csvUrl}${CONFIG.csvUrl.includes('?') ? '&' : '?'}_=${Date.now()}`;
  const base = `https://docs.google.com/spreadsheets/d/${CONFIG.sheetId}/gviz/tq`;
  const params = new URLSearchParams({
    tqx: 'out:csv',
    sheet: CONFIG.sheetName,
    headers: '1',
    _: String(Date.now()),
  });
  return `${base}?${params}`;
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 1; } else { inQuotes = false; }
      } else {
        field += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ',') {
      row.push(field); field = '';
    } else if (c === '\n') {
      row.push(field); rows.push(row); row = []; field = '';
    } else if (c !== '\r') {
      field += c;
    }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function toRecords(csvRows) {
  if (!csvRows.length) return [];
  const header = csvRows[0].map((h) => h.trim());
  const records = [];

  for (const cells of csvRows.slice(1)) {
    if (!cells.some((c) => c.trim() !== '')) continue;
    const record = {};
    header.forEach((name, i) => {
      const raw = (cells[i] ?? '').trim();
      if (NUMERIC_COLUMNS.has(name)) {
        record[name] = raw === '' ? null : Number(raw);
        if (Number.isNaN(record[name])) record[name] = null;
      } else {
        record[name] = raw;
      }
    });

    const t = Date.parse(record.timestamp);
    if (!Number.isFinite(t)) continue;
    record.t = t;

    // 転送量は bytes の差分から導出する(欠測明けの差分は間隔が長い点に注意)
    record.recv_mb = record.recv_bytes_delta == null ? null : record.recv_bytes_delta / (1024 ** 2);
    record.sent_mb = record.sent_bytes_delta == null ? null : record.sent_bytes_delta / (1024 ** 2);

    records.push(record);
  }

  records.sort((a, b) => a.t - b.t);
  return records;
}

async function fetchRecords() {
  const response = await fetch(csvUrl(), { credentials: 'omit', cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`スプレッドシートの取得に失敗しました (HTTP ${response.status})`);
  }
  const text = await response.text();
  if (text.trimStart().startsWith('<')) {
    throw new Error('スプレッドシートを読み取れませんでした。共有設定が「リンクを知っている全員が閲覧可」になっているか確認してください。');
  }
  return toRecords(parseCsv(text));
}

// ------------------------------------------------------------------
// 欠測の検出
// ------------------------------------------------------------------

// 全期間の行から、表示範囲 [xMin, xMax] に重なる欠測区間を求める。
// 最初の記録より前は「まだ記録が始まっていない」だけなので欠測にしない。
function detectGaps(allRows, xMin, xMax, now) {
  const gaps = [];
  for (let i = 1; i < allRows.length; i += 1) {
    const start = allRows[i - 1].t;
    const end = allRows[i].t;
    if (end - start > GAP_MS && end > xMin && start < xMax) {
      gaps.push({ start, end, ongoing: false });
    }
  }

  const last = allRows.length ? allRows[allRows.length - 1].t : null;
  const edge = Math.min(xMax, now);
  if (last != null && edge - last > GAP_MS && last < xMax) {
    gaps.push({ start: last, end: edge, ongoing: true });
  }
  return gaps;
}

// 表示範囲に収めたうえで、その欠測で失われた記録回数を見積もる
function missedSamples(gap, xMin, xMax) {
  const start = Math.max(gap.start, xMin);
  const end = Math.min(gap.end, xMax);
  const duration = Math.max(0, end - start);
  if (gap.ongoing || start > gap.start) return Math.floor(duration / INTERVAL_MS);
  return Math.max(1, Math.round(duration / INTERVAL_MS) - 1);
}

function clampedGapDuration(gap, xMin, xMax) {
  return Math.max(0, Math.min(gap.end, xMax) - Math.max(gap.start, xMin));
}

// ------------------------------------------------------------------
// 目盛り
// ------------------------------------------------------------------

function niceScale(maxValue, tickCount = 4) {
  if (!(maxValue > 0)) return { max: 1, step: 0.25 };
  const raw = maxValue / tickCount;
  const exponent = 10 ** Math.floor(Math.log10(raw));
  const f = raw / exponent;
  const m = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  const step = m * exponent;
  return { max: Math.ceil(maxValue / step) * step, step };
}

const TIME_STEPS = [
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR,
  DAY, 2 * DAY, 5 * DAY, 7 * DAY, 14 * DAY,
];

function timeTicks(xMin, xMax, plotWidth) {
  const span = xMax - xMin;
  const maxTicks = Math.max(2, Math.floor(plotWidth / 74));
  const step = TIME_STEPS.find((s) => span / s <= maxTicks) ?? TIME_STEPS[TIME_STEPS.length - 1];
  const useDate = span > 26 * HOUR;

  const cursor = new Date(xMin);
  cursor.setMinutes(0, 0, 0);
  if (step >= DAY) {
    cursor.setHours(0, 0, 0, 0);
  } else {
    const stepHours = step / HOUR;
    cursor.setHours(cursor.getHours() - (cursor.getHours() % stepHours));
  }

  const ticks = [];
  let t = cursor.getTime();
  while (t <= xMax && ticks.length < 40) {
    if (t >= xMin) ticks.push({ t, label: useDate ? fmtMonthDay(t) : fmtTimeOnly(t) });
    if (step >= DAY) {
      const next = new Date(t);
      next.setDate(next.getDate() + step / DAY);
      next.setHours(0, 0, 0, 0);
      t = next.getTime();
    } else {
      t += step;
    }
  }
  return ticks;
}

// ------------------------------------------------------------------
// SVG 折れ線チャート
// ------------------------------------------------------------------

const SVG_NS = 'http://www.w3.org/2000/svg';
const PAD = { left: 46, right: 12, top: 10, bottom: 24 };
const PLOT_HEIGHT = 168;

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(name, String(value));
  }
  return node;
}

function seriesColor(slot) {
  return `var(--series-${slot})`;
}

class Chart {
  constructor(spec) {
    this.spec = spec;
    this.view = null;
    this.cursorIndex = null;

    const card = document.createElement('section');
    card.className = 'chart-card';

    const head = document.createElement('div');
    head.className = 'chart-head';
    const title = document.createElement('h2');
    title.className = 'chart-title';
    title.textContent = spec.title + (spec.unit ? `（${spec.unit}）` : '');
    this.latestEl = document.createElement('span');
    this.latestEl.className = 'chart-latest';
    head.append(title, this.latestEl);
    card.append(head);

    // 系列が 2 本以上のときだけ凡例を出す(1 本はタイトルが名前になっている)
    if (spec.series.length > 1) {
      const legend = document.createElement('div');
      legend.className = 'legend';
      for (const s of spec.series) {
        const item = document.createElement('span');
        item.className = 'legend-item';
        const line = document.createElement('span');
        line.className = 'legend-line';
        line.style.color = seriesColor(s.slot);
        const label = document.createElement('span');
        label.textContent = s.label;
        item.append(line, label);
        legend.append(item);
      }
      card.append(legend);
    }

    this.plot = document.createElement('div');
    this.plot.className = 'plot';

    this.overlay = document.createElement('div');
    this.overlay.className = 'plot-overlay';
    this.overlay.tabIndex = 0;
    this.overlay.setAttribute('role', 'application');
    this.overlay.setAttribute('aria-label', `${spec.title}のグラフ。左右キーで値を読み上げます。`);

    this.tooltip = document.createElement('div');
    this.tooltip.className = 'tooltip';
    this.tooltip.hidden = true;

    this.plot.append(this.overlay, this.tooltip);
    card.append(this.plot);

    this.noteEl = document.createElement('p');
    this.noteEl.className = 'chart-note';
    card.append(this.noteEl);

    this.el = card;
    this.bindPointer();
  }

  bindPointer() {
    const onMove = (event) => {
      if (!this.view) return;
      const rect = this.plot.getBoundingClientRect();
      this.showAt(event.clientX - rect.left);
    };
    this.overlay.addEventListener('pointermove', onMove);
    this.overlay.addEventListener('pointerdown', onMove);
    this.overlay.addEventListener('pointerleave', () => this.hideCursor());
    this.overlay.addEventListener('blur', () => this.hideCursor());

    this.overlay.addEventListener('keydown', (event) => {
      if (!this.view || !this.view.rows.length) return;
      const last = this.view.rows.length - 1;
      let index = this.cursorIndex;
      if (event.key === 'ArrowRight') index = index == null ? 0 : Math.min(last, index + 1);
      else if (event.key === 'ArrowLeft') index = index == null ? last : Math.max(0, index - 1);
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = last;
      else if (event.key === 'Escape') { this.hideCursor(); return; }
      else return;
      event.preventDefault();
      this.showRow(index);
    });
  }

  draw(view) {
    this.view = view;
    this.hideCursor();

    const width = Math.max(240, this.plot.clientWidth || this.el.clientWidth || 320);
    const height = PLOT_HEIGHT + PAD.top + PAD.bottom;
    const plotW = width - PAD.left - PAD.right;
    const plotH = PLOT_HEIGHT;

    this.geom = { width, height, plotW, plotH, xMin: view.xMin, xMax: view.xMax };

    const svg = svgEl('svg', {
      width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
      'aria-label': this.ariaSummary(view),
    });

    // 欠測ハッチ
    const defs = svgEl('defs');
    const patternId = `hatch-${this.spec.id}`;
    const pattern = svgEl('pattern', {
      id: patternId, width: 7, height: 7,
      patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)',
    });
    pattern.style.color = 'var(--text-muted)';
    pattern.append(svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 7, stroke: 'currentColor', 'stroke-width': 1.2, opacity: 0.45 }));
    defs.append(pattern);
    svg.append(defs);

    // Y スケール
    const values = [];
    for (const s of this.spec.series) {
      for (const row of view.rows) {
        const v = row[s.key];
        if (v != null && Number.isFinite(v)) values.push(v);
      }
    }
    const dataMax = values.length ? Math.max(...values) : 0;
    let yMax = this.spec.yMax;
    let step;
    if (yMax != null) {
      step = yMax / 4;
    } else {
      const scale = niceScale(dataMax);
      yMax = scale.max;
      step = scale.step;
    }
    const yMin = this.spec.yMin ?? 0;

    const x = (t) => PAD.left + ((t - view.xMin) / (view.xMax - view.xMin)) * plotW;
    const y = (v) => PAD.top + (1 - (v - yMin) / (yMax - yMin)) * plotH;
    this.scales = { x, y, yMax, yMin };

    // グリッドと Y ラベル
    // 目盛り幅をちょうど表せる桁数(0.02 なら 2桁、2.5 なら 1桁)
    let decimals = 0;
    while (decimals < 4 && Math.abs(step * 10 ** decimals - Math.round(step * 10 ** decimals)) > 1e-9) {
      decimals += 1;
    }
    for (let v = yMin; v <= yMax + step / 2; v += step) {
      const py = Math.round(y(v)) + 0.5;
      const line = svgEl('line', { x1: PAD.left, y1: py, x2: PAD.left + plotW, y2: py, 'stroke-width': 1 });
      line.style.stroke = v === yMin ? 'var(--baseline)' : 'var(--gridline)';
      svg.append(line);

      const label = svgEl('text', { x: PAD.left - 8, y: py + 4, 'text-anchor': 'end', 'font-size': 11 });
      label.style.fill = 'var(--text-muted)';
      label.style.fontVariantNumeric = 'tabular-nums';
      label.textContent = fmtNum(v, decimals);
      svg.append(label);
    }

    // X ラベル
    for (const tick of timeTicks(view.xMin, view.xMax, plotW)) {
      const px = Math.round(x(tick.t)) + 0.5;
      const line = svgEl('line', { x1: px, y1: PAD.top, x2: px, y2: PAD.top + plotH, 'stroke-width': 1 });
      line.style.stroke = 'var(--gridline)';
      svg.append(line);

      const label = svgEl('text', { x: px, y: PAD.top + plotH + 16, 'text-anchor': 'middle', 'font-size': 11 });
      label.style.fill = 'var(--text-muted)';
      label.style.fontVariantNumeric = 'tabular-nums';
      label.textContent = tick.label;
      svg.append(label);
    }

    // 欠測帯(データが 0 なのではなく、記録が無い区間)
    for (const gap of view.gaps) {
      const gx = Math.max(PAD.left, x(Math.max(gap.start, view.xMin)));
      const gxEnd = Math.min(PAD.left + plotW, x(Math.min(gap.end, view.xMax)));
      const w = gxEnd - gx;
      if (w <= 0.5) continue;
      svg.append(svgEl('rect', {
        x: gx, y: PAD.top, width: w, height: plotH,
        fill: `url(#${patternId})`,
      }));
      if (w > 44) {
        const label = svgEl('text', {
          x: gx + w / 2, y: PAD.top + 14, 'text-anchor': 'middle', 'font-size': 10,
        });
        label.style.fill = 'var(--text-muted)';
        label.textContent = '欠測';
        svg.append(label);
      }
    }

    // 折れ線(欠測やセル空白でセグメントを分割する)
    // 点が詰まっているとき(1ヶ月表示など)は線を細くして、重なりで潰れないようにする
    const strokeWidth = view.rows.length > plotW / 3 ? 1.4 : 2;
    for (const s of this.spec.series) {
      const segments = this.segmentsFor(s.key, view.rows);
      for (const segment of segments) {
        if (segment.length === 1) {
          const dot = svgEl('circle', { cx: x(segment[0].t), cy: y(segment[0].v), r: 2.6 });
          dot.style.fill = seriesColor(s.slot);
          svg.append(dot);
          continue;
        }
        const d = segment.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ');
        const path = svgEl('path', { d, fill: 'none', 'stroke-width': strokeWidth, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
        path.style.stroke = seriesColor(s.slot);
        svg.append(path);
      }
    }

    // カーソル(十字線と各系列のマーカー)
    this.cursorLine = svgEl('line', { y1: PAD.top, y2: PAD.top + plotH, 'stroke-width': 1, visibility: 'hidden' });
    this.cursorLine.style.stroke = 'var(--text-muted)';
    svg.append(this.cursorLine);

    this.cursorDots = this.spec.series.map((s) => {
      const dot = svgEl('circle', { r: 3.5, visibility: 'hidden', 'stroke-width': 2 });
      dot.style.fill = seriesColor(s.slot);
      dot.style.stroke = 'var(--surface)';
      svg.append(dot);
      return dot;
    });

    if (this.svg) this.svg.remove();
    this.svg = svg;
    this.plot.prepend(svg);

    this.updateHeadline(view);
  }

  segmentsFor(key, rows) {
    const segments = [];
    let current = [];
    let previousT = null;
    for (const row of rows) {
      const v = row[key];
      const missing = v == null || !Number.isFinite(v);
      const jumped = previousT != null && row.t - previousT > GAP_MS;
      previousT = row.t;

      if (missing || jumped) {
        if (current.length) segments.push(current);
        current = [];
      }
      if (missing || (jumped && this.spec.skipAfterGap)) continue;
      current.push({ t: row.t, v });
    }
    if (current.length) segments.push(current);
    return segments;
  }

  updateHeadline(view) {
    const latest = view.rows.length ? view.rows[view.rows.length - 1] : null;
    if (!latest) {
      this.latestEl.textContent = '期間内にデータなし';
      this.noteEl.textContent = '';
      return;
    }
    const parts = this.spec.series.map((s) => {
      const v = latest[s.key];
      const shown = v == null ? '—' : fmtNum(v, this.spec.decimals);
      return this.spec.series.length > 1 ? `${s.label} ${shown}` : shown;
    });
    this.latestEl.textContent = `最新 ${parts.join(' / ')}${this.spec.unit ? ` ${this.spec.unit}` : ''}`;
    this.noteEl.textContent = this.spec.note ? this.spec.note(latest) : (this.spec.staticNote ?? '');
  }

  ariaSummary(view) {
    const names = this.spec.series.map((s) => s.label).join('、');
    return `${this.spec.title}の時系列グラフ。系列: ${names}。データ ${view.rows.length} 件、欠測 ${view.gaps.length} 区間。`;
  }

  showAt(px) {
    const { plotW, xMin, xMax } = this.geom;
    const ratio = (px - PAD.left) / plotW;
    const t = xMin + ratio * (xMax - xMin);
    const rows = this.view.rows;

    const gap = this.view.gaps.find((g) => t > g.start && t < g.end);
    const index = nearestIndex(rows, t);
    const near = index != null && Math.abs(rows[index].t - t) <= INTERVAL_MS * 0.6;

    if (gap && !near) {
      this.showMissing(gap, t);
      return;
    }
    if (index == null) return;
    this.showRow(index);
  }

  showRow(index) {
    const row = this.view.rows[index];
    if (!row) return;
    this.cursorIndex = index;

    const px = this.scales.x(row.t);
    this.cursorLine.setAttribute('x1', px);
    this.cursorLine.setAttribute('x2', px);
    this.cursorLine.setAttribute('visibility', 'visible');

    this.spec.series.forEach((s, i) => {
      const v = row[s.key];
      const dot = this.cursorDots[i];
      if (v == null || !Number.isFinite(v)) {
        dot.setAttribute('visibility', 'hidden');
        return;
      }
      dot.setAttribute('cx', px);
      dot.setAttribute('cy', this.scales.y(v));
      dot.setAttribute('visibility', 'visible');
    });

    const time = document.createElement('p');
    time.className = 'tooltip-time';
    time.textContent = fmtDateTime(row.t);

    const frag = document.createDocumentFragment();
    frag.append(time);
    for (const s of this.spec.series) {
      const line = document.createElement('div');
      line.className = 'tooltip-row';
      line.style.color = seriesColor(s.slot);

      const key = document.createElement('span');
      key.className = 'tooltip-key';

      const value = document.createElement('span');
      value.className = 'tooltip-value';
      const v = row[s.key];
      value.textContent = v == null || !Number.isFinite(v)
        ? '—'
        : `${fmtNum(v, this.spec.decimals)}${this.spec.unit ? ` ${this.spec.unit}` : ''}`;

      const label = document.createElement('span');
      label.className = 'tooltip-label';
      label.textContent = s.label;

      line.append(key, value, label);
      frag.append(line);
    }

    this.paintTooltip(frag, px);
  }

  showMissing(gap, t) {
    this.cursorIndex = null;
    const px = this.scales.x(t);
    this.cursorLine.setAttribute('x1', px);
    this.cursorLine.setAttribute('x2', px);
    this.cursorLine.setAttribute('visibility', 'visible');
    for (const dot of this.cursorDots) dot.setAttribute('visibility', 'hidden');

    const time = document.createElement('p');
    time.className = 'tooltip-time';
    time.textContent = `${fmtDateTime(gap.start)} → ${gap.ongoing ? '現在' : fmtDateTime(gap.end)}`;

    const body = document.createElement('div');
    body.className = 'tooltip-missing';
    const strong = document.createElement('span');
    strong.className = 'tooltip-value';
    strong.textContent = 'データなし';
    body.append(strong, document.createTextNode(` （${fmtDuration(gap.end - gap.start)}）`));

    const frag = document.createDocumentFragment();
    frag.append(time, body);
    this.paintTooltip(frag, px);
  }

  paintTooltip(fragment, px) {
    this.tooltip.replaceChildren(fragment);
    this.tooltip.hidden = false;
    const width = this.tooltip.offsetWidth;
    const max = this.geom.width - width - 4;
    this.tooltip.style.left = `${Math.max(4, Math.min(max, px + 14))}px`;
    this.tooltip.style.top = '6px';
  }

  hideCursor() {
    this.cursorIndex = null;
    this.tooltip.hidden = true;
    if (this.cursorLine) this.cursorLine.setAttribute('visibility', 'hidden');
    if (this.cursorDots) for (const dot of this.cursorDots) dot.setAttribute('visibility', 'hidden');
  }
}

function nearestIndex(rows, t) {
  if (!rows.length) return null;
  let low = 0;
  let high = rows.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (rows[mid].t < t) low = mid + 1; else high = mid;
  }
  const candidates = [low - 1, low].filter((i) => i >= 0 && i < rows.length);
  return candidates.reduce((best, i) => (
    best == null || Math.abs(rows[i].t - t) < Math.abs(rows[best].t - t) ? i : best
  ), null);
}

// ------------------------------------------------------------------
// 画面全体の状態
// ------------------------------------------------------------------

const state = {
  records: [],
  host: null,
  range: '24h',
  fetchedAt: null,
  error: null,
};

const charts = CHART_SPECS.map((spec) => new Chart(spec));

function hostRecords() {
  if (!state.host) return state.records;
  return state.records.filter((r) => r.hostname === state.host);
}

function buildView() {
  const rows = hostRecords();
  const now = Date.now();
  const xMax = now;
  const xMin = xMax - RANGES[state.range].ms;
  const inRange = rows.filter((r) => r.t >= xMin && r.t <= xMax);
  return { rows: inRange, allRows: rows, gaps: detectGaps(rows, xMin, xMax, now), xMin, xMax, now };
}

function render() {
  const view = buildView();
  renderStatus(view);
  renderTiles(view);
  for (const chart of charts) chart.draw(view);
  if ($('table-view').open) renderTable(view);
}

function renderStatus(view) {
  const rows = view.allRows;
  const latest = rows.length ? rows[rows.length - 1] : null;
  const card = $('state-card');
  card.classList.remove('state-ok', 'state-warn', 'state-error');

  if (!latest) {
    card.classList.add('state-error');
    $('state-label').textContent = 'データなし';
    $('state-note').textContent = 'スプレッドシートに記録がありません';
    $('latest-ts').textContent = '—';
    $('latest-age').textContent = '';
    $('uptime').textContent = '—';
    $('uptime-note').textContent = '';
  } else {
    const ageMs = view.now - latest.t;
    const ageMin = ageMs / MIN;
    let cls = 'state-ok';
    let label = '正常';
    let note = `${CONFIG.intervalMin}分間隔で記録されています`;
    if (ageMin > CONFIG.errorAfterMin) {
      cls = 'state-error';
      label = '記録が停止しています';
      note = `${fmtDuration(ageMs)}以上データがありません`;
    } else if (ageMin > CONFIG.warnAfterMin) {
      cls = 'state-warn';
      label = '記録が遅れています';
      note = `直近の記録から${fmtDuration(ageMs)}経過`;
    }
    card.classList.add(cls);
    $('state-label').textContent = label;
    $('state-note').textContent = note;

    $('latest-ts').textContent = fmtDateTime(latest.t);
    $('latest-age').textContent = `${fmtDuration(ageMs)}前`;

    $('uptime').textContent = latest.uptime_sec == null ? '—' : fmtDuration(latest.uptime_sec * 1000);
    $('uptime-note').textContent = latest.hostname ? latest.hostname : '';
  }

  const gaps = view.gaps;
  const totalMissed = gaps.reduce((sum, g) => sum + missedSamples(g, view.xMin, view.xMax), 0);
  const totalMs = gaps.reduce((sum, g) => sum + clampedGapDuration(g, view.xMin, view.xMax), 0);
  $('gap-count').textContent = gaps.length ? `${gaps.length}区間 / ${totalMissed}回` : 'なし';
  $('gap-note').textContent = gaps.length
    ? `合計 ${fmtDuration(totalMs)}（記録が無い期間）`
    : `${RANGES[state.range].label}の記録は途切れていません`;

  const host = latest?.hostname || '';
  $('host-label').textContent = host
    ? `${host} ／ ${RANGES[state.range].label}の推移（${view.rows.length}件）`
    : `${RANGES[state.range].label}の推移（${view.rows.length}件）`;
}

function renderTiles(view) {
  const host = $('tiles');
  const latest = view.allRows.length ? view.allRows[view.allRows.length - 1] : null;
  host.replaceChildren();
  if (!latest) return;

  const stale = view.now - latest.t > CONFIG.warnAfterMin * MIN;

  for (const spec of TILE_SPECS) {
    const tile = document.createElement('div');
    tile.className = 'tile';

    const key = document.createElement('p');
    key.className = 'tile-key';
    key.textContent = spec.label;

    const value = document.createElement('p');
    value.className = 'tile-value';
    const v = latest[spec.key];
    if (v == null || !Number.isFinite(v)) {
      tile.classList.add('tile-missing');
      value.textContent = '—';
    } else {
      value.append(document.createTextNode(fmtNum(v, spec.decimals)));
      if (spec.unit) {
        const unit = document.createElement('span');
        unit.className = 'tile-unit';
        unit.textContent = spec.unit;
        value.append(unit);
      }
    }

    const note = document.createElement('p');
    note.className = 'tile-note';
    const parts = [];
    if (spec.note) parts.push(spec.note(latest));
    if (stale) parts.push(`${fmtDuration(view.now - latest.t)}前の値`);
    note.textContent = parts.join(' ／ ');

    tile.append(key, value, note);
    host.append(tile);
  }
}

function renderTable(view) {
  const host = $('table-host');
  host.replaceChildren();

  const rows = view.rows.slice().reverse();
  const shown = rows.slice(0, CONFIG.tableRowCap);

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const label of ['時刻', ...TABLE_COLUMNS.map((c) => c.label)]) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = label;
    headRow.append(th);
  }
  thead.append(headRow);

  const tbody = document.createElement('tbody');
  const gapsDesc = view.gaps.slice().sort((a, b) => b.start - a.start);

  // 新しい順に並べ、行と行のあいだに欠測区間を挟んで表示する
  const emitGap = (gap) => {
    const tr = document.createElement('tr');
    tr.className = 'missing-row';
    const td = document.createElement('td');
    td.colSpan = TABLE_COLUMNS.length + 1;
    const missed = missedSamples(gap, view.xMin, view.xMax);
    td.textContent = `欠測: ${fmtDateTime(gap.start)} → ${gap.ongoing ? '現在' : fmtDateTime(gap.end)}（${fmtDuration(gap.end - gap.start)} / 約${missed}回分）`;
    tr.append(td);
    tbody.append(tr);
  };

  for (const gap of gapsDesc) {
    if (gap.ongoing || (shown.length && gap.start >= shown[0].t)) { emitGap(gap); break; }
  }

  shown.forEach((row, i) => {
    const tr = document.createElement('tr');
    const time = document.createElement('td');
    time.textContent = fmtDateTime(row.t);
    tr.append(time);
    for (const column of TABLE_COLUMNS) {
      const td = document.createElement('td');
      const v = row[column.key];
      td.textContent = v == null || !Number.isFinite(v) ? '—' : fmtNum(v, column.decimals);
      tr.append(td);
    }
    tbody.append(tr);

    const next = shown[i + 1];
    if (next) {
      const gap = gapsDesc.find((g) => g.start === next.t && g.end === row.t);
      if (gap) emitGap(gap);
    }
  });

  table.append(thead, tbody);
  host.append(table);

  const caption = document.createElement('p');
  caption.className = 'table-cap';
  caption.textContent = rows.length > shown.length
    ? `新しい順に ${shown.length} 行を表示しています（期間内 ${rows.length} 行のうち）。`
    : `期間内の ${rows.length} 行すべてを表示しています。`;
  host.append(caption);
}

function showBanner(message) {
  const banner = $('banner');
  if (!message) { banner.hidden = true; banner.textContent = ''; return; }
  banner.hidden = false;
  banner.textContent = message;
}

async function load({ silent = false } = {}) {
  const chartsHost = $('charts');
  if (!silent) chartsHost.classList.add('is-stale');
  $('reload-btn').disabled = true;

  try {
    const records = await fetchRecords();
    state.records = records;
    state.fetchedAt = Date.now();
    state.error = null;
    showBanner(null);

    const hosts = [...new Set(records.map((r) => r.hostname).filter(Boolean))];
    if (!state.host || !hosts.includes(state.host)) {
      state.host = records.length ? records[records.length - 1].hostname : null;
    }
    renderHostFilter(hosts);
    render();
  } catch (error) {
    state.error = error;
    showBanner(`${error.message}（表示中のデータは ${state.fetchedAt ? fmtDateTime(state.fetchedAt) : '未取得'} 時点）`);
  } finally {
    chartsHost.classList.remove('is-stale');
    $('reload-btn').disabled = false;
    $('fetched-at').textContent = state.fetchedAt ? `取得 ${fmtTimeOnly(state.fetchedAt)}` : '';
  }
}

function renderHostFilter(hosts) {
  const wrap = $('host-filter');
  const select = $('host-select');
  if (hosts.length <= 1) { wrap.hidden = true; return; }
  wrap.hidden = false;
  select.replaceChildren();
  for (const host of hosts) {
    const option = document.createElement('option');
    option.value = host;
    option.textContent = host;
    option.selected = host === state.host;
    select.append(option);
  }
}

function selectRange(range) {
  state.range = range;
  for (const button of document.querySelectorAll('.range-btn')) {
    button.setAttribute('aria-pressed', String(button.dataset.range === range));
  }
  const url = new URL(location.href);
  url.searchParams.set('range', range);
  history.replaceState(null, '', url);
}

function init() {
  $('sheet-link').href = `https://docs.google.com/spreadsheets/d/${CONFIG.sheetId}/edit`;
  $('charts').append(...charts.map((c) => c.el));

  const requested = new URLSearchParams(location.search).get('range');
  selectRange(RANGES[requested] ? requested : state.range);

  for (const button of document.querySelectorAll('.range-btn')) {
    button.addEventListener('click', () => {
      selectRange(button.dataset.range);
      render();
    });
  }

  $('host-select').addEventListener('change', (event) => {
    state.host = event.target.value;
    render();
  });

  $('reload-btn').addEventListener('click', () => load());
  $('table-view').addEventListener('toggle', () => {
    if ($('table-view').open) renderTable(buildView());
  });

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 150);
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.fetchedAt && Date.now() - state.fetchedAt > CONFIG.refreshMs) {
      load({ silent: true });
    }
  });

  setInterval(() => load({ silent: true }), CONFIG.refreshMs);
  load();
}

init();
