// Validate the JSON contract and source references. This cannot prove LLM reasoning.
function fail(detail) {
  throw new Error(`Assessment validation: ${detail}`);
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isText(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function requireFields(value, fields, label) {
  if (!isObject(value) || Object.keys(value).length !== fields.length
    || fields.some((field) => !Object.hasOwn(value, field))) {
    fail(`${label} fields do not match the contract`);
  }
}

function documentSet(documents, label, exactFields = false) {
  if (!Array.isArray(documents)) fail(`${label} must be an array`);
  const result = new Set();
  for (const document of documents) {
    if (exactFields) requireFields(document, ['document_id', 'document_version'], label);
    if (!isObject(document) || !isText(document.document_id) || !isText(document.document_version)) {
      fail(`${label} contains an invalid document identity`);
    }
    const key = JSON.stringify([document.document_id, document.document_version]);
    if (result.has(key)) fail(`${label} contains a duplicate document`);
    result.add(key);
  }
  return result;
}

function sameSet(actual, expected) {
  return actual.size === expected.size && [...actual].every((key) => expected.has(key));
}

const context = $('Require complete context').first().json;
const collection = context?.collection;
if (!collection || collection.outcome !== 'complete'
  || !Array.isArray(collection.missing_documents) || collection.missing_documents.length !== 0
  || !Array.isArray(collection.errors) || collection.errors.length !== 0) {
  fail('trusted context is not a complete collection');
}

const sources = collection.sources;
if (!isObject(sources) || !isObject(sources.policy) || !isObject(sources.company)
  || !isObject(sources.package)) {
  fail('trusted context is missing sources');
}
const { policy, company, package: manifest, documents } = sources;
const identities = {
  tender_id: manifest.tender_id,
  package_version: manifest.package_version,
  company_id: company.company_id,
  profile_version: company.profile_version,
  policy_id: policy.policy_id,
  policy_version: policy.policy_version,
};
if (Object.values(identities).some((value) => !isText(value))) {
  fail('trusted context has invalid source identities');
}

const readDocuments = documentSet(documents, 'trusted context documents');
const inventory = documentSet(manifest.documents, 'trusted context manifest');
const reviewed = documentSet(collection.reviewed_documents, 'trusted context reviewed_documents', true);
if (!sameSet(readDocuments, reviewed) || [...readDocuments].some((key) => !inventory.has(key))) {
  fail('trusted context document inventories disagree');
}
for (const document of manifest.documents) {
  if ((policy.all_package_documents_required === true || document.required === true)
    && !readDocuments.has(JSON.stringify([document.document_id, document.document_version]))) {
    fail('trusted context is missing a required document');
  }
}

const documentEvidence = new Set();
for (const document of documents) {
  if (document.tender_id !== manifest.tender_id || document.package_version !== manifest.package_version
    || !Array.isArray(document.sections)) {
    fail('trusted context contains a foreign or malformed document');
  }
  for (const section of document.sections) {
    if (!isObject(section) || !isText(section.source_ref) || documentEvidence.has(section.source_ref)) {
      fail('trusted context contains invalid or duplicate section references');
    }
    documentEvidence.add(section.source_ref);
  }
}

if (!Array.isArray(policy.criteria)) fail('trusted context policy criteria must be an array');
const expectedCriteria = new Set();
for (const criterion of policy.criteria) {
  if (!isObject(criterion) || !isText(criterion.criterion_id) || expectedCriteria.has(criterion.criterion_id)) {
    fail('trusted context policy contains invalid or duplicate criteria');
  }
  expectedCriteria.add(criterion.criterion_id);
}

const response = $input.first().json;
if (!isObject(response) || response.done !== true || response.done_reason !== 'stop'
  || !isObject(response.message) || !isText(response.message.content)) {
  fail('Ollama response is missing, unfinished, or truncated');
}
let assessment;
try {
  assessment = JSON.parse(response.message.content);
} catch {
  fail('response must contain one JSON object');
}
if (!isObject(assessment)) fail('response must contain one JSON object');
requireFields(assessment, [
  ...Object.keys(identities), 'reviewed_documents', 'findings', 'error',
], 'result');
for (const [field, expected] of Object.entries(identities)) {
  if (assessment[field] !== expected) fail(`${field} does not match the trusted source`);
}
if (assessment.error !== null) fail('error must be null for a completed assessment');
if (!sameSet(documentSet(assessment.reviewed_documents, 'reviewed_documents', true), reviewed)) {
  fail('reviewed_documents does not match the read document set');
}
if (!Array.isArray(assessment.findings) || assessment.findings.length !== expectedCriteria.size) {
  fail('findings must cover every policy criterion exactly once');
}

const companyPrefix = `${company.company_id}/${company.profile_version}#`;
function isCompanyEvidence(reference) {
  if (!reference.startsWith(companyPrefix)) return false;
  const path = reference.slice(companyPrefix.length).split('.');
  let value = company;
  for (const field of path) {
    if (!field || ['__proto__', 'constructor', 'prototype'].includes(field)
      || value === null || typeof value !== 'object'
      || (Array.isArray(value) && !/^(0|[1-9][0-9]*)$/.test(field))
      || !Object.hasOwn(value, field)) return false;
    value = value[field];
  }
  return true;
}

const foundCriteria = new Set();
for (const finding of assessment.findings) {
  requireFields(finding, [
    'criterion_id', 'status', 'reason', 'evidence', 'clarification_question',
  ], 'finding');
  if (!expectedCriteria.has(finding.criterion_id) || foundCriteria.has(finding.criterion_id)) {
    fail('finding criterion is unknown or duplicated');
  }
  foundCriteria.add(finding.criterion_id);
  if (!['match', 'mismatch', 'needs_clarification'].includes(finding.status)) {
    fail('finding status is invalid');
  }
  if (!isText(finding.reason)) fail('finding reason must be nonempty');
  if (!Array.isArray(finding.evidence) || finding.evidence.length === 0
    || finding.evidence.some((reference) => !isText(reference))) {
    fail('finding evidence must be a nonempty array of nonempty strings');
  }
  let hasDocument = false;
  let hasCompany = false;
  for (const reference of finding.evidence) {
    if (documentEvidence.has(reference)) hasDocument = true;
    else if (isCompanyEvidence(reference)) hasCompany = true;
    else fail(`finding evidence does not exist in the trusted context: ${reference}`);
  }
  if (finding.status === 'needs_clarification') {
    if (!isText(finding.clarification_question)) {
      fail('clarification_question must be nonempty for needs_clarification');
    }
  } else {
    if (finding.clarification_question !== null) {
      fail('clarification_question must be null for match and mismatch');
    }
    if (!hasDocument || !hasCompany) fail('comparison evidence must reference a document and the company');
  }
}

return [{ json: assessment }];
