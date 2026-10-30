import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../prepare-collection.js', import.meta.url), 'utf8');
const run = new Function('$execution', 'Date', source);

test('captures execution identity and start time before retryable requests', () => {
  let now = 100_250;
  const [item] = run({ id: '17' }, { now: () => now });
  now = 200_000;
  assert.equal(item.json.execution_id, '17');
  assert.equal(item.json.started_at, 100.25);
  assert.equal(item.json.workflow, 'tender-precheck');
  assert.equal(typeof item.json.expected, 'object');
  assert.equal(Object.keys(item.json.expected).length, 6);
});

test('accepts a numeric n8n identity as a string suitable for the exporter', () => {
  const [item] = run({ id: 18 }, { now: () => 100_000 });
  assert.equal(item.json.execution_id, '18');
});

for (const id of [undefined, null, '', ' ', {}, [], '17\n', 'x'.repeat(129)]) {
  test(`rejects unusable execution identity ${JSON.stringify(id)}`, () => {
    assert.throws(() => run({ id }, Date), /execution/i);
  });
}
