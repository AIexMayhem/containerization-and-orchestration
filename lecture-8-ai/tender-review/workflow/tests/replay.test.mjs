import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const fixture = JSON.parse(await readFile(new URL('./fixtures/execution-15-tools.json', import.meta.url), 'utf8'));
const prepare = new Function('$execution', 'Date', await readFile(new URL('../prepare-collection.js', import.meta.url), 'utf8'));
const validate = new Function('$input', '$', 'Date', await readFile(new URL('../validate-collection.js', import.meta.url), 'utf8'));

test('replay of recorded execution 15 detects the unread mandatory contract', () => {
  const [context] = prepare({ id: fixture.execution_id }, { now: () => 100_000 });
  const [result] = validate(
    { first: () => ({ json: fixture }) },
    () => ({ first: () => context }),
    { now: () => 107_000 },
  );
  assert.equal(result.json.collection.outcome, 'incomplete');
  assert.deepEqual(result.json.collection.missing_documents, [
    { document_id: 'draft-contract', document_version: 'v1' },
  ]);
  assert.deepEqual(result.json.collection.reviewed_documents, [
    { document_id: 'technical-spec', document_version: 'v1' },
  ]);
  assert.deepEqual(result.json.collection.errors, []);
  assert.equal(result.json.telemetry.finish.execution_id, '15');
  assert.equal(result.json.telemetry.finish.outcome, 'incomplete');
});
