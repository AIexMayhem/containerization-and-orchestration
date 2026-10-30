import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const script = await readFile(new URL('../validate-collection.js', import.meta.url), 'utf8');
const execute = new AsyncFunction('$input', '$', 'Date', script);
const fixtureRoot = new URL('../../fixtures/', import.meta.url);
const company = JSON.parse(await readFile(new URL('reference/companies/demo-integrator/v1.json', fixtureRoot)));
const policy = JSON.parse(await readFile(new URL('reference/policies/tender-precheck/v1.json', fixtureRoot)));
const manifest = JSON.parse(await readFile(new URL('tenders/demo-tender/v1/manifest.json', fixtureRoot)));
const inventory = {
  tender_id: 'demo-tender', package_version: 'v1',
  documents: manifest.documents.map(({ document_id, document_version, required }) => ({ document_id, document_version, required })),
};
const technicalText = await readFile(new URL('tenders/demo-tender/v1/technical-spec.md', fixtureRoot), 'utf8');
const contractText = await readFile(new URL('tenders/demo-tender/v1/draft-contract.md', fixtureRoot), 'utf8');
const technical = {
  tender_id: 'demo-tender', package_version: 'v1', document_id: 'technical-spec', document_version: 'v1', text: technicalText,
  sections: [
    { section_id: 'TS-1', title: 'Предмет работ', text: 'Исполнитель обеспечивает сопровождение внутреннего веб-приложения заказчика: приём обращений, диагностику неисправностей и восстановление работоспособности.', source_ref: 'technical-spec/v1#TS-1' },
    { section_id: 'TS-2', title: 'Режим поддержки', text: 'Приём и обработка обращений инженерами поддержки должны быть доступны круглосуточно, семь дней в неделю, включая праздничные дни: 24×7.', source_ref: 'technical-spec/v1#TS-2' },
  ],
};
const contract = {
  tender_id: 'demo-tender', package_version: 'v1', document_id: 'draft-contract', document_version: 'v1', text: contractText,
  sections: [
    { section_id: 'CT-1', title: 'Обязанности сторон', text: 'Исполнитель оказывает услуги сопровождения в соответствии с техническим заданием. Заказчик предоставляет доступ к сопровождаемому приложению и назначает ответственного сотрудника для взаимодействия.', source_ref: 'draft-contract/v1#CT-1' },
    { section_id: 'CT-2', title: 'Учёт оказанных услуг', text: 'Исполнитель ведёт журнал обращений и ежемесячно передаёт заказчику отчёт с перечнем выполненных работ. Заказчик рассматривает отчёт и направляет замечания по его содержанию.', source_ref: 'draft-contract/v1#CT-2' },
  ],
};
const context = {
  workflow: 'tender-precheck', execution_id: '123', started_at: 1793350800,
  expected: { tender_id: 'demo-tender', package_version: 'v1', company_id: 'demo-integrator', profile_version: 'v1', policy_id: 'tender-precheck', policy_version: 'v1' },
};

function call(name, input, data, overrides = {}) {
  return {
    action: { tool: `MCP_Client1_${name}`, toolInput: input },
    observation: JSON.stringify([{ response: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data }]),
    ...overrides,
  };
}
function completeSteps() {
  return [
    call('get_evaluation_policy', { policy_id: 'tender-precheck', policy_version: 'v1' }, policy),
    call('get_company_profile', { company_id: 'demo-integrator', profile_version: 'v1' }, company),
    call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, inventory),
    call('read_tender_document', { document_id: 'technical-spec', document_version: 'v1' }, technical),
    call('read_tender_document', { document_id: 'draft-contract', document_version: 'v1' }, contract),
  ];
}
async function run(intermediateSteps, extra = {}) {
  return runInput({ output: 'Все прочитано', intermediateSteps, ...extra });
}
async function runInput(input) {
  const result = await execute(
    { first: () => ({ json: input }) },
    (name) => {
      assert.equal(name, 'Collection context');
      return { first: () => ({ json: structuredClone(context) }) };
    },
    { now: () => 1793351220000 },
  );
  assert.ok(Array.isArray(result), 'Code node must return an n8n items array');
  assert.equal(result.length, 1);
  return result[0].json;
}

test('complete collection retains the exact sources and stable event identities', async () => {
  const steps = completeSteps();
  steps[0].action.messageLog = ['private model reasoning'];
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'complete');
  assert.deepEqual(result.collection.errors, []);
  assert.deepEqual(result.collection.missing_documents, []);
  assert.deepEqual(result.collection.reviewed_documents, [
    { document_id: 'technical-spec', document_version: 'v1' },
    { document_id: 'draft-contract', document_version: 'v1' },
  ]);
  assert.deepEqual(result.collection.sources, { policy, company, package: inventory, documents: [technical, contract] });
  assert.deepEqual(result.telemetry, {
    start: { workflow: 'tender-precheck', execution_id: '123', started_at: 1793350800 },
    finish: { workflow: 'tender-precheck', execution_id: '123', finished_at: 1793351220, outcome: 'complete' },
  });
  assert.equal(JSON.stringify(result).includes('private model reasoning'), false);
  assert.equal('output' in result, false);
});

