
// Paste into an n8n Code node configured as Run Once for All Items.
// Tool observations are evidence; the agent's final prose is never evidence.
const input = $input.first().json;
const context = $('Collection context').first().json;
const expected = context.expected;
const sourceNames = ['get_evaluation_policy', 'get_company_profile', 'list_tender_documents', 'read_tender_document'];
const sources = { policy: null, company: null, package: null, documents: [] };
const diagnostics = [];
const attempted = new Set();
const sourceFailures = new Map();
const reads = new Map();

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nonempty(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function identity(value) {
  return JSON.stringify([value.document_id, value.document_version]);
}

function failure(code, detail) {
  return { code, detail };
}

function reject(code, detail) {
  throw failure(code, detail);
}

function toolErrorDetail(value) {
  if (typeof value === 'string') return value.slice(0, 300);
  if (Array.isArray(value)) return value.map(toolErrorDetail).filter(Boolean).join('; ').slice(0, 300);
  if (!object(value)) return '';
  return toolErrorDetail(value.text ?? value.message ?? value.response ?? value.content);
}

function observation(value) {
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      reject('INVALID_OBSERVATION', 'Tool observation is not valid JSON');
    }
  }
  if (Array.isArray(value)) {
    if (value.length !== 1) reject('INVALID_OBSERVATION', 'Expected exactly one tool response');
    return observation(value[0]);
  }
  if (!object(value)) reject('INVALID_OBSERVATION', 'Tool response must be an object');
  if (value.isError === true || value.is_error === true) {
    reject('TOOL_ERROR', toolErrorDetail(value) || 'Tool response explicitly reports a failure');
  }
  if (Object.hasOwn(value, 'structuredContent')) {
    if (!object(value.structuredContent)) reject('INVALID_OBSERVATION', 'structuredContent must be an object');
    return observation(value.structuredContent);
  }
  const wrapped = value.response ?? value.content;
  if (wrapped !== undefined) {
    if (object(wrapped)) return observation(wrapped);
    if (!Array.isArray(wrapped)) reject('INVALID_OBSERVATION', 'Tool content must contain JSON text');
    const texts = wrapped.filter((part) => object(part) && part.type === 'text' && typeof part.text === 'string');
    if (texts.length !== 1) reject('INVALID_OBSERVATION', 'Expected one JSON text content block');
    return observation(texts[0].text);
  }
  return value;
}

function sameFields(value, wanted, fields, code) {
  if (!object(value) || fields.some((field) => value[field] !== wanted[field])) {
    reject(code, `Expected ${fields.map((field) => `${field}=${wanted[field]}`).join(', ')}`);
  }
}

function validateInventory(value) {
  if (!Array.isArray(value.documents) || value.documents.length === 0) {
    reject('INVALID_INVENTORY', 'Package must contain a nonempty document list');
  }
  const seen = new Set();
  for (const document of value.documents) {
    if (!object(document) || !nonempty(document.document_id) || !nonempty(document.document_version)
      || typeof document.required !== 'boolean') {
      reject('INVALID_INVENTORY', 'Each document needs an identity, version and boolean required flag');
    }
    const key = identity(document);
    if (seen.has(key)) reject('INVALID_INVENTORY', `Duplicate document ${document.document_id}/${document.document_version}`);
    seen.add(key);
  }
}

const referenceTools = {
  get_evaluation_policy: { source: 'policy', fields: ['policy_id', 'policy_version'] },
  get_company_profile: { source: 'company', fields: ['company_id', 'profile_version'] },
  list_tender_documents: { source: 'package', fields: ['tender_id', 'package_version'] },
};

if (input.error !== undefined && input.error !== null) {
  diagnostics.push(failure('RUNTIME_ERROR', 'n8n returned an execution error'));
}
if (!Object.hasOwn(input, 'output') && !Object.hasOwn(input, 'intermediateSteps')) {
  diagnostics.push(failure('INVALID_AGENT_RESULT', 'Expected an agent result containing output or intermediateSteps'));
}
if (input.intermediateSteps !== undefined && !Array.isArray(input.intermediateSteps)) {
  diagnostics.push(failure('INVALID_STEPS', 'intermediateSteps must be an array'));
}

