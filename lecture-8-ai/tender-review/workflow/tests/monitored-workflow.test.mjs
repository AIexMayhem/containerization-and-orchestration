import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const workflow = JSON.parse(await readFile(new URL('../../workflow.json', import.meta.url), 'utf8'));
const node = (name) => workflow.nodes.find((item) => item.name === name);
const targets = (name, output = 0) => workflow.connections[name].main[output].map((edge) => edge.node);

test('export embeds the exact tested Code node bodies', async () => {
  for (const [name, file] of [
    ['Collection context', 'prepare-collection.js'],
    ['Validate collection', 'validate-collection.js'],
    ['Require complete context', 'require-complete.js'],
    ['Prepare assessment', 'prepare-assessment.js'],
    ['Validate assessment', 'validate-assessment.js'],
  ]) {
    assert.equal(node(name).parameters.jsCode, await readFile(new URL(`../${file}`, import.meta.url), 'utf8'));
  }
});

test('start acknowledgement precedes the agent and both agent outcomes reach validation', () => {
  assert.deepEqual(targets('Collection context'), ['Register collection']);
  assert.deepEqual(targets('Register collection'), ['AI Agent']);
  assert.equal(node('Register collection').onError, undefined);
  assert.equal(node('AI Agent').onError, 'continueErrorOutput');
  assert.equal(node('AI Agent').parameters.options.returnIntermediateSteps, true);
  assert.deepEqual(targets('AI Agent', 0), ['Validate collection']);
  assert.deepEqual(targets('AI Agent', 1), ['Validate collection']);
});

test('the verdict is recorded before the completeness gate stops the workflow', () => {
  assert.deepEqual(targets('Validate collection'), ['Finish collection']);
  assert.deepEqual(targets('Finish collection'), ['Require complete context']);
  for (const name of ['Register collection', 'Finish collection']) {
    const request = node(name);
    assert.equal(request.retryOnFail, true);
    assert.equal(request.maxTries, 3);
    assert.equal(request.waitBetweenTries, 1000);
    assert.equal(request.parameters.options.timeout, 5000);
    // n8n omits false defaults when a workflow is saved in the editor.
    assert.equal(request.parameters.options.response.response.neverError ?? false, false);
    assert.doesNotMatch(request.parameters.jsonBody, /Date|now\(/);
  }
});

test('the import is inactive and omits owner data and execution pins', () => {
  assert.equal(workflow.active, false);
  assert.equal(workflow.id, undefined);
  assert.equal(workflow.shared, undefined);
  assert.equal(workflow.pinData, undefined);
  assert.equal(workflow.nodes.filter((item) => item.type.includes('mcpClientTool')).length, 2);
  assert.equal(node('Ollama/Qwen3:8b').parameters.options.numCtx, 16384);
});
