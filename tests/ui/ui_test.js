// Drives the real UI against the real server (tests/ui/serve_fake.py) with a fake AI.
const { JSDOM, VirtualConsole } = require('jsdom');
const fs = require('fs');
const path = require('path');
const base = 'http://127.0.0.1:8123/';
const SAMPLE = fs.readFileSync(path.join(__dirname, '..', '..', 'data', 'sample.pdf'));
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/fonts\.g|Could not load link/.test(String(e.message))) errors.push('jsdomError: ' + e.message); });
vc.on('error', (e) => errors.push('console.error: ' + e));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let pass = 0, fail = 0;
const ok = (cond, msg) => { cond ? pass++ : (fail++, console.log('FAIL:', msg)); if (cond) console.log('ok  :', msg); };

(async () => {
  const dom = await JSDOM.fromURL(base, {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (u, o = {}) => {
        if (o.body && o.body.constructor && o.body.constructor.name === 'File') {
          const buf = await new Promise((res) => { const fr = new w.FileReader(); fr.onload = () => res(Buffer.from(fr.result)); fr.readAsArrayBuffer(o.body); });
          o = { ...o, body: buf };
        }
        return fetch(new URL(u, base), o);
      };
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = () => {};
      w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener('unhandledrejection', (e) => errors.push('unhandled rejection: ' + (e.reason && e.reason.stack || e.reason)));
  w.addEventListener('error', (e) => errors.push('window error: ' + e.message));
  const $ = (s) => d.querySelector(s);
  const $$ = (s) => [...d.querySelectorAll(s)];
  const submit = (text) => { $('#q').value = text; $('#askForm').dispatchEvent(new w.Event('submit', { cancelable: true })); };
  const lastBot = () => $$('.msg.bot').pop();
  const btn = (root, label) => [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
  await sleep(1000);

  // ---- first screen
  ok($('#welcome').hidden === false, 'welcome card is shown first');
  ok($('#docbar').hidden === true, 'no document bar yet');
  ok($('#stage').classList.contains('compact') === false, 'mascot is large on the welcome screen');
  ok($('#offline').hidden === true, 'offline banner hidden when AI is connected');
  ok($('#mic').hidden === true && $('#micNote').hidden === false, 'mic hidden with a note when speech is unsupported');
  ok($('#q').placeholder.includes('Add a document'), 'input tells you to add a document');
  ok($('#providerName').textContent === 'Google Gemini', 'privacy note names the AI provider: ' + $('#providerName').textContent);

  // ---- + menu
  $('#plus').click();
  ok($('#menu').hidden === false && $('#plus').getAttribute('aria-expanded') === 'true', '+ opens the menu');
  ok($$('#menu [role=menuitem]').length === 2 && $('#menuUpload').textContent === 'Upload a document' && $('#menuLink').textContent === 'Add a web link', 'menu offers upload and web link');
  d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  ok($('#menu').hidden === true, 'Escape closes the menu');

  // ---- asking with no document
  submit('What is this?');
  ok(lastBot().textContent.includes('Add a document first'), 'asking without a document explains what to do');

  // ---- the chat box: multi-line, Enter sends, generous limit
  ok($('#q').tagName === 'TEXTAREA' && $('#q').maxLength === 4000, 'chat box is a multi-line box with a 4000 character limit');
  const enter = (shift) => $('#q').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', shiftKey: shift, bubbles: true, cancelable: true }));
  let before = $$('.msg.bot').length;
  $('#q').value = 'first line'; enter(true);
  ok($$('.msg.bot').length === before && $('#q').value === 'first line', 'Shift+Enter does not send');
  enter(false);
  ok($$('.msg.bot').length === before + 1 && $('#q').value === 'first line', 'Enter sends (no document yet, so it explains and keeps your text)');
  $('#q').value = ''; $('#q').dispatchEvent(new w.Event('input'));

  // ---- try the sample
  $('#trySample').click();
  await sleep(1200);
  ok($('#welcome').hidden === true && $('#docbar').hidden === false, 'welcome hides and document bar shows');
  ok($('#docName').textContent === 'Sample.pdf' && $('#docMeta').textContent.includes('44 pages') && $('#docMeta').textContent.includes('2 links'), 'document bar shows name, pages and link count: ' + $('#docMeta').textContent);
  ok($('#stage').classList.contains('compact'), 'mascot shrinks once a document is loaded');
  const ov = $('.overview');
  ok(ov && ov.querySelector('h3').textContent === 'A planning agreement for affordable housing', 'overview title shown');
  const titles = [...ov.querySelectorAll('.ov-section h4')].map((h) => h.textContent);
  ok(titles.length === 10 && titles[0] === 'What is this document?' && titles[3] === 'What does this mean for local residents?' && titles[9] === 'What happens next?', 'overview has the ten reader questions in order');
  ok(ov.querySelectorAll('.ov-section:not(.missing)').length === 4 && ov.querySelectorAll('.ov-section:not(.missing) .slip').length === 4, 'four written sections, each with an exact-words slip');
  const missing = [...ov.querySelectorAll('.ov-section.missing')];
  ok(missing.length === 6 && missing.every((m) => m.textContent.includes("The document doesn't say.")), 'six sections plainly say the document is silent');
  ok(ov.textContent.includes('Page 28 of your document, printed page 27'), 'slip shows page and printed page');
  ok(ov.textContent.includes('Hard words explained (1)') && ov.textContent.includes('Indemnity'), 'hard words section shown');
  ok(ov.textContent.includes('Other laws and documents it mentions (1)'), 'references section shown');
  ok(ov.querySelector('.linkbox h4').textContent === 'This document has 2 links', 'links box shown');
  ok($$('#ideaChips .chip').length === 4, 'four idea chips shown');
  ok($('#q').placeholder === 'Ask a question', 'placeholder updated');

  // ---- a normal question
  $$('#ideaChips .chip')[0].click();
  await sleep(700);
  ok($('#ideas').hidden === true, 'ideas close after asking');
  let bot = lastBot();
  ok(bot.querySelector('.origin').textContent === 'From your document', 'origin badge: from your document');
  ok(bot.textContent.includes('13,592.7'), 'answer shown');
  ok(bot.querySelector('.slip blockquote') && bot.textContent.includes('Page 27 of your document, printed page 26'), 'answer has a slip with page and printed page');
  ok(btn(bot, 'Explain simpler') && btn(bot, 'Listen again'), 'answer has Explain simpler and Listen again');
  ok(bot.querySelectorAll('.followups .chip').length === 1, 'follow-up chip shown');
  ok($('#mascot').dataset.state === 'speaking', 'mascot speaks');

  // ---- a long question is fine now
  submit('Please explain the affordable housing part of this agreement. '.repeat(15));
  await sleep(700);
  ok($$('.msg.user').pop().textContent.length > 800 && !lastBot().textContent.includes('under'), 'a 900 character question is accepted');

  // ---- explain simpler
  btn(bot, 'Explain simpler').click();
  await sleep(600);
  ok($$('.msg.user').pop().textContent.startsWith('Explain this in shorter, simpler words:'), 'Explain simpler sends a self-contained question');

  // ---- refusal
  submit('Will this increase traffic?');
  await sleep(600);
  bot = lastBot();
  ok(bot.classList.contains('refusal') && bot.textContent.includes("I can't find that in your document"), 'refusal shown');
  ok(!bot.querySelector('.slip') && bot.querySelectorAll('.followups .chip').length === 3, 'no slip, three suggestions');
  ok($('#mascot').dataset.state === 'refuse', 'mascot in refuse state');

  // ---- legal word not in the document
  submit('What does estoppel mean?');
  await sleep(600);
  bot = lastBot();
  ok(bot.querySelector('.origin.warn').textContent === 'General meaning, not from your document', 'general meaning is clearly labelled');
  ok(bot.querySelector('.general-note') && !bot.querySelector('.slip'), 'general meaning has a caution note and no fake source');

  // ---- follow the links
  const readBtn = btn($('.overview'), 'Read the linked pages (up to 10)');
  readBtn.click();
  await sleep(800);
  const lb = $('.linkbox');
  ok(lb.querySelectorAll('.linklist')[0].querySelectorAll('li').length === 2, 'each link result listed');
  ok(lb.textContent.includes('Residential Tenancies Act') && lb.textContent.includes('government site'), 'read link shows title and government tag');
  ok(lb.textContent.includes('blocks readers') && lb.querySelector('.tag.fail'), 'blocked link explained');
  ok(lb.textContent.includes('I read 1 of 2 links'), 'summary of links read: ' + lb.querySelector('.hint[aria-live]').textContent.slice(0, 40));

  // ---- answer that comes from a linked page
  submit('How much notice before an inspection?');
  await sleep(700);
  bot = lastBot();
  ok(bot.querySelector('.origin').textContent === 'From a linked page (government site)', 'linked answer labelled with government site');
  const a = bot.querySelector('.slip a');
  ok(a && a.getAttribute('href') === 'https://www.legislation.nsw.gov.au/act', 'linked slip links to the original page');
  ok(bot.textContent.includes('Section 1 of "Residential Tenancies Act"'), 'linked slip names the page');

  // ---- add a web link (unsafe)
  $('#plus').click(); $('#menuLink').click();
  ok($('#linkForm').hidden === false, 'web link form opens');
  $('#linkUrl').value = 'https://127.0.0.1/admin';
  $('#linkForm').dispatchEvent(new w.Event('submit', { cancelable: true }));
  await sleep(600);
  ok(lastBot().textContent.includes('private'), 'unsafe link refused in plain words');

  // ---- feedback
  bot = $$('.msg.bot').find((m) => m.querySelector('.feedback'));
  btn(bot.querySelector('.feedback'), 'No').click();
  ok(bot.querySelector('.feedback').textContent.includes('What went wrong?'), 'feedback reasons appear');
  btn(bot.querySelector('.feedback'), 'Too hard to understand').click();
  ok(bot.querySelector('.feedback').textContent.includes('Thank you'), 'feedback thanks');

  // ---- upload through the file input, then a bad file
  const fi = $('#file');
  Object.defineProperty(fi, 'files', { value: [new w.File([SAMPLE], 'my agreement.pdf', { type: 'application/pdf' })], configurable: true });
  fi.dispatchEvent(new w.Event('change'));
  await sleep(1500);
  ok($('#docName').textContent === 'my agreement.pdf', 'uploaded file replaces the document: ' + $('#docName').textContent);
  ok($$('.msg.user').length === 0 && $('.overview'), 'chat restarts with a fresh overview');
  Object.defineProperty(fi, 'files', { value: [new w.File([Buffer.from([0, 1, 2, 3])], 'virus.exe')], configurable: true });
  fi.dispatchEvent(new w.Event('change'));
  await sleep(800);
  ok(lastBot().textContent.includes('PDF, Word'), 'unsupported file explained: ' + lastBot().textContent.slice(0, 60));
  ok($('#docName').textContent === 'my agreement.pdf', 'a failed upload keeps the current document');

  // ---- preferences
  $('#contrast').click();
  ok(d.documentElement.dataset.contrast === 'high', 'high contrast toggles');
  $('.sizes button[data-size="l"]').click();
  ok(d.documentElement.dataset.size === 'l', 'text size changes');

  // ---- start again
  $('#newDoc').click();
  await sleep(300);
  ok($('#welcome').hidden === false && $$('.msg').length === 0 && $('#docbar').hidden === true, 'start a new document returns to the welcome screen');
  submit('Anything?');
  ok(lastBot().textContent.includes('Add a document first'), 'old document is gone');

  ok(errors.length === 0, 'no JS errors: ' + errors.join('; '));
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