for (const step of Array.isArray(input.intermediateSteps) ? input.intermediateSteps : []) {
  if (!object(step) || !object(step.action) || !nonempty(step.action.tool)) {
    diagnostics.push(failure('INVALID_STEP', 'A tool step has no action or tool name'));
    continue;
  }
  const tool = sourceNames.find((name) => step.action.tool === name || step.action.tool.endsWith(`_${name}`));
  if (!tool) {
    diagnostics.push(failure('UNSUPPORTED_TOOL', `Unsupported tool ${step.action.tool}`));
    continue;
  }
  const args = step.action.toolInput;
  if (tool !== 'read_tender_document') {
    const { source, fields } = referenceTools[tool];
    attempted.add(source);
    try {
      sameFields(args, expected, fields, 'INPUT_MISMATCH');
    } catch (error) {
      diagnostics.push(error);
      continue;
    }
    try {
      if (step.action.errored === true) reject('TOOL_ERROR', toolErrorDetail(step.observation) || `${tool} reports an execution failure`);
      const value = observation(step.observation);
      sameFields(value, expected, fields, 'SOURCE_MISMATCH');
      if (source === 'policy' && (typeof value.all_package_documents_required !== 'boolean'
        || !Array.isArray(value.criteria) || value.criteria.length === 0
        || value.criteria.some((criterion) => !object(criterion) || !nonempty(criterion.criterion_id)))) {
        reject('INVALID_POLICY', 'Policy needs its required-document rule and criteria');
      }
      if (source === 'package') validateInventory(value);
      sources[source] = value;
      sourceFailures.delete(source);
    } catch (error) {
      if (!sources[source]) sourceFailures.set(source, error);
    }
    continue;
  }
  if (!object(args) || !nonempty(args.document_id) || !nonempty(args.document_version)) {
    diagnostics.push(failure('INVALID_INPUT', 'Document read needs document_id and document_version'));
    continue;
  }
  const key = identity(args);
  const previous = reads.get(key);
  try {
    if (step.action.errored === true) reject('TOOL_ERROR', toolErrorDetail(step.observation) || `Read failed for ${args.document_id}/${args.document_version}`);
    const value = observation(step.observation);
    sameFields(value, args, ['document_id', 'document_version'], 'DOCUMENT_MISMATCH');
    sameFields(value, expected, ['tender_id', 'package_version'], 'PACKAGE_MISMATCH');
    if (typeof value.text !== 'string' || !Array.isArray(value.sections)) {
      reject('INVALID_DOCUMENT', `Read response lacks text or sections for ${args.document_id}/${args.document_version}`);
    }
    reads.set(key, { document_id: args.document_id, document_version: args.document_version, value });
  } catch (error) {
    if (!previous?.value) reads.set(key, { document_id: args.document_id, document_version: args.document_version, error });
  }
}

let hasError = diagnostics.length > 0;
let missingReference = false;
for (const source of ['policy', 'company', 'package']) {
  if (sources[source]) continue;
  missingReference = true;
  if (attempted.has(source)) {
    hasError = true;
    diagnostics.push(sourceFailures.get(source) ?? failure('INVALID_SOURCE', `No valid ${source} response`));
  } else {
    diagnostics.push(failure('SOURCE_NOT_CALLED', `Required ${source} source was not called`));
  }
}

const inventory = sources.package?.documents ?? [];
const inventoryById = new Map(inventory.map((document) => [identity(document), document]));
const required = inventory.filter((document) => sources.policy?.all_package_documents_required === true || document.required);
const missingDocuments = required.filter((document) => !reads.get(identity(document))?.value)
  .map(({ document_id, document_version }) => ({ document_id, document_version }));

for (const [key, read] of reads) {
  const listed = inventoryById.get(key);
  if (sources.package && !listed) {
    hasError = true;
    diagnostics.push(failure('FOREIGN_DOCUMENT', `Document ${read.document_id}/${read.document_version} is outside the requested package`));
    continue;
  }
  if (read.error && (read.error.code !== 'TOOL_ERROR'
    || sources.policy?.all_package_documents_required !== false || !listed || listed.required)) {
    hasError = true;
    diagnostics.push(read.error);
  }
}

sources.documents = inventory.flatMap((document) => {
  const value = reads.get(identity(document))?.value;
  return value ? [value] : [];
});
const reviewedDocuments = sources.documents.map(({ document_id, document_version }) => ({ document_id, document_version }));
const outcome = hasError ? 'error' : missingReference || missingDocuments.length > 0 ? 'incomplete' : 'complete';
const finishedAt = Date.now() / 1000;

return [{
  json: {
    collection: {
      outcome,
      missing_documents: missingDocuments,
      errors: diagnostics,
      reviewed_documents: reviewedDocuments,
      sources,
    },
    telemetry: {
      start: { workflow: context.workflow, execution_id: context.execution_id, started_at: context.started_at },
      finish: { workflow: context.workflow, execution_id: context.execution_id, finished_at: finishedAt, outcome },
    },
  },
}];
