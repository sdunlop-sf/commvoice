(() => {
  'use strict';

  // ------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);

  // Build DOM safely. Text is always added as text, never as HTML.
  function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === false || v == null) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(kid));
    }
    return el;
  }

  const newId = () =>
    window.crypto && crypto.randomUUID ? crypto.randomUUID() : 'id-' + Math.random().toString(36).slice(2) + Date.now().toString(36);


  const state = {
    session: newId(),
    doc: null, // { name, count, label, links, ... } once a document is loaded
    history: [],
    busy: false,
    voiceOn: true,
    ideas: { list: [], round: 0, closed: false },
    asked: false,
    aiConnected: true,
    hint: '',
    maxQuestion: 4000,
    maxMb: 15,
  };

  const CAPTIONS = {
    idle: "Hi, I'm Vox. Add a document and ask me about it.",
    ready: 'I have your document. Ask me anything about it.',
    listening: "I'm listening...",
    thinking: 'Checking the document...',
    speaking: "Here's what I found.",
    refuse: "That isn't in your document.",
  };

  async function api(path, options) {
    let res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      return { ok: false, status: 0, data: { detail: 'I could not reach the server. Check your connection and try again.' } };
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok && typeof data.detail !== 'string') data.detail = 'Something went wrong. Please check what you sent and try again.';
    return { ok: res.ok, status: res.status, data };
  }
  const postJson = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  // ---------------------------------------------------------- preferences
  function loadPrefs() {
    try {
      const size = localStorage.getItem('cv-size');
      if (size) document.documentElement.dataset.size = size;
      if (localStorage.getItem('cv-contrast') === 'high') document.documentElement.dataset.contrast = 'high';
    } catch (e) { /* storage may be blocked; that is fine */ }
    syncPrefButtons();
  }
  function savePref(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
  }
  function syncPrefButtons() {
    const size = document.documentElement.dataset.size;
    document.querySelectorAll('.sizes button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.size === size)));
    $('#contrast').setAttribute('aria-pressed', String(document.documentElement.dataset.contrast === 'high'));
  }
  function setupPrefs() {
    document.querySelectorAll('.sizes button').forEach((b) =>
      b.addEventListener('click', () => {
        document.documentElement.dataset.size = b.dataset.size;
        savePref('cv-size', b.dataset.size);
        syncPrefButtons();
      })
    );
    $('#contrast').addEventListener('click', () => {
      const on = document.documentElement.dataset.contrast !== 'high';
      document.documentElement.dataset.contrast = on ? 'high' : 'normal';
      savePref('cv-contrast', on ? 'high' : 'normal');
      syncPrefButtons();
    });
  }

  // --------------------------------------------------------------- mascot
  let idleTimer = null;
  function idleCaption() {
    return state.doc ? CAPTIONS.ready : CAPTIONS.idle;
  }
  function setMascot(mode, caption) {
    clearTimeout(idleTimer);
    $('#mascot').dataset.state = mode;
    $('#caption').textContent = caption || (mode === 'idle' ? idleCaption() : CAPTIONS[mode]) || '';
  }
  function idleSoon(ms = 2800) {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!listening && !state.busy) setMascot('idle');
    }, ms);
  }

  // ---------------------------------------------------------------- voice
  const synth = 'speechSynthesis' in window ? window.speechSynthesis : null;
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let listening = false;
  let rec = null;

  function pickVoice() {
    if (!synth) return null;
    const voices = synth.getVoices();
    return (
      voices.find((v) => v.lang === 'en-AU') ||
      voices.find((v) => v.lang === 'en-GB') ||
      voices.find((v) => v.lang && v.lang.startsWith('en')) ||
      null
    );
  }
  function stopSpeaking() {
    if (synth) synth.cancel();
  }
  function speak(text, { force = false, mode = 'speaking' } = {}) {
    if (!synth || !(state.voiceOn || force)) {
      setMascot(mode);
      idleSoon();
      return;
    }
    synth.cancel();
    const parts = (text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [text]).map((p) => p.trim()).filter(Boolean);
    setMascot(mode);
    parts.forEach((p, i) => {
      const u = new SpeechSynthesisUtterance(p);
      u.lang = 'en-AU';
      u.rate = 0.95;
      const v = pickVoice();
      if (v) u.voice = v;
      if (i === parts.length - 1) {
        u.onend = () => { if (!listening) setMascot('idle'); };
        u.onerror = u.onend;
      }
      synth.speak(u);
    });
  }

  function startListening() {
    if (!Rec || listening || state.busy) return;
    stopSpeaking();
    try { rec = new Rec(); } catch (e) { return; }
    rec.lang = 'en-AU';
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e) => {
      let t = '';
      for (const r of e.results) t += r[0].transcript;
      $('#q').value = t;
    };
    rec.onerror = (e) => {
      listening = false;
      $('#mic').classList.remove('live');
      setMascot('idle');
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        addProblem('The microphone is blocked. Allow it in your browser settings, or type your question.');
      }
    };
    rec.onend = () => {
      const was = listening;
      listening = false;
      $('#mic').classList.remove('live');
      const text = $('#q').value.trim();
      if (was && text) ask(text);
      else setMascot('idle');
    };
    try {
      rec.start();
      listening = true;
      $('#mic').classList.add('live');
      setMascot('listening');
    } catch (e) {
      listening = false;
    }
  }
  function stopListening() {
    if (rec && listening) {
      try { rec.stop(); } catch (e) { /* already stopped */ }
    }
  }

  function setupVoice() {
    const mic = $('#mic');
    if (!Rec) {
      mic.hidden = true;
      $('#micNote').hidden = false;
    } else {
      mic.addEventListener('pointerdown', (e) => { e.preventDefault(); startListening(); });
      ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => mic.addEventListener(ev, stopListening));
      mic.addEventListener('contextmenu', (e) => e.preventDefault());
      mic.addEventListener('keydown', (e) => {
        if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); startListening(); }
      });
      mic.addEventListener('keyup', (e) => {
        if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); stopListening(); }
      });
    }
    if (!synth) {
      $('#voiceOn').closest('label').hidden = true;
      $('#stopVoice').hidden = true;
    }
    $('#voiceOn').addEventListener('change', (e) => {
      state.voiceOn = e.target.checked;
      if (!state.voiceOn) stopSpeaking();
    });
    $('#stopVoice').addEventListener('click', () => { stopSpeaking(); setMascot('idle'); });
    if (synth) synth.getVoices();
  }

  // -------------------------------------------------- sources (the "slip")
  // A slip shows the exact words and where they are, so anyone can check.
  function slip(src, summary) {
    const linked = src.kind === 'linked';
    let where;
    let link = null;
    if (linked) {
      where = `Section ${src.page} of "${src.name}"`;
      if (src.url && src.url.startsWith('https://')) {
        link = h('a', { href: src.url, target: '_blank', rel: 'noopener noreferrer', text: 'Open the original link' });
      }
    } else if (src.label === 'part') {
      where = `Part ${src.page} of your document`;
    } else {
      where = `Page ${src.page} of your document` + (src.printed && String(src.printed) !== String(src.page) ? `, printed page ${src.printed}` : '');
    }
    const note = linked ? (src.official ? ' This is a government website.' : ' This site is not a government site and nobody has checked it.') : '';
    return h(
      'details',
      { class: `slip${linked ? ' linked' : ''}` },
      h('summary', { text: summary || `See the exact words (${where})` }),
      h('blockquote', { text: src.quote }),
      h('p', { class: 'pg' }, where + '.' + note + ' ', link)
    );
  }
  const docSlip = (item) => slip({ kind: 'main', label: state.doc ? state.doc.label : 'page', page: item.page, printed: item.printed, quote: item.quote });

  const listenBtn = (text) =>
    h('button', { type: 'button', class: 'link', 'aria-label': 'Listen to this', onclick: () => speak(text, { force: true }) }, 'Listen');

  // ----------------------------------------------------------------- chat
  const chat = () => $('#chat');

  function scrollToLatest(el) {
    el.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  }
  function addUser(text) {
    const el = h('div', { class: 'msg user', text });
    chat().append(el);
    scrollToLatest(el);
  }
  function addBot(node, cls = '') {
    const el = h('div', { class: `msg bot ${cls}`.trim() }, node);
    chat().append(el);
    scrollToLatest(el);
    return el;
  }
  function addProblem(message) {
    return addBot(h('p', { class: 'answer', text: message }), 'problem');
  }

  function chipRow(title, questions) {
    if (!questions || !questions.length) return null;
    return h(
      'div',
      { class: 'followups' },
      h('p', { text: title }),
      h('div', { class: 'chips' }, ...questions.map((q) => h('button', { type: 'button', class: 'chip', text: q, onclick: () => ask(q) })))
    );
  }

  function feedbackRow(origin) {
    const row = h('div', { class: 'feedback' });
    const send = (rating, reason) => {
      postJson('/api/feedback', { rating, reason: reason || null, origin });
      row.replaceChildren('Thank you. That helps us improve.');
    };
    const reasons = () =>
      row.replaceChildren(
        'What went wrong? ',
        h('button', { type: 'button', class: 'link', text: 'Too hard to understand', onclick: () => send('unclear', 'too_hard') }),
        h('button', { type: 'button', class: 'link', text: 'Not what I asked', onclick: () => send('unclear', 'not_asked') }),
        h('button', { type: 'button', class: 'link', text: 'Something looks wrong', onclick: () => send('unclear', 'looks_wrong') })
      );
    row.append('Was this clear? ', h('button', { type: 'button', class: 'link', text: 'Yes', onclick: () => send('clear') }), h('button', { type: 'button', class: 'link', text: 'No', onclick: reasons }));
    return row;
  }

  function renderAnswer(question, d) {
    const refusal = !d.covered;
    const box = h('div');
    if (!refusal) {
      if (d.origin === 'general') {
        box.append(h('span', { class: 'origin warn', text: 'General meaning, not from your document' }));
      } else if (d.origin === 'linked') {
        const official = d.source && d.source.official;
        box.append(h('span', { class: 'origin warn', text: official ? 'From a linked page (government site)' : 'From a linked page (not checked)' }));
      } else {
        box.append(h('span', { class: 'origin', text: 'From your document' }));
      }
    }
    box.append(h('p', { class: 'answer', text: d.answer }));
    if (d.origin === 'general') {
      box.append(h('p', { class: 'general-note', text: 'Your document does not explain this word. This is general information. A lawyer can tell you how it applies to you.' }));
    }
    if (d.source) box.append(slip(d.source));
    const tools = h('div', { class: 'tools-row' }, h('button', { type: 'button', class: 'link', text: 'Listen again', onclick: () => speak(d.answer, { force: true, mode: refusal ? 'refuse' : 'speaking' }) }));
    if (!refusal) {
      tools.append(h('button', { type: 'button', class: 'link', text: 'Explain simpler', onclick: () => ask(`Explain this in shorter, simpler words: ${question}`) }));
    }
    box.append(tools);
    const follow = refusal ? chipRow('You could try', state.ideas.list.slice(0, 3)) : chipRow('You could also ask', d.follow_ups);
    if (follow) box.append(follow);
    box.append(feedbackRow(d.origin || 'none'));
    addBot(box, refusal ? 'refusal' : '');
    state.history.push({ q: question, a: d.answer });
    speak(d.answer, { mode: refusal ? 'refuse' : 'speaking' });
  }

  function setBusy(on, label) {
    state.busy = on;
    $('#send').disabled = on;
    $('#send').textContent = on ? (label || 'Checking...') : 'Ask';
    $('#plus').disabled = on;
  }

  function autosize() {
    const q = $('#q');
    q.style.height = 'auto';
    q.style.height = Math.min(q.scrollHeight, 160) + 'px';
  }
  function clearInput() {
    $('#q').value = '';
    autosize();
  }
  function showOfflineBanner() {
    $('#offline').hidden = !!state.aiConnected;
    if (state.hint) $('#offlineHint').textContent = state.hint;
  }

  async function ask(question) {
    question = (question || '').trim();
    if (!question || state.busy) return;
    if (!state.doc) {
      addBot(h('p', { class: 'answer', text: 'Add a document first. Tap the + button to upload a PDF, Word or text file, or try the sample.' }), 'problem');
      return;
    }
    if (question.length > state.maxQuestion) {
      addProblem(`Please keep your question under ${state.maxQuestion} characters.`);
      return;
    }
    hideIdeas();
    state.asked = true;
    stopSpeaking();
    addUser(question);
    clearInput();
    setBusy(true);
    setMascot('thinking');
    const r = await postJson('/api/ask', { question, session_id: state.session, history: state.history.slice(-6) });
    setBusy(false);
    if (!r.ok) {
      setMascot('idle');
      addProblem(r.data.detail);
      return;
    }
    renderAnswer(question, r.data);
  }

  // ---------------------------------------------------------------- ideas
  function ideaSet() {
    const list = state.ideas.list;
    if (!list.length) return [];
    const n = Math.min(4, list.length);
    return Array.from({ length: n }, (_, i) => list[(state.ideas.round * n + i) % list.length]);
  }
  function renderIdeas() {
    $('#ideaChips').replaceChildren(...ideaSet().map((q) => h('button', { type: 'button', class: 'chip', text: q, onclick: () => ask(q) })));
  }
  function showIdeas() {
    if (!state.ideas.list.length) return;
    renderIdeas();
    $('#ideas').hidden = false;
  }
  function hideIdeas() {
    $('#ideas').hidden = true;
  }
  function setupIdeas() {
    $('#moreIdeas').addEventListener('click', () => { state.ideas.round += 1; renderIdeas(); });
    $('#closeIdeas').addEventListener('click', () => { state.ideas.closed = true; hideIdeas(); });
  }

  // -------------------------------------------------------- the + menu
  function setMenu(open) {
    $('#menu').hidden = !open;
    $('#plus').setAttribute('aria-expanded', String(open));
    if (open) $('#menuUpload').focus();
  }
  function setupMenu() {
    $('#plus').addEventListener('click', () => setMenu($('#menu').hidden));
    document.addEventListener('click', (e) => {
      if (!$('#menu').hidden && !e.target.closest('#menu') && !e.target.closest('#plus')) setMenu(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('#menu').hidden) { setMenu(false); $('#plus').focus(); }
    });
    const pick = () => { setMenu(false); $('#file').click(); };
    $('#menuUpload').addEventListener('click', pick);
    $('#welcomeUpload').addEventListener('click', pick);
    $('#menuLink').addEventListener('click', () => {
      setMenu(false);
      if (!state.doc) {
        addProblem('Add a document first, then you can add web links to go with it.');
        return;
      }
      $('#linkForm').hidden = false;
      $('#linkUrl').focus();
    });
    $('#cancelLink').addEventListener('click', () => { $('#linkForm').hidden = true; });
    $('#linkForm').addEventListener('submit', (e) => { e.preventDefault(); addWebLink($('#linkUrl').value.trim()); });
    $('#file').addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      if (f) uploadFile(f);
    });
    $('#trySample').addEventListener('click', trySample);
    $('#newDoc').addEventListener('click', resetDocument);

    // drag and drop anywhere on the page
    let depth = 0;
    window.addEventListener('dragenter', (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { depth += 1; document.body.classList.add('dragging'); } });
    window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove('dragging'); });
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      depth = 0;
      document.body.classList.remove('dragging');
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadFile(f);
    });
  }

  // ------------------------------------------------- loading a document
  function working(text) {
    const el = addBot(h('p', { class: 'answer', text }), 'working');
    return { el, set: (t) => { el.querySelector('.answer').textContent = t; }, done: () => el.remove() };
  }

  async function withProgress(firstText, run) {
    stopSpeaking();
    hideIdeas();
    setBusy(true, 'Reading...');
    setMascot('thinking', firstText);
    const w = working(firstText);
    const t1 = setTimeout(() => w.set('Writing a plain-language overview. This can take up to a minute for long documents...'), 6000);
    const r = await run();
    clearTimeout(t1);
    w.done();
    setBusy(false);
    return r;
  }

  async function uploadFile(file) {
    if (state.busy) return;
    if (file.size > state.maxMb * 1024 * 1024) {
      addProblem(`That file is too big. The limit is ${state.maxMb} MB.`);
      return;
    }
    const r = await withProgress(`Reading ${file.name}...`, () =>
      api(`/api/upload?session_id=${encodeURIComponent(state.session)}&filename=${encodeURIComponent(file.name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
      })
    );
    onDocument(r);
  }

  async function trySample() {
    if (state.busy) return;
    const r = await withProgress('Reading the sample document...', () => postJson('/api/sample', { session_id: state.session }));
    onDocument(r);
  }

  function onDocument(r) {
    if (!r.ok) {
      setMascot('refuse', 'I could not read that file.');
      idleSoon(4000);
      addProblem(r.data.detail);
      return;
    }
    const d = r.data;
    chat().replaceChildren();
    state.doc = d;
    state.history = [];
    state.asked = false;
    state.ideas = { list: (d.overview && d.overview.questions ? d.overview.questions.map((q) => q.question) : []), round: 0, closed: false };
    $('#welcome').hidden = true;
    $('#stage').classList.add('compact');
    $('#docbar').hidden = false;
    $('#docName').textContent = d.name;
    $('#docMeta').textContent = `${d.count} ${d.label === 'page' ? (d.count === 1 ? 'page' : 'pages') : (d.count === 1 ? 'part' : 'parts')}` + (d.links.length ? `, ${d.links.length} ${d.links.length === 1 ? 'link' : 'links'} found` : '');
    $('#q').placeholder = 'Ask a question';
    showOfflineBanner();
    renderOverview(d);
    if (d.overview) {
      const first = d.overview.sections.filter((x) => x.status === 'ok').slice(0, 3).map((x) => x.text).join(' ');
      speak(`I have read your document. ${d.overview.title}. ${first}`);
      showIdeas();
    } else {
      setMascot('idle');
    }
  }

  function resetDocument() {
    stopSpeaking();
    api(`/api/workspace?session_id=${encodeURIComponent(state.session)}`, { method: 'DELETE' });
    state.session = newId();
    state.doc = null;
    state.history = [];
    chat().replaceChildren();
    hideIdeas();
    $('#welcome').hidden = false;
    $('#stage').classList.remove('compact');
    $('#docbar').hidden = true;
    $('#linkForm').hidden = true;
    $('#q').placeholder = 'Add a document with +';
    setMascot('idle');
    window.scrollTo({ top: 0 });
  }

  // --------------------------------------------------------- the overview
  function renderOverview(d) {
    const o = d.overview;
    const box = h('div', { class: 'overview' });

    if (!o) {
      box.append(
        h('h3', { text: `I read ${d.name}` }),
        h('p', { class: 'answer', text: d.ai_error || 'I could not prepare an overview.' }),
        h('p', { class: 'hint', text: 'The file itself was read fine. Once the AI is switched on you can ask questions about it.' })
      );
    } else {
      box.append(
        h('h3', { text: o.title }),
        h('p', { class: 'hint', text: 'Here is what this document means in everyday words. Open "See the exact words" under any part to check it. Where the document says nothing, I say so.' })
      );
      const written = o.sections.filter((x) => x.status === 'ok');
      box.append(
        ...o.sections.map((x) =>
          h(
            'section',
            { class: `ov-section${x.status === 'ok' ? '' : ' missing'}` },
            h('h4', { text: x.title }),
            x.status === 'ok'
              ? [h('p', { text: x.text }), docSlip(x)]
              : h('p', { class: 'muted', text: x.status === 'unverified' ? "I couldn't confirm this from the document, so I'm not going to guess." : "The document doesn't say." })
          )
        )
      );
      if (written.length) {
        box.append(h('div', { class: 'tools-row' }, listenBtn(`${o.title}. ${written.map((x) => `${x.title} ${x.text}`).join(' ')}`)));
      } else {
        box.append(h('p', { class: 'answer', text: 'I could not write a reliable overview of this one. You can still ask me questions about it.' }));
      }
      if (o.key_terms.length) {
        box.append(
          h(
            'details',
            { class: 'acc' },
            h('summary', { text: `Hard words explained (${o.key_terms.length})` }),
            h('div', { class: 'body' }, ...o.key_terms.map((t) => h('div', { class: 'term' }, h('p', {}, h('strong', { text: t.term + ': ' }), t.meaning), docSlip(t))))
          )
        );
      }
      if (o.references.length) {
        box.append(
          h(
            'details',
            { class: 'acc' },
            h('summary', { text: `Other laws and documents it mentions (${o.references.length})` }),
            h(
              'div',
              { class: 'body' },
              h('p', { class: 'hint', text: 'I can only read these if the document links to them, or if you add a link with the + button.' }),
              ...o.references.map((r) => h('div', {}, h('p', { text: r.name }), docSlip(r)))
            )
          )
        );
      }
    }

    if (d.truncated) {
      box.append(h('p', { class: 'note', text: 'This document is very long, so I only read the first part of it.' }));
    }

    if (d.links.length) box.append(linksBox(d.links));
    addBot(box, 'wide');
  }

  function linksBox(found) {
    const status = h('p', { class: 'hint', 'aria-live': 'polite' });
    const results = h('ul', { class: 'linklist' });
    const btn = h('button', { type: 'button', class: 'btn ghost', text: `Read the linked pages (up to 10)` });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      status.textContent = 'Reading the links. This can take up to a minute...';
      setMascot('thinking', 'Reading the links...');
      const r = await postJson('/api/follow_links', { session_id: state.session });
      setMascot('idle');
      if (!r.ok) {
        status.textContent = r.data.detail;
        btn.disabled = false;
        return;
      }
      results.replaceChildren(
        ...r.data.results.map((x) =>
          h(
            'li',
            {},
            x.ok ? (x.title || x.url) : x.url,
            x.ok ? h('span', { class: `tag${x.official ? ' official' : ''}`, text: x.official ? 'government site' : 'not a government site' }) : h('span', { class: 'tag fail', text: 'not read' }),
            x.ok ? null : h('div', { class: 'hint', text: x.reason })
          )
        )
      );
      status.textContent = r.data.read
        ? `I read ${r.data.read} of ${r.data.results.length} links, one level deep. If your document does not answer a question, I will check these pages next and tell you where the answer came from.`
        : 'I could not read any of the links. Some sites block readers or need a login.';
      btn.textContent = 'Read them again';
      btn.disabled = false;
    });
    return h(
      'div',
      { class: 'linkbox' },
      h('h4', { text: `This document has ${found.length} ${found.length === 1 ? 'link' : 'links'}` }),
      h('p', { class: 'hint', text: 'Legal documents often point to other laws and pages. I can read them (one level deep, up to 10) to give a fuller picture.' }),
      btn,
      status,
      results,
      h(
        'details',
        { class: 'acc' },
        h('summary', { text: 'See the links' }),
        h('ul', { class: 'linklist' }, ...found.map((l) => h('li', {}, l.host || l.url, l.official ? h('span', { class: 'tag official', text: 'government site' }) : null, l.page ? h('span', { class: 'hint', text: `  on page ${l.page}` }) : null)))
      )
    );
  }

  async function addWebLink(url) {
    if (!url) return;
    $('#linkForm').hidden = true;
    $('#linkUrl').value = '';
    setMascot('thinking', 'Reading your link...');
    const r = await postJson('/api/add_link', { url, session_id: state.session });
    setMascot('idle');
    if (!r.ok) {
      addProblem(r.data.detail);
      return;
    }
    addBot(
      h(
        'div',
        {},
        h('p', { class: 'answer' }, 'I added ', h('strong', { text: r.data.title }), r.data.official ? ' (a government site).' : ' (not a government site, so nobody has checked it).'),
        h('p', { class: 'hint', text: 'If your document does not answer a question, I will look here next and tell you where the answer came from.' })
      )
    );
  }

  // ----------------------------------------------------------------- init
  async function init() {
    setupPrefs();
    loadPrefs();
    setupMenu();
    setupIdeas();
    setupVoice();
    $('#askForm').addEventListener('submit', (e) => { e.preventDefault(); ask($('#q').value); });
    $('#q').addEventListener('input', autosize);
    $('#q').addEventListener('keydown', (e) => {
      // Enter sends. Shift+Enter starts a new line.
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask($('#q').value); }
    });
    const r = await api('/api/health');
    if (r.ok) {
      state.aiConnected = r.data.ai_connected;
      state.hint = r.data.hint || '';
      if (r.data.max_question_chars) { state.maxQuestion = r.data.max_question_chars; $('#q').maxLength = state.maxQuestion; }
      if (r.data.max_upload_mb) { state.maxMb = r.data.max_upload_mb; $('#maxMb').textContent = state.maxMb; }
      if (r.data.tagline) $('#tagline').textContent = r.data.tagline;
      if (r.data.provider) $('#providerName').textContent = r.data.provider;
      showOfflineBanner();
    }
  }

  init();
})();
