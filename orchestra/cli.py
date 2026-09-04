"""Orchestra-next operator and worker bridge CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from orchestra import auth, claude, client, config, daemon, db, dsh, paths, service, supervise

API = "/api"


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _client(args):
    return client.Client(getattr(args, "url", None))


def _run_id(args) -> int:
    value = getattr(args, "run_id", None) or os.environ.get("ORCHESTRA_NEXT_RUN_ID")
    if not value:
        raise SystemExit("orchestra-next: --run is required outside a worker run")
    return int(value)


def cmd_init(args):
    config.ensure()
    con = db.connect()
    try:
        if con.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
            raise SystemExit("orchestra-next: already initialized; pair another client instead")
        device, token = auth.bootstrap_device(con, args.name)
        client.save_token(config.api_url(), token)
        _print({"state": str(paths.state_dir()), "url": config.api_url(), "device": device})
    finally:
        con.close()


def cmd_dsh_setup(args):
    path, changed = dsh.setup_profile()
    result = {"path": str(path), "changed": changed}
    if not args.no_check:
        result.update(dsh.check_profile())
    _print(result)


def cmd_dsh_check(args):
    result = dsh.check_profile()
    if args.capabilities:
        catalog = dsh.catalog(os.getcwd())
        result["models"] = [{"provider": provider, "model": model, "efforts": sorted(efforts)} for (provider, model), efforts in catalog.items()]
    _print(result)


def cmd_claude_setup(args):
    path, changed = claude.setup()
    result = {"path": str(path), "changed": changed}
    if not args.no_check:
        result.update(claude.check())
    _print(result)


def cmd_claude_check(args):
    _print(claude.check())


def cmd_daemon(args):
    return daemon.run(args.interval, once=args.once, preflight=not args.no_preflight)


def cmd_supervise(args):
    return supervise.supervise(args.run_id)


def cmd_run(args):
    verify = {"argv": args.verify, "timeout_seconds": args.verify_timeout} if args.verify else None
    body = {"request_id": args.request_id or str(uuid.uuid4()), "profile": args.profile,
            "objective": args.objective, "group": args.group, "strategy": args.strategy,
            "permission_mode": args.permission_mode, "title": args.title, "cwd": args.cwd,
            "ref": args.ref, "requested_by": "operator",
            "limits": {"max_rounds": args.max_rounds, "active_seconds": args.active_seconds},
            "verify": verify, "max_children": args.max_children,
            "max_child_tier": args.max_child_tier, "allow_antigravity": args.allow_antigravity}
    _print(_client(args).post(API + "/runs", body))


def cmd_runs(args):
    _print(_client(args).get(API + "/runs", after=args.after))


def cmd_show(args):
    _print(_client(args).get(f"{API}/runs/{args.run_id}"))


def cmd_control(args):
    run_id = _run_id(args)
    body = {"message": getattr(args, "message", None), "reason": getattr(args, "reason", None)}
    _print(_client(args).post(f"{API}/runs/{run_id}/{args.command}", body))


def cmd_reroute(args):
    _print(_client(args).post(f"{API}/runs/{_run_id(args)}/reroute", {"provider": args.provider, "model": args.model, "effort": args.effort, "message": args.message}))


def cmd_retry(args):
    endpoint = "continue" if args.command == "continue" else "retry"
    _print(_client(args).post(f"{API}/runs/{args.run_id}/{endpoint}", {"request_id": args.request_id or str(uuid.uuid4()), "direction": getattr(args, "direction", None)}))


def cmd_profiles_import_v2(args):
    from orchestra import db, profiles
    catalog, _ = dsh.cached_catalog(os.getcwd())
    con = db.connect()
    try:
        _print(profiles.import_v2(con, args.v2_db, catalog=catalog, apply=args.apply))
    finally:
        con.close()


def cmd_profile_create(args):
    _print(_client(args).post(API + "/profiles", {"name": args.name, "slug": args.slug, "provider": args.provider, "model": args.model, "effort": args.effort, "tier": args.tier, "max_concurrency": args.max_concurrency, "note": args.note}))


def cmd_list(args):
    _print(_client(args).get(API + "/" + args.command))


def cmd_group_create(args):
    _print(_client(args).post(API + "/groups", {"name": args.name, "slug": args.slug, "cwd": args.cwd}))


def cmd_ask(args):
    item = _client(args).post(f"{API}/runs/{_run_id(args)}/attention", {"kind": args.kind, "prompt": args.prompt, "context": {}})
    _print(item)


def cmd_child(args):
    body = {"request_id": args.request_id or str(uuid.uuid4()), "profile": args.profile, "objective": args.objective, "strategy": args.strategy, "limits": {"max_rounds": args.max_rounds}}
    _print(_client(args).post(f"{API}/runs/{_run_id(args)}/children", body))


def cmd_artifact(args):
    _print(_client(args).post(f"{API}/runs/{_run_id(args)}/artifacts", {"path": args.path, "name": args.name}))


def cmd_delegate(args):
    # The daemon runs agy (the worker's shell is sandboxed); this call blocks until the review returns.
    run_id = _run_id(args)
    api = client.Client(getattr(args, "url", None), timeout=720)
    try:
        record = api.post(f"{API}/runs/{run_id}/delegate", {"objective": args.objective, "model": args.model, "mode": args.mode})["data"]
    except client.ClientError as exc:
        # A failed review still answers 502 with the recorded delegation in the envelope.
        data = exc.payload.get("data") if isinstance(exc.payload, dict) else None
        if not isinstance(data, dict) or "status" not in data:
            raise
        record = data
    print(record["response"] if record.get("response") is not None else "")
    print(json.dumps({"delegation": {"model": record["model"], "conversation_id": record["conversation_id"], "status": record["status"], "tokens": record["delta"]["total"]}}), file=sys.stderr)
    return 0 if record["status"] == "SUCCESS" else 1


def cmd_attention(args):
    if args.action == "list":
        _print(_client(args).get(API + "/attention", status=args.status))
    elif args.action == "lease":
        _print(_client(args).post(f"{API}/attention/{args.attention_id}/lease", {"seconds": args.seconds}))
    else:
        _print(_client(args).post(f"{API}/attention/{args.attention_id}/answer", {"answer": args.answer, "lease_id": args.lease_id}))


def cmd_pair(args):
    _print(_client(args).post(API + "/auth/pair", {}))


def cmd_service(args):
    _print(_client(args).post(API + "/auth/service-tokens", {"name": args.name, "authorities": args.authorities}))


def build_parser():
    parser = argparse.ArgumentParser(prog="orchestra-next", description="Durable DSH-only agent execution")
    parser.add_argument("--url")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--name", default="Local operator"); init.set_defaults(func=cmd_init)
    dsh_parser = sub.add_parser("dsh"); dsh_sub = dsh_parser.add_subparsers(dest="dsh_command", required=True)
    setup = dsh_sub.add_parser("setup"); setup.add_argument("--no-check", action="store_true"); setup.set_defaults(func=cmd_dsh_setup)
    check = dsh_sub.add_parser("check"); check.add_argument("--capabilities", action="store_true"); check.set_defaults(func=cmd_dsh_check)
    claude_parser = sub.add_parser("claude"); claude_sub = claude_parser.add_subparsers(dest="claude_command", required=True)
    claude_setup = claude_sub.add_parser("setup"); claude_setup.add_argument("--no-check", action="store_true"); claude_setup.set_defaults(func=cmd_claude_setup)
    claude_check = claude_sub.add_parser("check"); claude_check.set_defaults(func=cmd_claude_check)
    serve = sub.add_parser("daemon"); serve.add_argument("--interval", type=float, default=1); serve.add_argument("--once", action="store_true"); serve.add_argument("--no-preflight", action="store_true", help=argparse.SUPPRESS); serve.set_defaults(func=cmd_daemon)
    internal = sub.add_parser("supervise", help="internal per-run supervisor"); internal.add_argument("run_id", type=int); internal.set_defaults(func=cmd_supervise)
    run = sub.add_parser("run"); run.add_argument("profile"); run.add_argument("objective"); run.add_argument("--request-id"); run.add_argument("--group", default="general"); run.add_argument("--strategy", choices=("goal", "ralph"), default="goal"); run.add_argument("--permission-mode", choices=("read-only", "workspace-write", "danger-full-access"), default="workspace-write"); run.add_argument("--title"); run.add_argument("--cwd"); run.add_argument("--ref"); run.add_argument("--max-rounds", type=int); run.add_argument("--active-seconds", type=int); run.add_argument("--verify", nargs="+"); run.add_argument("--verify-timeout", type=int, default=600); run.add_argument("--max-children", type=int); run.add_argument("--max-child-tier", type=int); run.add_argument("--allow-antigravity", action="store_true"); run.set_defaults(func=cmd_run)
    listing = sub.add_parser("runs"); listing.add_argument("--after", type=int, default=0); listing.set_defaults(func=cmd_runs)
    show = sub.add_parser("show"); show.add_argument("run_id", type=int); show.set_defaults(func=cmd_show)
    for name in ("tell", "interrupt"):
        item = sub.add_parser(name); item.add_argument("message"); item.add_argument("--run", dest="run_id", type=int); item.set_defaults(func=cmd_control)
    stop = sub.add_parser("stop"); stop.add_argument("--run", dest="run_id", type=int); stop.add_argument("--reason", default="stopped by operator"); stop.set_defaults(func=cmd_control)
    resume = sub.add_parser("resume"); resume.add_argument("--run", dest="run_id", type=int); resume.set_defaults(func=cmd_control)
    route = sub.add_parser("reroute"); route.add_argument("provider"); route.add_argument("model"); route.add_argument("--effort"); route.add_argument("--message"); route.add_argument("--run", dest="run_id", type=int); route.set_defaults(func=cmd_reroute)
    for name in ("retry", "continue"):
        item = sub.add_parser(name); item.add_argument("run_id", type=int); item.add_argument("--request-id");
        if name == "continue": item.add_argument("direction")
        item.set_defaults(func=cmd_retry)
    profiles_parser = sub.add_parser("profiles"); profiles_parser.set_defaults(func=cmd_list)
    imp = sub.add_parser("profiles-import-v2"); imp.add_argument("--v2-db", default=os.path.expanduser("~/.orchestra/v2/orchestra.db")); imp.add_argument("--apply", action="store_true"); imp.set_defaults(func=cmd_profiles_import_v2)
    profile = sub.add_parser("profile-create"); profile.add_argument("name"); profile.add_argument("provider"); profile.add_argument("model"); profile.add_argument("--slug"); profile.add_argument("--effort"); profile.add_argument("--tier", type=int, default=1); profile.add_argument("--max-concurrency", type=int); profile.add_argument("--note"); profile.set_defaults(func=cmd_profile_create)
    groups_parser = sub.add_parser("groups"); groups_parser.set_defaults(func=cmd_list)
    group = sub.add_parser("group-create"); group.add_argument("name"); group.add_argument("--slug"); group.add_argument("--cwd"); group.set_defaults(func=cmd_group_create)
    ask = sub.add_parser("ask"); ask.add_argument("prompt"); ask.add_argument("--kind", default="question"); ask.add_argument("--run", dest="run_id", type=int); ask.set_defaults(func=cmd_ask)
    child = sub.add_parser("child"); child.add_argument("profile"); child.add_argument("objective"); child.add_argument("--strategy", default="goal"); child.add_argument("--max-rounds", type=int); child.add_argument("--request-id"); child.add_argument("--run", dest="run_id", type=int); child.set_defaults(func=cmd_child)
    artifact = sub.add_parser("artifact"); artifact.add_argument("path"); artifact.add_argument("--name"); artifact.add_argument("--run", dest="run_id", type=int); artifact.set_defaults(func=cmd_artifact)
    delegate = sub.add_parser("delegate", help="bounded read-only Antigravity review of the worktree"); delegate.add_argument("objective"); delegate.add_argument("--model", required=True); delegate.add_argument("--mode", default="review"); delegate.add_argument("--run", dest="run_id", type=int); delegate.set_defaults(func=cmd_delegate)
    att = sub.add_parser("attention"); att_sub = att.add_subparsers(dest="action", required=True)
    al = att_sub.add_parser("list"); al.add_argument("--status", default="open"); al.set_defaults(func=cmd_attention)
    lease = att_sub.add_parser("lease"); lease.add_argument("attention_id"); lease.add_argument("--seconds", type=int, default=60); lease.set_defaults(func=cmd_attention)
    answer = att_sub.add_parser("answer"); answer.add_argument("attention_id"); answer.add_argument("answer"); answer.add_argument("--lease-id"); answer.set_defaults(func=cmd_attention)
    pair = sub.add_parser("pair"); pair.set_defaults(func=cmd_pair)
    token = sub.add_parser("service-token"); token.add_argument("name"); token.add_argument("authorities", nargs="+"); token.set_defaults(func=cmd_service)
    storage_parser = sub.add_parser("storage"); storage_parser.set_defaults(func=cmd_list)
    svc = sub.add_parser("service", help="launchd LaunchAgent for the daemon (macOS)"); svc_sub = svc.add_subparsers(dest="action", required=True)
    svc_install = svc_sub.add_parser("install"); svc_install.add_argument("--start", action="store_true"); svc_install.set_defaults(func=service.main)
    for name in ("uninstall", "status", "restart"):
        svc_sub.add_parser(name).set_defaults(func=service.main)
    return parser


def main(argv=None):
    try:
        result = build_parser().parse_args(argv)
        return result.func(result) or 0
    except (claude.ClaudeError, client.ClientError, dsh.DshError, ValueError) as exc:
        print(f"orchestra-next: {exc}", file=sys.stderr)
        return 1
