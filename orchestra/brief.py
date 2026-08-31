"""Neutral, bounded instructions frozen for one v2 run."""
from __future__ import annotations

from pathlib import Path


PROTOCOL = """## Orchestra protocol

- Work only inside the supplied working directory and declared external access.
- Do not alter the owner's checkout or rewrite git history. Orchestra snapshots changes.
- Operator messages may arrive between actions; apply them and continue.
- Use `orchestra ask` only when progress genuinely requires a decision or missing input.
- Publish only intentional outputs with `orchestra artifact PATH`.
- End with a concise result: what happened, useful outputs, and unresolved caveats.
"""

DELEGATION_HEAD = """## Delegation

You may delegate bounded pieces with `orchestra child --profile PROFILE -- MISSION`.
Name each child profile explicitly. Children inherit this run's group and working
directory. You remain responsible for combining their results.

Run `orchestra profiles` first and skip any profile marked unavailable. A
profile whose provider has no capacity left is admitted and then held, so the
child never starts. Among profiles that would do the job equally well, prefer
one marked burn and avoid one marked preserve; that is the owner saying which
account to spend and which to leave alone.
"""

TIERS = {1: "workhorse", 2: "core", 3: "frontier"}


def delegation(children: int | None = None,
               tier_ceiling: int | None = None) -> str:
    """The delegation paragraph, stating the ceilings this run really has.

    An operator may raise either one when dispatching, so the tier sentence is
    replaced rather than appended: a brief that said both "your tier or lower"
    and "any tier up to 3" would contradict itself.
    """
    tier = ("Children may use your tier or a lower tier."
            if tier_ceiling is None else
            f"Children may use any tier up to {tier_ceiling} "
            f"({TIERS[tier_ceiling]}), including tiers above your own.")
    count = ("" if children is None else
             f" You may run up to {children} children at once.")
    return DELEGATION_HEAD + tier + count + "\n"


DELEGATION = delegation()


def compose(*, run_id: int, display_number: str, profile_name: str,
            runtime_name: str, request: str, requester: str, group_name: str,
            workdir: str | Path, context: str | None = None,
            may_delegate: bool = False,
            max_children: int | None = None,
            max_child_tier: int | None = None) -> str:
    parts = [f"""# {display_number}

- Run ID: `{run_id}`
- Group: **{group_name}**
- Profile: **{profile_name}** via **{runtime_name}**
- Requested by: **{requester}**
- Working directory: `{workdir}`

## Request

{request.strip()}
"""]
    if context and context.strip():
        parts.append(f"## Context\n\n{context.strip()}\n")
    parts.append(PROTOCOL)
    if may_delegate:
        parts.append(delegation(max_children, max_child_tier))
    return "\n".join(parts).rstrip() + "\n"


def resume_message(*, reason: str, messages: list[str], child_results: list[str] | None = None,
                   replay_risk: bool = False) -> str:
    parts = [f"# Resume this run\n\nReason: {reason.strip()}"]
    if replay_risk:
        parts.append(
            "The prior turn ended before a reliable session reference was captured. "
            "This is a replay from the frozen brief; inspect existing files before "
            "repeating any side effect.")
    if messages:
        parts.append("## New direction\n\n" + "\n\n".join(messages))
    if child_results:
        parts.append("## Child results\n\n" + "\n\n".join(child_results))
    return "\n\n".join(parts).rstrip() + "\n"
