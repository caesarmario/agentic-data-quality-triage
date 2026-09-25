/**
 * Proxy policy tests without Next.js, provider calls, or private credentials.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { browserCapability, browserRequestError, readBoundedBody, MAX_REQUEST_BYTES } from '../lib/proxy-policy.ts';

// --- Exercising Exact Route Boundaries
for (const [method, path, expected] of [
  ['GET', 'api/v1/alerts', 'read'],
  ['GET', 'api/v1/reports/read', 'read'],
  ['GET', 'api/v1/metadata/assets/dq.raw_orders', 'read'],
  ['POST', 'api/v1/triage/run', 'triage'],
  ['POST', 'api/v1/approvals/requests/APR-123/decision', 'approval'],
  ['POST', 'api/v1/approvals/requests/APR-123/cancel', 'approval'],
  ['POST', 'api/v1/approvals/requests/APR-123/execute', null],
  ['GET', 'api/v1/alerts/unreviewed/future-route', null],
  ['GET', 'api/v1/alerts/../approvals/requests', null],
  ['GET', 'api/v1/alerts/%2e%2e', null],
  ['GET', 'api/v1/alerts/a%2fb', null],
  ['POST', 'api/v1/approvals/requests', null],
  ['DELETE', 'api/v1/alerts', null],
]) {
  test(`capability ${method} ${path}`, () => {
    assert.equal(browserCapability(method, path.split('/')), expected);
  });
}

// --- Exercising Browser Identity And Content Rules
const trusted = 'http://localhost:3000';
for (const [name, overrides, expected] of [
  ['same origin', {}, null],
  ['missing origin', { origin: '' }, 403],
  ['foreign origin', { origin: 'https://attacker.invalid' }, 403],
  ['null origin', { origin: 'null' }, 403],
  ['lookalike origin', { origin: 'http://localhost:3000.attacker.invalid' }, 403],
  ['DNS rebinding', { host: 'attacker.invalid:3000' }, 403],
  ['forwarded spoof', { origin: '', 'x-forwarded-host': 'localhost:3000' }, 403],
  ['cross-site metadata', { 'sec-fetch-site': 'cross-site' }, 403],
  ['HTML form', { 'content-type': 'application/x-www-form-urlencoded' }, 415],
  ['plain text', { 'content-type': 'text/plain' }, 415],
  ['JSON charset', { 'content-type': 'application/json; charset=utf-8' }, null],
]) {
  test(name, () => {
    const headers = new Headers({ host: 'localhost:3000', origin: trusted, 'content-type': 'application/json', ...overrides });
    assert.equal(browserRequestError('POST', headers, trusted), expected);
  });
}
test('configuration errors fail closed', () => {
  assert.equal(browserRequestError('POST', new Headers(), 'not-a-url'), 503);
  assert.equal(browserRequestError('GET', new Headers(), trusted + '/'), 503);
});
test('read requests need trusted Host but no Origin', () => {
  assert.equal(browserRequestError('GET', new Headers({ host: 'localhost:3000' }), trusted), null);
});

// --- Exercising Actual Body Size Rather Than Trusting Headers
for (const [name, body, headers, rejected] of [
  ['small body', '{}', {}, false],
  ['exact byte boundary', 'x'.repeat(MAX_REQUEST_BYTES), {}, false],
  ['large stream', 'x'.repeat(MAX_REQUEST_BYTES + 1), {}, true],
  ['multibyte body', 'é'.repeat(MAX_REQUEST_BYTES), {}, true],
  ['lying length', 'x'.repeat(MAX_REQUEST_BYTES + 1), { 'content-length': '2' }, true],
  ['declared oversize', '{}', { 'content-length': String(MAX_REQUEST_BYTES + 1) }, true],
]) {
  test(name, async () => {
    const request = new Request(trusted, { method: 'POST', body, headers });
    if (rejected) await assert.rejects(readBoundedBody(request), RangeError);
    else assert.equal(await readBoundedBody(request), body);
  });
}
