import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../validate-assessment.js', import.meta.url), 'utf8');
const snapshot = JSON.parse(await readFile(
  new URL('../../reports/context-demo-tender-v1.json', import.meta.url), 'utf8',
));
delete snapshot._provenance;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const validate = new AsyncFunction('$input', '$', source);
const freshContext = () => structuredClone(snapshot);

function assessment(context = freshContext()) {
  const { policy, company, package: manifest, documents } = context.collection.sources;
  return {
    tender_id: manifest.tender_id,
    package_version: manifest.package_version,
    company_id: company.company_id,
    profile_version: company.profile_version,
    policy_id: policy.policy_id,
    policy_version: policy.policy_version,
    reviewed_documents: structuredClone(context.collection.reviewed_documents),
    findings: policy.criteria.map(({ criterion_id }, index) => ({
      criterion_id,
      status: index === 0 ? 'mismatch' : 'needs_clarification',
      reason: index === 0
        ? 'Требуемый режим отличается от режима компании.'
        : 'В проверенных документах нужно уточнить срок реакции.',
      evidence: index === 0
        ? [documents[0].sections[0].source_ref, `${company.company_id}/${company.profile_version}#support_schedule`]
        : documents.map((document) => document.sections[0].source_ref),
      clarification_question: index === 0 ? null : 'Какой срок реакции требуется?',
    })),
    error: null,
  };
}

function response(value) {
  return { done: true, done_reason: 'stop', message: { content: JSON.stringify(value) } };
}

async function run(value = assessment(), context = freshContext(), nativeResponse) {
  return validate(
    { first: () => ({ json: nativeResponse ?? response(value) }) },
    (name) => {
      assert.equal(name, 'Require complete context');
      return { first: () => ({ json: context }) };
    },
  );
}

test('returns only the validated assessment for the Convert to File node', async () => {
  const value = assessment();
  assert.deepEqual(await run(value), [{ json: value }]);
});

test('uses source identities and policy criteria rather than demo constants', async () => {
  const context = freshContext();
  const { policy, company, package: manifest, documents } = context.collection.sources;
  policy.policy_id = 'another-policy';
  policy.policy_version = 'v9';
  policy.criteria[0].criterion_id = 'custom-criterion';
  company.company_id = 'other-company';
  company.profile_version = 'v8';
  manifest.tender_id = 'other-tender';
  manifest.package_version = 'v7';
  for (const document of documents) {
    document.tender_id = manifest.tender_id;
    document.package_version = manifest.package_version;
  }
  const value = assessment(context);
  assert.deepEqual(await run(value, context), [{ json: value }]);
});

test('blocks missing, incomplete, or erroneous trusted collection context', async () => {
  for (const context of [{}, { collection: { outcome: 'incomplete' } }, { collection: { outcome: 'error' } }]) {
    await assert.rejects(run(assessment(), context), /context/i);
  }
});

test('rejects unfinished, truncated, or missing Ollama completions', async () => {
  for (const change of [{ done: false }, { done_reason: 'length' }, { done_reason: undefined }, { message: {} }]) {
    const nativeResponse = { ...response(assessment()), ...change };
    await assert.rejects(run(undefined, freshContext(), nativeResponse), /Ollama/i);
  }
});

test('requires one JSON object with no markdown, trailing text, or array wrapper', async () => {
  for (const content of ['not json', '```json\n{}\n```', '{} {}', '[]', 'null', '"text"', '42']) {
    await assert.rejects(run(undefined, freshContext(), {
      done: true, done_reason: 'stop', message: { content },
    }), /JSON object/i);
  }
});

test('rejects missing or additional top-level fields', async () => {
  const extra = assessment();
  extra.analysis = 'unexpected';
  const missing = assessment();
  delete missing.error;
  for (const value of [extra, missing]) {
    await assert.rejects(run(value), /fields/i);
  }
});

test('checks all six identity fields against trusted sources', async () => {
  for (const field of ['tender_id', 'package_version', 'company_id', 'profile_version', 'policy_id', 'policy_version']) {
    const value = assessment();
    value[field] = 'invented';
    await assert.rejects(run(value), new RegExp(field));
  }
});

test('requires the exact reviewed document set without duplicates or extra fields', async () => {
  const mutations = [
    (value) => { value.reviewed_documents.pop(); },
    (value) => { value.reviewed_documents.push({ ...value.reviewed_documents[0] }); },
    (value) => { value.reviewed_documents[0].document_id = 'unread'; },
    (value) => { value.reviewed_documents[0].document_version = 'v99'; },
    (value) => { value.reviewed_documents[0].text = 'extra'; },
  ];
  for (const mutate of mutations) {
    const value = assessment();
    mutate(value);
    await assert.rejects(run(value), /reviewed_documents/i);
  }
  const reordered = assessment();
  reordered.reviewed_documents.reverse();
  assert.deepEqual(await run(reordered), [{ json: reordered }]);
});

