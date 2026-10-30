// Run once before the retryable start request. Retries must reuse this metadata.
const rawId = $execution.id;
if (!['string', 'number'].includes(typeof rawId)) {
  throw new Error('Missing n8n execution identity');
}
const executionId = String(rawId);
if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(executionId) || /\s/u.test(executionId)) {
  throw new Error('Invalid n8n execution identity');
}

return [{
  json: {
    workflow: 'tender-precheck',
    execution_id: executionId,
    started_at: Date.now() / 1000,
    expected: {
      tender_id: 'demo-tender',
      package_version: 'v1',
      company_id: 'demo-integrator',
      profile_version: 'v1',
      policy_id: 'tender-precheck',
      policy_version: 'v1',
    },
  },
}];
