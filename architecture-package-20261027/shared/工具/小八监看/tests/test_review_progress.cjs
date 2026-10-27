'use strict';

// Runs the real ReviewCenter closure with DOM/request/interval substitutes.
// No service is started and no real candidate, receipt or browser storage is read.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const PAGE = path.join(__dirname, '..', 'xiaoba-monitor.html');
const NOW = 2_000_000_000_000;
const STEP_TEXT = {
  record: '写入正式审核记录',
  publish: '敏感检查、发布经验并校验索引',
  sync: '同步第二大脑回执',
};

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function drain() {
  // Each API call has separate fetch and JSON microtasks. Never wait on real time.
  for (let i = 0; i < 16; i++) await Promise.resolve();
}

class Element {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this.children = new Map();
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.className = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const on = force === undefined ? !names.has(name) : force;
        if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
        return on;
      },
    };
  }
  addEventListener(name, fn) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(fn);
    this.listeners.set(name, listeners);
  }
  async dispatch(name, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    await Promise.all((this.listeners.get(name) || []).map(fn => fn(event)));
  }
  async click() { if (!this.disabled) await this.dispatch('click'); }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(name) {
    if (!this.children.has(name)) this.children.set(name, new Element());
    return this.children.get(name);
  }
  showModal() { this.open = true; }
  close() {
    this.open = false;
    for (const fn of this.listeners.get('close') || []) fn({ target: this });
  }
  getBoundingClientRect() { return { left: 0, right: 500, top: 0, bottom: 500 }; }
  remove() {}
  select() {}
}

function candidate(taskId = 'candidate-a', extra = {}) {
  const evidence = (taskId.endsWith('a') ? 'a' : 'b').repeat(64);
  const expected = { status: 'pending_final_review', evidence_sha256: evidence, updated_at: NOW / 1000 - 100 };
  return {
    task_id: taskId, title: taskId === 'candidate-a' ? '候选 A' : '候选 B',
    stage: 'final', status: expected.status, status_label: '等待终审',
    created_at: NOW / 1000 - 200, updated_at: expected.updated_at,
    evidence_sha256: evidence, expected, hash_matches: true,
    allowed_decisions: ['approve', 'revise', 'reject'], body: '测试候选正文',
    prompt: '测试候选', reviews: [], ...extra,
  };
}

function liveState(items, extra = {}) {
  return { ok: true, items, reviews: [], holds: 0, orphans: [], release: 'test',
    revision: 'initial', sync: { ok: true, at: '2026-10-05 20:00:00' }, ...extra };
}

function record(item, extra = {}) {
  return { id: 'receipt-new-' + item.task_id, task_id: item.task_id,
    stage: item.stage, decision: 'approve', candidate_sha256: item.expected.evidence_sha256,
    created_at: NOW / 1000 + 1, ...extra };
}

function receipt(items, extra = {}) {
  return { id: 'packet-1', items: items.map(item => ({ task_id: item.task_id, ok: true, recorded: true,
    review_id: 'receipt-new-' + item.task_id, status: 'published' })),
    sync: { ok: true, at: '2026-10-05 20:01:00' }, ...extra };
}

