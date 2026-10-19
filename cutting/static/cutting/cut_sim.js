/* CraftFlow Cutting Station — cut-order simulation (Level A preview).
 * Reads <script type="application/json" data-cut-sim> inside each sheet panel,
 * derives a deterministic shelf cut sequence (rips per row, cross cuts between
 * parts) and animates it over the existing SVG map. Preview only — it never
 * alters the .CUT / CNC outputs. */
(() => {
  'use strict';

  const root = document.querySelector('.station-plan');
  if (!root) return;

  const modeTabs = [...root.querySelectorAll('[data-station-mode]')];
  const panels = [...root.querySelectorAll('[data-sheet-panel]')];
  const number = value => Number(String(value ?? '').replace(',', '.')) || 0;
  const fmt = value => number(value).toLocaleString('en-US', { maximumFractionDigits: 1 });

  /* ---------- mode switching ---------- */
  function setMode(mode) {
    root.dataset.mode = mode;
    modeTabs.forEach(tab => {
      const active = tab.dataset.stationMode === mode;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    if (mode === 'sim') {
      const panel = panels.find(item => !item.classList.contains('d-none'));
      if (panel) resetSim(panel);
    }
  }
  modeTabs.forEach(tab => tab.addEventListener('click', () => setMode(tab.dataset.stationMode)));

  /* ---------- sequence builder ---------- */
  function buildSequence(data) {
    const kerf = number(data.kerf);
    const parts = (data.parts || [])
      .map(part => ({ ...part, x: number(part.x), y: number(part.y), width: number(part.width), height: number(part.height) }))
      .sort((a, b) => (a.y - b.y) || (b.width * b.height - a.width * a.height) || (a.x - b.x));
    const rows = [];
    parts.forEach(part => {
      const row = rows.find(item => Math.abs(item.y - part.y) < 0.05);
      if (row) row.parts.push(part);
      else rows.push({ y: part.y, parts: [part] });
    });
    rows.forEach(row => {
      row.parts.sort((a, b) => a.x - b.x);
      row.height = Math.max(...row.parts.map(part => part.height));
      row.bottom = row.y + row.height;
    });
    rows.sort((a, b) => a.y - b.y);

    const sheetH = number(data.h);
    const sheetW = number(data.w);
    const cuts = [];
    rows.forEach((row, rowIndex) => {
      row.parts.forEach((part, index) => {
        if (index < row.parts.length - 1) {
          cuts.push({
            type: 'cross', label: 'عرضی', axis: 'x',
            at: part.x + part.width + kerf / 2, y: row.y + row.height / 2,
            pieces: [part],
          });
        }
      });
      const lastPart = row.parts[row.parts.length - 1];
      const nextRow = rows[rowIndex + 1];
      const ripY = nextRow ? nextRow.y - kerf / 2 : row.bottom + kerf / 2;
      cuts.push({
        type: 'rip', label: 'طولی', axis: 'y',
        at: Math.min(ripY, sheetH), x: sheetW / 2,
        pieces: [lastPart, ...row.parts.slice(0, -1).reverse()],
      });
    });
    if (!cuts.length && rows.length) {
      cuts.push({ type: 'trim', label: 'سربر', axis: 'y', at: sheetH, x: sheetW / 2, pieces: rows[0].parts });
    }
    return { cuts, sheetW, sheetH };
  }

  /* ---------- per-panel simulation ---------- */
  const SEQ_LABELS = { trim: 'سربر', rip: 'طولی', cross: 'عرضی' };

  /* Stored server-side sequence (Level B) — authoritative machine order. */
  function storedSequence(data) {
    const sheetW = number(data.w);
    const sheetH = number(data.h);
    const partByNo = {};
    (data.parts || []).forEach(part => {
      // Coordinates arrive as JSON strings; concatenation like "0" + 610 must
      // never leak into geometry (regression: cross-cut Y became "0610" and
      // stretched the cut arrow far outside the viewBox).
      partByNo[number(part.no)] = {
        ...part,
        x: number(part.x), y: number(part.y),
        width: number(part.width), height: number(part.height),
      };
    });
    const cuts = (data.seq || []).map(cut => {
      const pieces = (cut.pieces || []).map(no => partByNo[number(no)]).filter(Boolean);
      const first = pieces[0];
      return {
        type: cut.t, label: SEQ_LABELS[cut.t] || 'برش',
        axis: cut.axis === 'x' ? 'x' : 'y',
        at: number(cut.at), x: sheetW / 2,
        y: cut.t === 'cross' && first ? first.y + first.height / 2 : sheetH / 2,
        pieces,
      };
    });
    return { cuts, sheetW, sheetH };
  }

  function resetSim(panel) {
    const dock = panel.querySelector('[data-sim]');
    const dataNode = panel.querySelector('[data-cut-sim]');
    if (!dock || !dataNode) return;
    if (dock._sim) { dock._sim.teardown(); dock._sim = null; }
    let data;
    try { data = JSON.parse(dataNode.textContent); } catch (_) { return; }
    dock._sim = createSim(panel, dock, data);
    dock._sim.renderList();
    dock._sim.paint(0);
  }

  function createSim(panel, dock, data) {
    const { cuts, sheetW, sheetH } = (Array.isArray(data.seq) && data.seq.length)
      ? storedSequence(data)
      : buildSequence(data);
    const svg = panel.querySelector('svg.cut-map');
    const viewBox = svg ? (svg.viewBox.baseVal || { width: sheetW, height: sheetH }) : { width: sheetW, height: sheetH };
    const list = dock.querySelector('[data-sim-list]');
    const progress = dock.querySelector('[data-sim-progress]');
    const typeChip = dock.querySelector('[data-sim-type]');
    const xOut = dock.querySelector('[data-sim-x]');
    const yOut = dock.querySelector('[data-sim-y]');
    const remnantOut = dock.querySelector('[data-sim-remnant]');
    const playBtn = dock.querySelector('[data-sim-play]');
    const prevBtn = dock.querySelector('[data-sim-prev]');
    const nextBtn = dock.querySelector('[data-sim-next]');
    const resetBtn = dock.querySelector('[data-sim-reset]');
    const speedBtns = [...dock.querySelectorAll('[data-sim-speed]')];

    let ghost = null, line = null, arrow = null, marks = [];
    if (svg) {
      const ns = 'http://www.w3.org/2000/svg';
      ghost = document.createElementNS(ns, 'line');
      ghost.setAttribute('class', 'sim-line-ghost');
      line = document.createElementNS(ns, 'line');
      line.setAttribute('class', 'sim-line');
      arrow = document.createElementNS(ns, 'path');
      arrow.setAttribute('class', 'sim-cut-arrow');
      marks = cuts.map(() => {
        const rect = document.createElementNS(ns, 'rect');
        rect.setAttribute('class', 'sim-piece-hl');
        rect.style.display = 'none';
        return rect;
      });
      const layer = svg.querySelector('[data-sim-layer]') || svg;
      marks.forEach(rect => layer.appendChild(rect));
      layer.appendChild(ghost);
      layer.appendChild(line);
      layer.appendChild(arrow);
    }
    [ghost, line].forEach(node => { if (node) { node.style.display = 'none'; node.setAttribute('x1', 0); node.setAttribute('x2', 0); node.setAttribute('y1', 0); node.setAttribute('y2', 0); } });

    let index = 0;          // 0 = idle, i = after cut i
    let timer = null;
    let speed = 1;
    let playing = false;

    function placeMarker(node, cut, ghostly) {
      if (!node) return;
      node.style.display = '';
      if (cut.axis === 'x') {
        node.setAttribute('x1', cut.at); node.setAttribute('x2', cut.at);
        node.setAttribute('y1', Math.max(0, cut.y - 4000)); node.setAttribute('y2', Math.min(sheetH, cut.y + 4000));
      } else {
        node.setAttribute('x1', Math.max(0, (cut.x ?? sheetW / 2) - 4000)); node.setAttribute('x2', Math.min(sheetW, (cut.x ?? sheetW / 2) + 4000));
        node.setAttribute('y1', cut.at); node.setAttribute('y2', cut.at);
      }
      node.style.opacity = ghostly ? '.35' : '1';
    }

    function highlight(step) {
      marks.forEach((rect, i) => {
        const cut = cuts[i];
        const on = i < step && cut.pieces.length === 1;
        rect.style.display = on ? '' : 'none';
        if (on) {
          const piece = cut.pieces[0];
          rect.setAttribute('x', piece.x); rect.setAttribute('y', piece.y);
          rect.setAttribute('width', piece.width); rect.setAttribute('height', piece.height);
        }
      });
    }

    function paint(step) {
      index = Math.max(0, Math.min(step, cuts.length));
      highlight(index);
      const current = index > 0 ? cuts[index - 1] : null;
      const upcoming = cuts[index];
      if (ghost) ghost.style.display = upcoming ? '' : 'none';
      if (upcoming) placeMarker(ghost, upcoming, true);
      if (line) line.style.display = current ? '' : 'none';
      if (current) placeMarker(line, current, false);
      if (arrow) {
        if (current) {
          arrow.style.display = '';
          const m = 70;
          if (current.axis === 'x') {
            arrow.setAttribute('d', `M${current.at + m},${current.y - m} L${current.at + m},${current.y + m} L${current.at},${current.y} Z`);
          } else {
            const ax = current.x ?? sheetW / 2;
            arrow.setAttribute('d', `M${ax - m},${current.at + m} L${ax + m},${current.at + m} L${ax},${current.at} Z`);
          }
        } else {
          arrow.style.display = 'none';
        }
      }
      const items = [...list.children];
      items.forEach((li, i) => {
        li.classList.toggle('current', i === index - 1);
        li.classList.toggle('done', i < index - 1);
        if (i === index - 1) li.scrollIntoView({ block: 'nearest' });
      });
      if (progress) progress.textContent = cuts.length
        ? `برش ${index} از ${cuts.length}${current && current.pieces[0] ? ` — قطعه: ${current.pieces[0].label || '#' + current.pieces[0].no}` : ''}`
        : 'ترتیبی برای شبیه‌سازی نیست';
      if (typeChip) {
        typeChip.textContent = current ? current.label : (upcoming ? upcoming.label : '—');
        typeChip.className = `sim-cut-type ${current ? current.type : (upcoming ? upcoming.type : 'trim')}`;
      }
      if (xOut) xOut.textContent = current && current.axis === 'x' ? fmt(current.at) : (upcoming && upcoming.axis === 'x' ? fmt(upcoming.at) : '—');
      if (yOut) yOut.textContent = current && current.axis === 'y' ? fmt(current.at) : (upcoming && upcoming.axis === 'y' ? fmt(upcoming.at) : '—');
      if (remnantOut) {
        if (!cuts.length) remnantOut.textContent = '—';
        else {
          const cut = current || upcoming;
          const done = index;
          const ripDone = cuts.slice(0, done).filter(item => item.type === 'rip').length;
          const ripTotal = cuts.filter(item => item.type === 'rip').length;
          remnantOut.textContent = done >= cuts.length
            ? 'پرت پیش‌بینی‌شده'
            : (ripDone < ripTotal ? `${fmt(sheetW)} × ${fmt(Math.max(0, sheetH - (cuts.filter(item => item.type === 'rip')[ripDone]?.at ?? sheetH)))}` : '—');
        }
      }
      if (playBtn) {
        playBtn.innerHTML = playing ? '<i class="bi bi-pause-fill"></i> توقف' : '<i class="bi bi-play-fill"></i> پخش';
        playBtn.classList.toggle('primary', !playing);
        playBtn.disabled = cuts.length === 0;
      }
      if (prevBtn) prevBtn.disabled = index === 0 || cuts.length === 0;
      if (nextBtn) nextBtn.disabled = index >= cuts.length || cuts.length === 0;
    }

    function renderList() {
      if (!list) return;
      list.replaceChildren();
      if (!cuts.length) {
        const li = document.createElement('li');
        li.textContent = 'برش قابل شبیه‌سازی نیست.';
        list.appendChild(li);
        return;
      }
      cuts.forEach((cut, i) => {
        const li = document.createElement('li');
        const pieces = cut.pieces.length ? cut.pieces.map(piece => `#${piece.no}`).join('، ') : '—';
        const value = cut.axis === 'x' ? `X=${fmt(cut.at)}` : `Y=${fmt(cut.at)}`;
        li.innerHTML = `<span class="sim-no num">${String(i + 1).padStart(2, '0')}</span><span class="sim-cut-type ${cut.type}">${cut.label}</span><span class="num">${value}</span><span class="sim-piece">${pieces}</span>`;
        list.appendChild(li);
      });
    }

    function stop() { if (timer) { clearInterval(timer); timer = null; } playing = false; paint(index); }
    function tick() {
      if (index >= cuts.length) { stop(); return; }
      paint(index + 1);
      if (index >= cuts.length) stop();
    }
    function play() {
      if (cuts.length === 0) return;
      if (playing) { stop(); return; }
      if (index >= cuts.length) paint(0);
      playing = true;
      paint(index);
      timer = setInterval(tick, Math.max(180, 900 / speed));
    }

    playBtn?.addEventListener('click', play);
    prevBtn?.addEventListener('click', () => { stop(); paint(index - 1); });
    nextBtn?.addEventListener('click', () => { stop(); paint(index + 1); });
    resetBtn?.addEventListener('click', () => { stop(); paint(0); });
    speedBtns.forEach(btn => btn.addEventListener('click', () => {
      speed = number(btn.dataset.simSpeed) || 1;
      speedBtns.forEach(item => item.classList.toggle('on', item === btn));
      if (playing) { clearInterval(timer); timer = setInterval(tick, Math.max(180, 900 / speed)); }
    }));

    return {
      renderList, paint,
      teardown() { stop(); marks.forEach(rect => rect.remove()); if (ghost) ghost.remove(); if (line) line.remove(); if (arrow) arrow.remove(); },
    };
  }

  /* re-init when the visible panel changes or files load */
  const observer = new MutationObserver(() => {
    if (root.dataset.mode !== 'sim') return;
    const panel = panels.find(item => !item.classList.contains('d-none'));
    if (!panel) return;
    const dock = panel.querySelector('[data-sim]');
    if (dock && !dock._sim) resetSim(panel);
  });
  observer.observe(root, { subtree: true, attributes: true, attributeFilter: ['class'] });

  panels.forEach(panel => { const dock = panel.querySelector('[data-sim]'); if (dock) resetSim(panel); });
  setMode(root.dataset.mode || 'layout');
})();
