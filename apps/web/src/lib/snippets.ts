/**
 * Integration snippets shared by the landing page, the docs and the dashboard empty states.
 * They follow the contracts in docs/ARCHITECTURE.md and infra/otel/collector.yaml. Endpoints ship
 * in the milestone noted next to each snippet; field names may still change until then.
 */

/** Placeholder used on public pages, where there is no single API URL to show. */
export const API_URL_PLACEHOLDER = "$RELEASELENS_API_URL";

/** POST /api/v1/deployments (M6). */
export function deploymentCurl(apiUrl: string = API_URL_PLACEHOLDER): string {
  return `curl -X POST "${apiUrl}/api/v1/deployments" \\
  -H "x-releaselens-key: $RELEASELENS_API_KEY" \\
  -H "content-type: application/json" \\
  -d '{
    "service": "payment",
    "environment": "production",
    "version": "v2.4.0",
    "commit_sha": "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "deployed_at": "2026-10-13T09:30:00Z"
  }'

# 202 Accepted -> {"event_id": "..."}`;
}

/** The composite GitHub Action (M5). */
export const GITHUB_ACTION_YAML = `name: ReleaseLens risk check
on: pull_request

jobs:
  risk:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0   # change facts need the base branch history
      - uses: ayushman46/releaselens/integrations/github-action@main
        with:
          api-key: \${{ secrets.RELEASELENS_API_KEY }}`;

/** OpenTelemetry Collector exporter (M7). Mirrors infra/otel/collector.yaml. */
export function otelCollectorYaml(apiUrl: string = API_URL_PLACEHOLDER): string {
  const endpoint = apiUrl.startsWith("$") ? `\${env:RELEASELENS_API_URL}` : apiUrl;
  return `# Excerpt: add the exporter and list it in your traces pipeline.
exporters:
  otlphttp/releaselens:
    endpoint: ${endpoint}   # the exporter appends /v1/traces
    encoding: proto
    compression: gzip
    headers:
      x-releaselens-key: \${env:RELEASELENS_API_KEY}

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/releaselens]`;
}

/** SDK-only setup through the standard OTel environment variables (M7). */
export function otelEnv(apiUrl: string = API_URL_PLACEHOLDER): string {
  return `export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="${apiUrl}/v1/traces"
export OTEL_EXPORTER_OTLP_HEADERS="x-releaselens-key=$RELEASELENS_API_KEY"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_SERVICE_NAME="payment"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=production"`;
}