test('requires exactly one finding for each criterion in the policy', async () => {
  const mutations = [
    (value) => { value.findings.pop(); },
    (value) => { value.findings.push({ ...value.findings[0] }); },
    (value) => { value.findings[0].criterion_id = 'invented'; },
  ];
  for (const mutate of mutations) {
    const value = assessment();
    mutate(value);
    await assert.rejects(run(value), /findings|criterion/i);
  }
});

test('requires the exact finding fields, a nonempty reason, and string evidence', async () => {
  const mutations = [
    (value) => { value.findings[0].confidence = 1; },
    (value) => { delete value.findings[0].evidence; },
    (value) => { value.findings[0].reason = '   '; },
    (value) => { value.findings[0].evidence = 'not an array'; },
    (value) => { value.findings[0].evidence = [42]; },
  ];
  for (const mutate of mutations) {
    const value = assessment();
    mutate(value);
    await assert.rejects(run(value), /finding|evidence|reason/i);
  }
});

test('accepts all contract statuses without claiming to prove the model reasoning', async () => {
  const value = assessment();
  value.findings[0].status = 'match';
  assert.deepEqual(await run(value), [{ json: value }]);
  value.findings[0].status = 'probably';
  await assert.rejects(run(value), /status/i);
});

test('rejects an unsupported clarification finding with no evidence', async () => {
  const value = assessment();
  value.findings[1].evidence = [];
  await assert.rejects(run(value), /evidence/i);
});

test('rejects invented document sections, versions, and unprovided references', async () => {
  for (const reference of ['unknown/v1#TS-1', 'technical-spec/v2#TS-1', 'technical-spec/v1#invented']) {
    const value = assessment();
    value.findings[0].evidence[0] = reference;
    await assert.rejects(run(value), /evidence/i);
  }
});

test('accepts existing company objects, nested fields, and array positions', async () => {
  for (const path of ['support_schedule', 'support_schedule.start', 'support_schedule.days.0', 'support_schedule.outside_schedule_available']) {
    const value = assessment();
    value.findings[0].evidence[1] = `${value.company_id}/${value.profile_version}#${path}`;
    assert.deepEqual(await run(value), [{ json: value }]);
  }
});

test('rejects forged company identities, absent fields, and prototype paths', async () => {
  const base = assessment();
  const prefix = `${base.company_id}/${base.profile_version}#`;
  for (const reference of [
    'other/v1#support_schedule', `${base.company_id}/v99#support_schedule`,
    prefix, prefix + 'support_schedule.response_time', prefix + 'support_schedule.',
    prefix + '__proto__', prefix + 'constructor', prefix + 'support_schedule.days.99',
  ]) {
    const value = assessment();
    value.findings[0].evidence[1] = reference;
    await assert.rejects(run(value), /evidence/i);
  }
});

test('requires both document and company evidence for match and mismatch', async () => {
  for (const status of ['match', 'mismatch']) {
    for (const indices of [[], [0], [1]]) {
      const value = assessment();
      value.findings[0].status = status;
      value.findings[0].evidence = indices.map((index) => value.findings[0].evidence[index]);
      await assert.rejects(run(value), /evidence/i);
    }
  }
});

test('requires a question only for needs_clarification', async () => {
  for (const question of [null, '', '   ', 42]) {
    const value = assessment();
    value.findings[1].clarification_question = question;
    await assert.rejects(run(value), /clarification_question/i);
  }
  const value = assessment();
  value.findings[0].clarification_question = 'Unexpected question';
  await assert.rejects(run(value), /clarification_question/i);
});

test('rejects an error result instead of saving it as a completed assessment', async () => {
  const value = assessment();
  value.error = 'Some source failed';
  value.findings = [];
  await assert.rejects(run(value), /error/i);
});

test('fails closed for malformed trusted inventories even if outcome says complete', async () => {
  const contexts = [
    (context) => { context.collection.sources.documents.pop(); },
    (context) => { context.collection.sources.policy.criteria[1].criterion_id = context.collection.sources.policy.criteria[0].criterion_id; },
    (context) => { context.collection.sources.documents[0].package_version = 'wrong'; },
    (context) => { context.collection.reviewed_documents.push({ ...context.collection.reviewed_documents[0] }); },
  ];
  for (const mutate of contexts) {
    const context = freshContext();
    mutate(context);
    await assert.rejects(run(assessment(), context), /context/i);
  }
});
