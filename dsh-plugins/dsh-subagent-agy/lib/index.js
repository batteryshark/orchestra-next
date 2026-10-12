/**
 * One-shot DSH subagent provider over Google's official Antigravity CLI.
 *
 * Each run spawns `agy --print <prompt> --output-format json` in the parent
 * session's workspace and returns the CLI's final response. The CLI owns its
 * login in the OS keyring; this plugin never reads, copies, or refreshes any
 * credential and never calls Antigravity endpoints itself. GEMINI_API_KEY and
 * GOOGLE_API_KEY are removed from the child environment so a subscription
 * route can never silently become API billing.
 *
 * Modes:
 *   review       --sandbox, read-only; edits are auto-denied (default)
 *   accept-edits --mode accept-edits; file edits allowed, other tools denied
 *   full         --dangerously-skip-permissions; everything auto-approved
 *
 * @module @orchestra-next/dsh-subagent-agy
 */
import { randomUUID } from "node:crypto";
import z from "@deepseek-ai/schemastery";
import { NO_START_CAPABILITIES, assertPositiveFinite, resolveChildCwd, settleRunResult, subprocessRunHandle } from "@deepseek-ai/dsh-subagent";

const name = "subagent-agy";
const inject = ["subagents", "subprocess"];
const MODES = ["review", "accept-edits", "full"];
const SCRUBBED_ENV = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"];
const DIAGNOSTIC_LIMIT = 4000;

const Config = z.object({
  providerName: z.string().min(1).default("agy"),
  executable: z.string().min(1).default("agy"),
  model: z.string().min(1),
  effort: z.union(["low", "medium", "high", "xhigh", "max"]),
  mode: z.union(MODES).default("review"),
  timeoutSeconds: z.number().default(600),
  disposeGraceMs: z.number().default(3000),
  env: z.dict(z.string()).default({}),
});

function promptText(blocks) {
  return (blocks ?? [])
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n\n")
    .trim();
}

function buildArgv(config, prompt, cwd) {
  const argv = [config.executable, "--print", prompt, "--output-format", "json",
    "--disable-slash-commands", "--print-timeout", `${Math.ceil(config.timeoutSeconds)}s`,
    "--add-dir", cwd];
  if (config.model) argv.push("--model", config.model);
  if (config.effort) argv.push("--effort", config.effort);
  if (config.mode === "review") argv.push("--sandbox");
  else if (config.mode === "accept-edits") argv.push("--mode", "accept-edits");
  else if (config.mode === "full") argv.push("--dangerously-skip-permissions");
  return argv;
}

/** The last stdout line that parses as a JSON object (agy prints progress first). */
function parseResult(stdout) {
  const lines = (stdout ?? "").split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    try {
      const value = JSON.parse(line);
      if (value && typeof value === "object" && !Array.isArray(value)) return value;
    } catch {}
  }
  return undefined;
}

function clip(text) {
  const value = String(text ?? "").trim();
  return value.length > DIAGNOSTIC_LIMIT ? value.slice(-DIAGNOSTIC_LIMIT) : value;
}

class AgyProvider {
  capabilities = NO_START_CAPABILITIES;
  inheritsParentContext = false;

  constructor(providerName, ctx, config) {
    this.name = providerName;
    this.ctx = ctx;
    this.config = config;
  }

  async start(request) {
    const parentCwd = request.parent.session.header.cwd;
    if (parentCwd === undefined) throw new Error("subagent-agy: no working directory for the child — delegate from a parent session that has one");
    const cwd = resolveChildCwd("subagent-agy", undefined, parentCwd);
    const prompt = promptText(request.prompt);
    if (!prompt) throw new Error("subagent-agy: the delegation prompt has no text");
    request.signal.throwIfAborted();

    const env = { ...this.config.env };
    for (const key of SCRUBBED_ENV) env[key] = undefined; // tombstone: removed from the child
    const child = this.ctx.subprocess.spawn({
      argv: buildArgv(this.config, prompt, cwd),
      cwd,
      stdio: { stdin: "ignore", stdout: { maxBytes: 4 * 1024 * 1024 }, stderr: { maxBytes: 64 * 1024 } },
      graceMs: this.config.disposeGraceMs,
      signal: request.signal,
      env,
    });

    let cancelled = false;
    let output = [];
    let diagnostic;
    const requestCancel = () => {
      if (cancelled) return;
      cancelled = true;
      child.terminate();
    };
    const onAbort = () => requestCancel();
    request.signal.addEventListener("abort", onAbort, { once: true });

    const teardown = async () => {
      child.terminate();
      try {
        await child.waitForExit(AbortSignal.timeout(Math.ceil(this.config.disposeGraceMs) + 1000));
      } catch {}
    };

    const attempt = async () => {
      const exit = await child.done;
      const stdout = child.collected.stdout?.readFrom(0).text ?? "";
      const stderr = child.collected.stderr?.readFrom(0).text ?? "";
      const parsed = parseResult(stdout);
      const response = typeof parsed?.response === "string" ? parsed.response : "";
      const status = typeof parsed?.status === "string" ? parsed.status : undefined;
      if (status === "SUCCESS" && response.trim()) {
        output = [{ type: "text", text: response }];
        return { output, stopReason: "completed" };
      }
      if (status === "SUCCESS") {
        diagnostic = clip(stderr.includes("auto-denied")
          ? `agy auto-denied a tool it could not prompt for (mode=${this.config.mode}): ${stderr.trim().split("\n").pop()}`
          : "agy reported success but returned no response text");
      } else if (parsed === undefined) {
        diagnostic = clip(`agy exited ${exit.exitCode ?? exit.signal} without a JSON result. ${stderr}`);
      } else {
        diagnostic = clip(`agy status ${status ?? "unknown"}: ${parsed.error ?? stderr}`);
      }
      return { output, diagnostic, stopReason: "error" };
    };

    const result = settleRunResult({
      attempt,
      collectOutput: () => output,
      collectDiagnostic: () => diagnostic,
      cancelled: () => cancelled,
      onError: (error, stopReason) => this.ctx.logger.warn(`subagent-agy "${this.name}": child run failed (${stopReason}): ${error.message}`),
      signal: request.signal,
      onAbort,
    });
    return subprocessRunHandle({ id: randomUUID(), result, signal: request.signal, onAbort, requestCancel, teardown });
  }
}

/** Register one profile-named agy provider. */
function apply(ctx, config) {
  const resolved = {
    providerName: config.providerName ?? "agy",
    executable: config.executable ?? "agy",
    model: config.model,
    effort: config.effort,
    mode: config.mode ?? "review",
    timeoutSeconds: config.timeoutSeconds ?? 600,
    disposeGraceMs: config.disposeGraceMs ?? 3000,
    env: config.env ?? {},
  };
  if (!MODES.includes(resolved.mode)) throw new Error(`subagent-agy: mode must be one of ${MODES.join(", ")}`);
  assertPositiveFinite("subagent-agy", "timeoutSeconds", resolved.timeoutSeconds);
  assertPositiveFinite("subagent-agy", "disposeGraceMs", resolved.disposeGraceMs);
  ctx.subagents.registerProvider(new AgyProvider(resolved.providerName, ctx, resolved));
}

export { Config, apply, buildArgv, inject, name, parseResult, promptText };