function harness(initial) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const choices = ['approve', 'revise', 'reject'].map(decision => {
    const element = new Element('choice-' + decision);
    element.dataset.decision = decision;
    return element;
  });
  const intervals = new Map();
  let intervalId = 0, uuid = 0, now = NOW;
  const stateResponses = [], decisionResponses = [], requests = [];
  const candidates = new Map(initial.items.map(item => [item.task_id, item]));
  const copied = [];
  const h = { el, choices, intervals, stateResponses, decisionResponses, requests, candidates, copied, live: initial };
  const token = { content: 'fixture-token', remove() {} };
  const document = {
    hidden: false, documentElement: { dataset: {} },
    querySelector: query => query.startsWith('meta[') ? token : choices.find(item => query.includes('"' + item.dataset.decision + '"')),
    querySelectorAll: query => query === '.choice' ? choices : [],
    addEventListener() {},
  };
  const store = new Map();
  class FixtureDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({
    document, $: el,
    esc: input => String(input ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    Date: FixtureDate,
    location: { hash: '#review', search: '' }, URLSearchParams,
    localStorage: { getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value) },
    navigator: { clipboard: { async writeText(value) { copied.push(value); } } },
    crypto: { randomUUID: () => 'packet-' + ++uuid },
    Tabs: { current: 'overview', onChange() {}, show() {} },
    addEventListener() {},
    setInterval: (fn, ms) => { const id = ++intervalId; intervals.set(id, { fn, ms }); return id; },
    clearInterval: id => intervals.delete(id),
    fetch: async (url, init = {}) => {
      requests.push({ url, init, body: init.body ? JSON.parse(init.body) : undefined });
      let value;
      if (url === 'api/review/state') value = stateResponses.length ? stateResponses.shift() : h.live;
      else if (url.startsWith('api/review/candidate?id=')) value = candidates.get(decodeURIComponent(url.split('=')[1]));
      else if (url === 'api/review/decisions') {
        assert.ok(decisionResponses.length, 'each simulated POST must have an explicit fixture response');
        value = decisionResponses.shift();
      } else throw new Error('Unexpected fixture API: ' + url);
      value = await value;
      return { ok: true, status: 200, async json() { return value; } };
    },
  });
  const page = fs.readFileSync(PAGE, 'utf8');
  const begin = page.indexOf('const ReviewCenter = (() => {');
  const end = page.indexOf('\nTabs.show(', begin);
  assert.ok(begin >= 0 && end > begin, 'the actual ReviewCenter closure must be found');
  let source = page.slice(begin, end);
  assert.match(source, /return \{open, refresh, badge\};/, 'production exports are unchanged');
  // Test-only access to existing functions; production source/export stays untouched.
  source = source.replace('return {open, refresh, badge};',
    'return {open, refresh, badge, __test: {submit, openSummary, select, closeDialog}};');
  vm.runInContext(source + '\nglobalThis.review = ReviewCenter;', context, { filename: PAGE });
  h.rc = context.review;
  h.fn = h.rc.__test;
  h.poll = () => {
    now += 1500;
    const polling = [...intervals.values()].filter(timer => timer.ms === 1500);
    assert.equal(polling.length, 1, 'one progress interval must exist during submission');
    return Promise.all(polling.map(timer => timer.fn()));
  };
  h.prepare = async (items = initial.items) => {
    await h.rc.refresh();
    for (const item of items) {
      await h.fn.select(item.task_id);
      await choices[0].click();
      await el('rcSave').click();
    }
    await el('rcReview').click();
    assert.equal(el('rcDialog').dataset.phase, 'review');
  };
  h.start = async () => {
    const pending = deferred();
    decisionResponses.push(pending.promise);
    const completion = h.fn.submit();
    await drain();
    assert.equal(el('rcDialog').dataset.phase, 'submitting');
    assert.equal(requests.filter(request => request.url === 'api/review/decisions').length > 0, true);
    return { pending, completion };
  };
  h.step = (index, key) => {
    const groups = [...el('rcItems').innerHTML.matchAll(/<ul class="rc-steps">([\s\S]*?)<\/ul>/g)];
    assert.ok(groups[index], 'rendered candidate step list exists');
    const row = [...groups[index][1].matchAll(/<li class="([^"]*)">([^<]*)<\/li>/g)]
      .find(match => match[2].startsWith(STEP_TEXT[key]));
    assert.ok(row, 'rendered step exists: ' + key);
    return row[1].split(/\s+/).filter(Boolean);
  };
  return h;
}

