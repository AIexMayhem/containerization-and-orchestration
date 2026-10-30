// The HTTP node returned an ACK. Restore the checked context from its source node.
const result = $('Validate collection').first().json;
const collection = result.collection;
if (!collection || !['complete', 'incomplete', 'error'].includes(collection.outcome)) {
  throw new Error('Missing or invalid collection validation result');
}
if (collection.outcome !== 'complete') {
  const missing = (collection.missing_documents ?? [])
    .map(({ document_id, document_version }) => `${document_id}/${document_version}`);
  const errors = (collection.errors ?? []).map(({ code, detail }) => `${code}: ${detail}`);
  throw new Error(`Collection ${collection.outcome}: ${[...missing, ...errors].join('; ')}`);
}
return [{ json: result }];
