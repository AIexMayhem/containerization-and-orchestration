import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../require-complete.js', import.meta.url), 'utf8');
const run = new Function('$', source);
const fromNode = (value) => (name) => {
  assert.equal(name, 'Validate collection');
  return { first: () => ({ json: value }) };
};

test('returns the verified sources after the exporter ACK', () => {
  const value = { collection: { outcome: 'complete', sources: { documents: [{ text: 'source' }] } } };
  assert.deepEqual(run(fromNode(value)), [{ json: value }]);
});

test('blocks an unread mandatory document after its incomplete event was recorded', () => {
  assert.throws(() => run(fromNode({ collection: {
    outcome: 'incomplete',
    missing_documents: [{ document_id: 'draft-contract', document_version: 'v1' }],
    errors: [],
  } })), /incomplete.*draft-contract\/v1/);
});

test('preserves technical failure diagnostics in the stopped execution', () => {
  assert.throws(() => run(fromNode({ collection: {
    outcome: 'error', missing_documents: [],
    errors: [{ code: 'TOOL_ERROR', detail: 'Connection refused' }],
  } })), /error.*TOOL_ERROR.*Connection refused/);
});

test('fails closed when the expected validation result is missing', () => {
  assert.throws(() => run(fromNode({})), /validation result/i);
});