test('POST 尚未返回时，每 1.5 秒取得匹配的新审核记录才点绿', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const attempt = await h.start();
  h.live = liveState([item], { reviews: [record(item)], revision: 'recorded' });
  await h.poll();
  assert.ok(h.step(0, 'record').includes('ok'));
  assert.ok(!h.step(0, 'publish').includes('ok'));
  assert.equal(h.el('rcDialog').dataset.phase, 'submitting');
  attempt.pending.resolve(receipt([item]));
  await attempt.completion;
});

for (const variant of ['旧 record ID', '不同 decision', '不同 evidence hash']) {
  test('实时状态中的' + variant + '不得点绿', async () => {
    const item = candidate();
    const old = record(item, { id: 'receipt-old', created_at: NOW / 1000 - 1 });
    const initial = liveState([item], { reviews: variant === '旧 record ID' ? [old] : [] });
    const h = harness(initial);
    await h.prepare();
    const attempt = await h.start();
    const wrong = variant === '旧 record ID' ? { ...old, created_at: NOW / 1000 + 1 }
      : variant === '不同 decision' ? record(item, { decision: 'reject' })
      : record(item, { candidate_sha256: 'c'.repeat(64) });
    h.live = liveState([item], { reviews: [wrong], revision: 'changed' });
    await h.poll();
    assert.ok(!h.step(0, 'record').includes('ok'), variant + ' cannot confirm this packet');
    attempt.pending.resolve(receipt([item]));
    await attempt.completion;
  });
}

for (const ok of [false, true]) {
  test((ok ? '不相关同步时间变化' : '同步失败且时间变化') + '不得推断发布或同步成功', async () => {
    const item = candidate();
    const h = harness(liveState([item]));
    await h.prepare();
    const attempt = await h.start();
    h.live = liveState([item], { reviews: [record(item)], revision: 'changed',
      sync: { ok, at: '2026-10-05 20:02:00', error: ok ? undefined : 'fixture sync failed' } });
    await h.poll();
    assert.ok(h.step(0, 'record').includes('ok'));
    assert.ok(!h.step(0, 'publish').includes('ok'), 'sync timestamp is not a publish receipt');
    assert.ok(!h.step(0, 'sync').includes('ok'), 'sync timestamp is not correlated with this packet');
    attempt.pending.resolve(receipt([item]));
    await attempt.completion;
  });
}

test('POST 完成后，迟到轮询不得改写正式结果', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const attempt = await h.start();
  const late = deferred();
  h.stateResponses.push(late.promise);
  const polling = h.poll();
  await drain();
  attempt.pending.resolve(receipt([item]));
  await attempt.completion;
  const html = h.el('rcItems').innerHTML;
  late.resolve(liveState([item], { reviews: [record(item)], sync: { ok: false, at: 'later' } }));
  await polling;
  assert.equal(h.el('rcDialog').dataset.phase, 'done');
  assert.equal(h.el('rcItems').innerHTML, html);
});

test('前一轮迟到轮询不得点亮正在重试的新一轮进度', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const first = await h.start();
  const late = deferred();
  h.stateResponses.push(late.promise);
  const polling = h.poll();
  await drain();
  first.pending.reject(new Error('fixture lost receipt'));
  await first.completion;
  const second = await h.start();
  const html = h.el('rcItems').innerHTML;
  late.resolve(liveState([item], { reviews: [record(item)], revision: 'late', sync: { ok: true, at: 'later' } }));
  await polling;
  assert.equal(h.el('rcItems').innerHTML, html);
  assert.ok(!h.step(0, 'record').includes('ok'));
  second.pending.resolve(receipt([item]));
  await second.completion;
});