test('actual four-call pattern reports the uncalled contract as incomplete', async () => {
  const result = await run(completeSteps().slice(0, 4), { output: 'Сервер не ответил при чтении draft-contract' });
  assert.equal(result.collection.outcome, 'incomplete');
  assert.deepEqual(result.collection.missing_documents, [{ document_id: 'draft-contract', document_version: 'v1' }]);
  assert.deepEqual(result.collection.errors, []);
});

test('repeating one successful read does not cover a different required document', async () => {
  const steps = completeSteps().slice(0, 4);
  steps.push(structuredClone(steps[3]));
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'incomplete');
  assert.equal(result.collection.reviewed_documents.length, 1);
  assert.deepEqual(result.collection.missing_documents, [{ document_id: 'draft-contract', document_version: 'v1' }]);
});

test('invented agent output cannot override complete tool responses', async () => {
  const result = await run(completeSteps(), { output: 'ERROR: сервер сломан, doc123 не прочитан', messageLog: ['reasoning'] });
  assert.equal(result.collection.outcome, 'complete');
  assert.equal(JSON.stringify(result).includes('doc123'), false);
});

for (const [name, change] of [
  ['different document version returned', (steps) => { steps[4] = call('read_tender_document', { document_id: 'draft-contract', document_version: 'v1' }, { ...contract, document_version: 'v2' }); }],
  ['different package returned', (steps) => { steps[4] = call('read_tender_document', { document_id: 'draft-contract', document_version: 'v1' }, { ...contract, package_version: 'v2' }); }],
  ['different document requested', (steps) => { steps[4].action.toolInput.document_id = 'doc123'; }],
  ['wrong policy requested', (steps) => { steps[0].action.toolInput.policy_id = 'invented'; }],
  ['wrong company returned', (steps) => { steps[1] = call('get_company_profile', { company_id: 'demo-integrator', profile_version: 'v1' }, { ...company, company_id: 'other' }); }],
  ['wrong inventory version returned', (steps) => { steps[2] = call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, { ...inventory, package_version: 'v2' }); }],
]) {
  test(`${name} is an error and cannot complete collection`, async () => {
    const steps = completeSteps();
    change(steps);
    const result = await run(steps);
    assert.equal(result.collection.outcome, 'error');
    assert.ok(result.collection.errors.length > 0);
  });
}

for (const [name, overrides] of [
  ['action.errored', { action: { tool: 'MCP_Client_read_tender_document', toolInput: { document_id: 'draft-contract', document_version: 'v1' }, errored: true }, observation: 'ECONNREFUSED' }],
  ['isError response', { observation: JSON.stringify([{ isError: true, content: [{ type: 'text', text: 'DOCUMENT_UNAVAILABLE' }] }]) }],
  ['is_error response', { observation: { is_error: true, response: 'DOCUMENT_UNAVAILABLE' } }],
]) {
  test(`${name} is a confirmed required-source failure`, async () => {
    const steps = completeSteps();
    steps[4] = { ...steps[4], ...overrides };
    const result = await run(steps);
    assert.equal(result.collection.outcome, 'error');
    assert.deepEqual(result.collection.missing_documents, [{ document_id: 'draft-contract', document_version: 'v1' }]);
    assert.ok(result.collection.errors.some((item) => item.code === 'TOOL_ERROR'));
    if (name !== 'action.errored') assert.ok(result.collection.errors.some((item) => item.detail.includes('DOCUMENT_UNAVAILABLE')));
  });
}

test('an exact successful retry recovers a failed required-source call', async () => {
  const steps = completeSteps();
  steps.splice(4, 0, { ...steps[4], observation: { isError: true, content: [{ type: 'text', text: 'Temporary failure' }] } });
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'complete');
  assert.deepEqual(result.collection.errors, []);
});

test('successful sources may arrive after their document reads', async () => {
  const steps = completeSteps();
  const result = await run([steps[4], steps[3], steps[2], steps[0], steps[1]]);
  assert.equal(result.collection.outcome, 'complete');
});

test('a failed optional document does not block completeness when policy permits it', async () => {
  const steps = completeSteps();
  const optionalInventory = structuredClone(inventory);
  optionalInventory.documents[1].required = false;
  steps[0] = call('get_evaluation_policy', { policy_id: 'tender-precheck', policy_version: 'v1' }, { ...policy, all_package_documents_required: false });
  steps[2] = call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, optionalInventory);
  steps[4].observation = { isError: true, content: [{ type: 'text', text: 'Optional unavailable' }] };
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'complete');
  assert.deepEqual(result.collection.missing_documents, []);
  assert.deepEqual(result.collection.errors, []);
});

