/* Jarvis dev agent beyin ağı — SALT OKUNUR pano.
 *
 * Veri: GET api/state (+ sayfa adresindeki ?t=token aynen iletilir), şema:
 *   {schema:1, runs:[{task_id,project,description,status,iteration,max_iterations,
 *    files:[{name,size}],rejects:{reject,eval,lock,method,ruff},model:{provider,name},
 *    events:[str],reason}], jobs:[{id,description,status,started_at,finished_at,summary:[str]}],
 *    errors:[str]}
 * ?demo=1 → gömülü örnek veri (sunucu olmadan tasarımı görmek için).
 *
 * Güvenlik: sunucudan gelen HİÇBİR metin innerHTML'e girmez; yalnızca textContent.
 * Dış kaynak, CDN, satır içi script/stil yok (CSP: default-src 'self' ile uyumlu).
 * Düğme, yazı kutusu, onay/iptal/komut YOK: pano yalnızca okur.
 */
(function () {
  'use strict';
  var W = 1280, H = 760, POLL_MS = 2000;
  var C = { dev: '#38e8ff', gate: '#ffc857', agent: '#7dffb0', model: '#c792ff', tool: '#5aa2ff', file: '#ff7ac6', user: '#ffffff' };
  var KIND = { dev: 'MERKEZ', gate: 'GÜVENLİK', agent: 'AJAN', model: 'MODEL', tool: 'ARAÇ', file: 'DOSYA', user: 'KULLANICI' };
  var DEMO = new URLSearchParams(location.search).get('demo') === '1';
  var stage = document.getElementById('stage');
  var picked = 'dev', lastSig = '', state = null, online = false;

  var DEMO_STATE = {
    schema: 1, errors: [],
    jobs: [{ id: 'A1', description: 'Hesap makinesi', status: 'running', started_at: '', finished_at: '', summary: [] }],
    runs: [{
      task_id: 'demo0001', project: 'tkinter_windows_benzeri', description: 'Tkinter ile Windows benzeri hesap makinesi',
      status: 'çalışıyor', iteration: 6, max_iterations: 25,
      files: [{ name: 'calculator.py', size: 1516 }, { name: 'main.py', size: 2210 }, { name: 'README.md', size: 938 }],
      rejects: { reject: 2, eval: 1, lock: 0, method: 0, ruff: 0 },
      model: { provider: 'ollama', name: 'qwen2.5-coder:14b' },
      events: ['#1 write: calculator.py (HATA)', '#2 write: REDDEDİLDİ (eval) (HATA)', '#3 write: calculator.py (OK)', '#4 write: README.md (OK)', '#5 write: main.py (OK)'],
      reason: ''
    }]
  };

  /* ── yardımcılar ─────────────────────────────────────────── */
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function at(e, x, y, w) {
    e.style.left = x + 'px'; e.style.top = y + 'px';
    if (w) e.style.width = w + 'px';
    return e;
  }
  function num(v) { return typeof v === 'number' && isFinite(v) ? v : 0; }
  function fit() {
    var s = Math.min(window.innerWidth / W, window.innerHeight / H);
    stage.style.transform = 'scale(' + s + ')';
  }

  /* ── durumdan türetme ────────────────────────────────────── */
  function currentRun(st) {
    var runs = (st && st.runs) || [];
    for (var i = runs.length - 1; i >= 0; i--) if (runs[i].status === 'çalışıyor') return runs[i];
    return runs.length ? runs[runs.length - 1] : null;
  }
  function sum(r) {
    var x = (r && r.rejects) || {};
    return num(x.reject) + num(x.method) + num(x.ruff);
  }

  function build(st) {
    var run = currentRun(st);
    var busy = !!run && run.status === 'çalışıyor';
    var prov = run && run.model ? run.model.provider : '';
    var rej = (run && run.rejects) || {};
    var files = (run && run.files) || [];
    var nodes = [
      { id: 'user', t: 'user', label: 'Kullanıcı', x: 640, y: 100, r: 14, text: 'Sesli veya yazılı istek buradan gelir.', status: 'bekliyor' },
      { id: 'gate', t: 'gate', label: 'Güvenlik kapısı', x: 640, y: 240, r: 30, text: 'Riskli işlemler onay olmadan geçmez. Pano onay veremez; onay yalnızca Jarvis penceresinden verilir.', status: 'aktif' },
      { id: 'dev', t: 'dev', label: 'DEV AGENT', x: 640, y: 390, r: 46,
        text: run ? ('Görev: ' + (run.description || '—')) : 'Şu an çalışan kodlama görevi yok.',
        status: run ? (run.status + ' · tur ' + num(run.iteration) + '/' + num(run.max_iterations)) : 'boşta', idle: !run },
      { id: 'plan', t: 'agent', label: 'Planlayıcı', x: 440, y: 310, r: 20, text: 'İstekten dosya listesini çıkarır.', status: run ? (files.length + ' dosya') : 'boşta', idle: !run },
      { id: 'res', t: 'agent', label: 'Araştırma', x: 420, y: 470, r: 18, text: 'Gerekirse bilgi toplar.', status: 'boşta', idle: true },
      { id: 'code', t: 'agent', label: 'Kodlayıcı', x: 840, y: 310, r: 22, text: 'Dosyaları tek tek yazar.', status: run ? run.status : 'boşta', idle: !busy },
      { id: 'audit', t: 'agent', label: 'Denetçi', x: 860, y: 480, r: 20,
        text: 'Ruff, import denemesi, eksik metot kapısı, pytest. Sahte kodu reddeder.',
        status: run ? (num(rej.reject) + ' ret · ' + num(rej.eval) + ' eval · ' + num(rej.method) + ' metot') : 'boşta', idle: !run },
      { id: 'ruff', t: 'tool', label: 'ruff', x: 760, y: 600, r: 14, text: 'Yalnızca bloklayan kurallar.', status: run ? (num(rej.ruff) + ' dosyada hata') : 'hazır', idle: !run },
      { id: 'gem', t: 'model', label: 'Gemini', x: 1040, y: 220, r: 20, text: 'Ücretsiz katman; kota veya 5xx hatasında Ollama’ya düşülür.', status: prov === 'gemini' ? 'aktif' : 'pasif', idle: prov !== 'gemini' },
      { id: 'oll', t: 'model', label: 'Ollama', x: 1090, y: 360, r: 24, text: 'Yerel model' + (run && run.model && prov === 'ollama' ? ': ' + run.model.name : '.'), status: prov === 'ollama' ? 'aktif' : 'pasif', idle: prov !== 'ollama' }
    ];
    var slots = [[235, 350], [255, 440], [215, 520], [300, 590], [385, 650]];
    var links = [['user', 'gate'], ['gate', 'dev'], ['dev', 'plan'], ['dev', 'res'], ['dev', 'code'], ['dev', 'audit'],
      ['code', 'gem'], ['code', 'oll'], ['audit', 'ruff'], ['code', 'audit']];
    files.slice(0, slots.length).forEach(function (f, i) {
      var id = 'f' + i;
      nodes.push({ id: id, t: 'file', label: f.name, x: slots[i][0], y: slots[i][1], r: 12,
        text: 'Boyut: ' + num(f.size) + ' bayt. İçerik panoda gösterilmez.', status: 'yazıldı' });
      links.push(['plan', id]);
    });
    return { nodes: nodes, links: links, run: run };
  }

  /* ── çizim ───────────────────────────────────────────────── */
  function render() {
    var g = build(state), by = {};
    g.nodes.forEach(function (n) { by[n.id] = n; });
    if (!by[picked]) picked = 'dev';
    stage.textContent = '';

    var head = at(el('div', 'abs'), 32, 28);
    head.appendChild(el('div', 'eyebrow', 'JARVIS // DEV AGENT'));
    head.appendChild(el('div', 'title', 'BEYİN AĞI'));
    stage.appendChild(head);

    var run = g.run, files = (run && run.files) || [];
    var stats = el('div', 'abs stats'); stats.style.right = '32px'; stats.style.top = '28px';
    [['TUR', run ? num(run.iteration) + '/' + num(run.max_iterations) : '—', '#fff'],
     ['DOSYA', run ? files.length : '—', '#7dffb0'],
     ['RET', run ? sum(run) : '—', '#ffc857'],
     ['MODEL', run && run.model && run.model.provider !== 'bilinmiyor' ? run.model.provider : '—', '#c792ff']
    ].forEach(function (s) {
      var b = el('div'); b.appendChild(el('div', 'stat-l', s[0]));
      var v = el('div', 'stat-v', s[1]); v.style.color = s[2]; b.appendChild(v); stats.appendChild(b);
    });
    stage.appendChild(stats);

    if (!online && !DEMO) stage.appendChild(el('div', 'banner', 'BAĞLANTI YOK — Jarvis kapalı ya da pano adresi/token hatalı'));

    g.links.forEach(function (l) {
      var a = by[l[0]], b = by[l[1]];
      var dx = b.x - a.x, dy = b.y - a.y, hot = picked === a.id || picked === b.id;
      var e = el('div', 'edge' + (hot ? ' hot' : ''));
      at(e, a.x, a.y, Math.sqrt(dx * dx + dy * dy));
      e.style.transform = 'rotate(' + (Math.atan2(dy, dx) * 180 / Math.PI) + 'deg)';
      if (hot) e.style.background = C[b.t];
      stage.appendChild(e);
    });

    g.nodes.forEach(function (n) {
      var d = n.r * 2, sel = picked === n.id;
      var b = el('button', 'node' + (sel ? ' sel' : '') + (n.idle ? ' idle' : ''));
      b.type = 'button'; b.setAttribute('aria-label', n.label);
      at(b, n.x - n.r, n.y - n.r); b.style.width = d + 'px'; b.style.height = d + 'px';
      b.style.background = C[n.t]; b.style.boxShadow = '0 0 ' + (sel ? 34 : 20) + 'px ' + C[n.t];
      var lb = el('span', 'node-label', n.label); lb.style.top = (d + 8) + 'px'; b.appendChild(lb);
      b.addEventListener('click', function () { picked = n.id; lastSig = ''; render(); });
      stage.appendChild(b);
    });

    /* son görev kartı */
    var card = at(el('div', 'card'), 32, 96, 176);
    card.appendChild(el('div', 'card-h', 'SON GÖREV'));
    card.appendChild(el('div', 'card-t', run ? (run.project || run.description || '—') : 'görev yok'));
    var bar = el('div', 'bar'), fill = el('i');
    var pct = run && num(run.max_iterations) ? Math.min(100, Math.round(100 * num(run.iteration) / num(run.max_iterations))) : 0;
    fill.style.width = pct + '%'; bar.appendChild(fill); card.appendChild(bar);
    card.appendChild(el('div', 'card-s', run ? (run.status + (run.reason ? ' · ' + run.reason : '')) : 'boşta'));
    stage.appendChild(card);

    /* olaylar */
    var evs = at(el('div', 'card'), 32, 214, 176); evs.style.height = '104px';
    evs.appendChild(el('div', 'card-h', 'OLAYLAR'));
    ((run && run.events) || []).slice(-4).reverse().forEach(function (t) {
      var e = el('div', 'ev', t); e.style.color = /HATA|REDDED/.test(t) ? '#ffc857' : '#9fb3d1'; evs.appendChild(e);
    });
    stage.appendChild(evs);

    /* gösterge */
    var lg = el('div', 'legend');
    [['Dev agent', C.dev], ['Güvenlik kapısı', C.gate], ['Ajanlar', C.agent], ['Modeller', C.model], ['Araçlar', C.tool], ['Dosyalar', C.file]].forEach(function (i) {
      var row = el('div'), dot = el('span', 'dot'); dot.style.background = i[1]; dot.style.boxShadow = '0 0 8px ' + i[1];
      row.appendChild(dot); row.appendChild(document.createTextNode(i[0])); lg.appendChild(row);
    });
    stage.appendChild(lg);

    /* detay */
    var s = by[picked], det = el('div', 'card detail');
    det.appendChild(el('div', 'd-kind', KIND[s.t]));
    det.appendChild(el('div', 'd-name', s.label));
    det.appendChild(el('div', 'd-text', s.text));
    var st = el('div', 'd-text', '● ' + s.status); st.style.color = C[s.t]; det.appendChild(st);
    stage.appendChild(det);

    stage.appendChild(el('div', 'foot', (DEMO ? 'ÖRNEK VERİ' : 'CANLI') + ' · SALT OKUNUR · DÜĞÜMLERE TIKLA'));
  }

  function apply(st, ok) {
    var sig = JSON.stringify([st, ok, picked]);
    state = st; online = ok;
    if (sig === lastSig) return;       // değişmediyse yeniden çizme (odak kaybolmasın)
    lastSig = sig; render();
  }

  function poll() {
    if (DEMO) { apply(DEMO_STATE, true); return; }
    fetch('api/state' + location.search, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('http'); return r.json(); })
      .then(function (j) { apply(j && typeof j === 'object' ? j : { runs: [], jobs: [] }, true); })
      .catch(function () { apply(state || { runs: [], jobs: [] }, false); });
  }

  window.addEventListener('resize', fit);
  fit(); poll(); setInterval(poll, POLL_MS);
})();