test('丢失回执且候选离开待审队列后，关闭再核对仍用同一 packet id 重试', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const first = await h.start();
  const originalPacket = h.requests.find(request => request.url === 'api/review/decisions').body;
  h.live = liveState([], { reviews: [record(item)], revision: 'removed' });
  first.pending.reject(new Error('fixture lost receipt'));
  await first.completion;
  assert.equal(h.el('rcDialog').dataset.phase, 'error');
  await h.el('rcClose').click();
  assert.equal(h.el('rcDialog').open, false);
  assert.equal(h.el('rcReview').disabled, false, 'original review button keeps the retry reachable');
  await h.el('rcReview').click();
  assert.equal(h.el('rcDialog').open, true);
  assert.match(h.el('rcSubmit').textContent, /重试/);
  const second = await h.start();
  const submitted = h.requests.filter(request => request.url === 'api/review/decisions');
  assert.deepEqual(submitted[1].body, originalPacket);
  second.pending.resolve(receipt([item]));
  await second.completion;
});

test('recovered:true / ok:false / status:published 只确认记录，必须展示后续失败原因', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const attempt = await h.start();
  const error = '已找到审核记录，完整回执缺失，请核对发布与后续处理状态';
  attempt.pending.resolve(receipt([item], { items: [{ task_id: item.task_id,
    ok: false, recorded: true, recovered: true, review_id: 'receipt-recovered', status: 'published', error }] }));
  await attempt.completion;
  assert.equal(h.el('rcDialog').dataset.result, 'partial');
  assert.ok(h.step(0, 'record').includes('ok'));
  assert.ok(!h.step(0, 'publish').includes('ok'));
  assert.ok(h.el('rcItems').innerHTML.includes(error));
  assert.ok(!h.el('rcItems').innerHTML.includes('经验已发布，并通过正式索引验证'));
  await h.el('rcCopy').click();
  assert.ok(h.copied.at(-1).includes(error));
  assert.ok(!h.copied.at(-1).includes('经验已发布，并通过正式索引验证'));
});

test('混合成功与失败的正式回执不能显示全部处理成功', async () => {
  const items = [candidate(), candidate('candidate-b')];
  const h = harness(liveState(items));
  await h.prepare();
  const attempt = await h.start();
  attempt.pending.resolve(receipt(items, { items: [receipt(items).items[0], {
    task_id: items[1].task_id, ok: false, recorded: true, review_id: 'receipt-b',
    status: 'pending_final_review', error: 'fixture publish failed',
  }] }));
  await attempt.completion;
  assert.equal(h.el('rcDialog').dataset.result, 'partial');
  assert.ok(!h.el('rcProgressTitle').textContent.includes('全部处理完成'));
  assert.ok(h.step(0, 'publish').includes('ok'));
  assert.ok(!h.step(1, 'publish').includes('ok'));
  assert.ok(h.el('rcItems').innerHTML.includes('fixture publish failed'));
});

test('逐项成功但第二大脑同步失败，结果不得声称全部处理完成', async () => {
  const item = candidate();
  const h = harness(liveState([item]));
  await h.prepare();
  const attempt = await h.start();
  attempt.pending.resolve(receipt([item], { sync: { ok: false, at: '', error: 'fixture sync failed' } }));
  await attempt.completion;
  assert.equal(h.el('rcDialog').dataset.result, 'partial');
  assert.ok(!h.el('rcProgressTitle').textContent.includes('全部处理完成'));
  assert.ok(!h.step(0, 'sync').includes('ok'));
  assert.ok(h.el('rcSyncLine').textContent.includes('fixture sync failed'));
});

test('正式回执缺一项时，该项不能沿用轮询绿点或被算入全部成功', async () => {
  const items = [candidate(), candidate('candidate-b')];
  const h = harness(liveState(items));
  await h.prepare();
  const attempt = await h.start();
  h.live = liveState(items, { reviews: items.map(item => record(item)), revision: 'recorded' });
  await h.poll();
  attempt.pending.resolve(receipt(items, { items: [receipt(items).items[0]] }));
  await attempt.completion;
  assert.equal(h.el('rcDialog').dataset.phase, 'error');
  assert.ok(!h.el('rcProgressTitle').textContent.includes('全部处理完成'));
  assert.ok(!h.step(1, 'publish').includes('ok'));
  assert.ok(!h.step(1, 'sync').includes('ok'));
});