test('a foreign package returned for an optional document remains a contract error', async () => {
  const steps = completeSteps();
  const optionalInventory = structuredClone(inventory);
  optionalInventory.documents[1].required = false;
  steps[0] = call('get_evaluation_policy', { policy_id: 'tender-precheck', policy_version: 'v1' }, { ...policy, all_package_documents_required: false });
  steps[2] = call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, optionalInventory);
  steps[4] = call('read_tender_document', { document_id: 'draft-contract', document_version: 'v1' }, { ...contract, package_version: 'foreign' });
  assert.equal((await run(steps)).collection.outcome, 'error');
});

test('policy requiring all documents overrides optional inventory flags', async () => {
  const steps = completeSteps().slice(0, 4);
  const optionalInventory = structuredClone(inventory);
  optionalInventory.documents[1].required = false;
  steps[2] = call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, optionalInventory);
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'incomplete');
  assert.deepEqual(result.collection.missing_documents, [{ document_id: 'draft-contract', document_version: 'v1' }]);
});

test('no tool steps are incomplete and do not invent an inventory', async () => {
  const result = await run([]);
  assert.equal(result.collection.outcome, 'incomplete');
  assert.deepEqual(result.collection.missing_documents, []);
  assert.deepEqual(result.collection.sources, { policy: null, company: null, package: null, documents: [] });
  assert.ok(result.collection.errors.some((item) => item.code === 'SOURCE_NOT_CALLED'));
});

test('n8n error output is a runtime failure even with complete tool responses', async () => {
  const result = await run(completeSteps(), { error: { message: 'Maximum iterations reached' } });
  assert.equal(result.collection.outcome, 'error');
  assert.ok(result.collection.errors.some((item) => item.code === 'RUNTIME_ERROR'));
});

test('malformed observation JSON is an attempted-source error', async () => {
  const steps = completeSteps();
  steps[4].observation = '[{broken';
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'error');
  assert.deepEqual(result.collection.missing_documents, [{ document_id: 'draft-contract', document_version: 'v1' }]);
});

test('JSON content text is accepted when structuredContent is absent', async () => {
  const steps = completeSteps();
  steps[4].observation = JSON.stringify({ content: [{ type: 'text', text: JSON.stringify(contract) }] });
  const result = await run(steps);
  assert.equal(result.collection.outcome, 'complete');
  assert.deepEqual(result.collection.sources.documents[1], contract);
});

test('structuredContent wins over a conflicting text summary', async () => {
  const steps = completeSteps();
  steps[4].observation = [{ structuredContent: contract, response: [{ type: 'text', text: JSON.stringify({ ...contract, document_id: 'invented' }) }] }];
  assert.equal((await run(steps)).collection.outcome, 'complete');
});

for (const [name, entries] of [
  ['duplicate identity', [...inventory.documents, { ...inventory.documents[1] }]],
  ['conflicting duplicate identity', [...inventory.documents, { ...inventory.documents[1], required: false }]],
  ['missing required flag', [{ document_id: 'technical-spec', document_version: 'v1' }]],
  ['empty inventory', []],
]) {
  test(`${name} makes the manifest invalid`, async () => {
    const steps = completeSteps();
    steps[2] = call('list_tender_documents', { tender_id: 'demo-tender', package_version: 'v1' }, { ...inventory, documents: entries });
    const result = await run(steps);
    assert.equal(result.collection.outcome, 'error');
    assert.equal(result.collection.sources.package, null);
  });
}

test('unsupported or malformed steps are errors instead of green results', async () => {
  for (const extra of [null, { action: { tool: 'invented_tool' }, observation: {} }]) {
    const result = await run([...completeSteps(), extra]);
    assert.equal(result.collection.outcome, 'error');
    assert.ok(result.collection.errors.length > 0);
  }
});

test('absence of intermediateSteps is incomplete instead of a thrown exception', async () => {
  const result = await run(undefined);
  assert.equal(result.collection.outcome, 'incomplete');
});

test('an upstream acknowledgement without an agent result is a technical error', async () => {
  const result = await runInput({ status: 'recorded' });
  assert.equal(result.collection.outcome, 'error');
  assert.ok(result.collection.errors.some((item) => item.code === 'INVALID_AGENT_RESULT'));
  assert.equal(result.telemetry.finish.outcome, 'error');
});

test('a genuine output-only agent result stays incomplete when it contains no tool calls', async () => {
  const result = await runInput({ output: 'Я не вызвал инструменты' });
  assert.equal(result.collection.outcome, 'incomplete');
  assert.equal(result.collection.errors.some((item) => item.code === 'INVALID_AGENT_RESULT'), false);
});
