import { readFileSync, statSync } from "node:fs";

function argument(name: string, fallback?: string): string {
  const index = Bun.argv.indexOf(name);
  const value = index >= 0 ? Bun.argv[index + 1] : fallback;
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const authFile = argument("--auth-file");
const requestedPort = Number(argument("--port", "0"));
if (!Number.isInteger(requestedPort) || requestedPort < 0 || requestedPort > 65535) {
  throw new Error("--port must be an integer from 0 to 65535");
}
if ((statSync(authFile).mode & 0o077) !== 0) {
  throw new Error("auth file must not be accessible by group or other users");
}
const token = JSON.parse(readFileSync(authFile, "utf8")).token;
if (typeof token !== "string" || token.length < 16) throw new Error("auth file has no usable token");

process.env.OPENCODE_CLAUDE_PROXY_PORT = "0";
const upstream = await import("./node_modules/@openchamber/opencode-claude/dist/index.js");
await upstream.startProxy();
const upstreamUrl = upstream.getClaudeProxyBaseUrl();

const server = Bun.serve({
  hostname: "127.0.0.1",
  port: requestedPort,
  idleTimeout: 0,
  async fetch(request) {
    const authorized = request.headers.get("authorization") === `Bearer ${token}`
      || request.headers.get("x-api-key") === token;
    if (!authorized) {
      return Response.json({ error: { type: "authentication_error", message: "authentication required" } }, { status: 401 });
    }
    const incoming = new URL(request.url);
    const target = upstreamUrl + incoming.pathname.replace(/^\/v1/, "") + incoming.search;
    const headers = new Headers(request.headers);
    headers.delete("authorization");
    headers.delete("x-api-key");
    let body: BodyInit | undefined = request.method === "GET" || request.method === "HEAD" ? undefined : request.body;
    if (request.method === "POST" && incoming.pathname.endsWith("/chat/completions")) {
      // DSH sends OpenAI reasoning_effort; the bridge only reads its own base64url selection header.
      const text = await request.text();
      body = text;
      try {
        const parsed = JSON.parse(text);
        if (typeof parsed.reasoning_effort === "string" && typeof parsed.model === "string") {
          headers.set("x-opencode-claude-effort", Buffer.from(JSON.stringify({ modelId: parsed.model, effort: parsed.reasoning_effort })).toString("base64url"));
        }
      } catch {}
    }
    return fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
    });
  },
});

console.log(JSON.stringify({ url: `http://127.0.0.1:${server.port}/v1`, pid: process.pid }));

const stop = async () => {
  server.stop(true);
  await upstream.stopProxy();
  process.exit(0);
};
process.once("SIGINT", stop);
process.once("SIGTERM", stop);
await new Promise(() => {});
