// OpenTelemetry bootstrap for any Node.js app, loaded without code changes:
//
//   NODE_OPTIONS="--import /opt/webkit/otel/register.mjs"
//
// - continues the traceparent that wk-inbound puts on every request;
// - writes that traceparent into every SQL statement as a sqlcommenter comment,
//   which is how wk-pgproxy attributes each statement to its request;
// - exports spans over OTLP/HTTP to the collector (OTEL_EXPORTER_OTLP_ENDPOINT).
//
// Set WK_OTEL_DISABLE=1 to load nothing.
import { register } from "node:module";

if (process.env.WK_OTEL_DISABLE !== "1") {
  // ESM apps need the loader hook registered before their modules load.
  register("@opentelemetry/instrumentation/hook.mjs", import.meta.url);

  const { NodeSDK } = await import("@opentelemetry/sdk-node");
  const { OTLPTraceExporter } = await import("@opentelemetry/exporter-trace-otlp-http");
  const { HttpInstrumentation } = await import("@opentelemetry/instrumentation-http");
  const { PgInstrumentation } = await import("@opentelemetry/instrumentation-pg");

  const sdk = new NodeSDK({
    serviceName: process.env.OTEL_SERVICE_NAME ?? "app",
    traceExporter: new OTLPTraceExporter(),
    instrumentations: [
      new HttpInstrumentation(),
      new PgInstrumentation({ addSqlCommenterCommentToQueries: true }),
    ],
  });
  sdk.start();

  const shutdown = () => sdk.shutdown().catch(() => {});
  process.once("beforeExit", shutdown);
}
