// Optional: node --test tests/test_manage_page.cjs (no npm packages required).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/manage.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../frontend/manage.html'), 'utf8');
const example = {
  case_id: 'case-one', player_description: '<img src=x onerror=alert(1)>',
  origin: 'synthetic', status: 'investigating', observation_count: 1,
  test_result_count: 2, history_event_count: 3, deletion_token: 'a'.repeat(64),
};

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function harness() {
  const elements = new Map();
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    const element = {
      value: '', textContent: '', dataset: {}, hidden: match[1] === 'preview',
      disabled: match[1] === 'delete-button', handlers: {}, focus() {},
      addEventListener(name, callback) { this.handlers[name] = callback; },
    };
    Object.defineProperty(element, 'innerHTML', { set() { throw new Error('Unsafe HTML write'); } });
    elements.set(match[1], element);
  }
  const calls = [];
  const replies = [];
  vm.runInNewContext(source, {
    document: { getElementById: id => {
      assert(elements.has(id), `Missing HTML element: ${id}`);
      return elements.get(id);
    } },
    AbortController,
    fetch: (url, options) => {
      calls.push({ url, options });
      assert(replies.length > 0, 'Unexpected HTTP request');
      return replies.shift();
    },
  });
  const el = id => elements.get(id);
  const fire = async (id, name) => el(id).handlers[name]({ preventDefault() {} });
  const review = async () => {
    el('case-id').value = 'case-one';
    replies.push(response(example));
    await fire('lookup-form', 'submit');
  };
  const confirm = async () => {
    el('confirm-id').value = 'case-one';
    await fire('confirm-id', 'input');
  };
  return { el, fire, calls, replies, review, confirm };
}

test('review uses text content and requires the exact case ID', async () => {
  const h = harness();
  await h.review();
  assert.equal(h.el('description').textContent, example.player_description);
  assert.equal(h.el('preview').hidden, false);
  assert.equal(h.el('delete-button').disabled, true);
  h.el('confirm-id').value = 'case-two';
  await h.fire('confirm-id', 'input');
  await h.fire('delete-form', 'submit');
  assert.equal(h.calls.length, 1);
  await h.confirm();
  assert.equal(h.el('delete-button').disabled, false);
});

test('cancel clears preview and does not issue a deletion', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  await h.fire('cancel-button', 'click');
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('confirm-id').value, '');
  assert.equal(h.el('delete-button').disabled, true);
  assert.equal(h.calls.length, 1);
});

test('editing the case ID invalidates an already displayed preview', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  h.el('case-id').value = 'case-two';
  await h.fire('case-id', 'input');
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('delete-button').disabled, true);
});

test('a late lookup response cannot replace the new selection', async () => {
  const h = harness();
  const late = deferred();
  h.el('case-id').value = 'case-one';
  h.replies.push(late.promise);
  const loading = h.fire('lookup-form', 'submit');
  h.el('case-id').value = 'case-two';
  await h.fire('case-id', 'input');
  late.resolve(response(example));
  await loading;
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('delete-button').disabled, true);
  assert.equal(h.el('case-id').value, 'case-two');
});

test('double submission makes one delete request and clears the case on success', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  const pending = deferred();
  h.replies.push(pending.promise);
  const first = h.fire('delete-form', 'submit');
  await h.fire('delete-form', 'submit');
  assert.equal(h.calls.length, 2);
  assert.equal(h.el('case-id').disabled, true);
  assert.equal(h.calls[1].options.method, 'DELETE');
  assert.equal(h.calls[1].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(h.calls[1].options.body), {
    confirm_case_id: example.case_id, deletion_token: example.deletion_token,
  });
  pending.resolve(response({ case_id: 'case-one', deleted: true }));
  await first;
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('case-id').value, '');
  assert.equal(h.el('case-id').disabled, false);
  assert.equal(h.el('feedback').dataset.tone, 'success');
});

test('a changed case requires another lookup and confirmation', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  h.replies.push(response({ detail: 'The case changed. Review it again before deleting.' }, 409));
  await h.fire('delete-form', 'submit');
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('confirm-id').value, '');
  assert.match(h.el('feedback').textContent, /case changed/);
  assert.equal(h.el('case-id').value, 'case-one');
});

test('an uncertain network result never claims successful deletion', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  const pending = deferred();
  h.replies.push(pending.promise);
  const deletion = h.fire('delete-form', 'submit');
  pending.reject(new Error('Network disconnected'));
  await deletion;
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('feedback').dataset.tone, 'error');
  assert.match(h.el('feedback').textContent, /could not be confirmed/);
  assert.equal(h.el('case-id').value, 'case-one');
});

test('request limit errors show the server wait and require review again', async () => {
  const h = harness();
  await h.review();
  await h.confirm();
  h.replies.push(response({ detail: 'Too many requests. Try again in 30 seconds.' }, 429));
  await h.fire('delete-form', 'submit');
  assert.match(h.el('feedback').textContent, /30 seconds/);
  assert.equal(h.el('delete-button').disabled, true);
});
