"""Approving what agents asked for: a person decides, the running app carries it out.

Decisions (``approve`` / ``reject``) are only made by a person on this
computer: from the terminal now (``python -m reelay.approvals``), from the
app later. The agent API can list requests but has no way to decide them.

Approving checks the target hash first: if the video changed after the request
(a caption edited, a file replaced, another post scheduled), the request
becomes "stale" and nothing happens. An approved request is then carried out
by the running Reelay app (the dispatcher, ``execute_approved``), because
that's the process that owns uploads and posting; it checks the hash once more
right before acting. A computer with a shared database open view only never
carries anything out.

Status flow:  pending → approved → executed | failed | stale
              pending → rejected | expired | stale
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from .models import ApprovalRequest, ApprovalStatus
from .pipeline import PipelineError, utc_iso
from .scoped_pipeline import OPERATION_LABELS, OPERATIONS, hash_for

log = logging.getLogger("reelay")

STALE_REASON = "The video changed after this was requested, so it wasn't done. Ask again if it's still wanted."


class ApprovalError(Exception):
    """Plain-language reason a decision couldn't be made."""


def _get(store, approval_id: int) -> ApprovalRequest:
    store.expire_approvals(utc_iso(datetime.now(timezone.utc)))
    req = store.get_approval(approval_id)
    if req is None:
        raise ApprovalError(f"There's no approval request #{approval_id}.")
    return req


def approve(store, approval_id: int, decided_by: str) -> ApprovalRequest:
    """A person approves. Returns the request: approved (the app will carry it out), or stale."""
    req = _get(store, approval_id)
    if req.status != ApprovalStatus.PENDING:
        raise ApprovalError(f"Request #{approval_id} is {req.status}, not waiting for a decision.")
    if req.operation not in OPERATIONS:
        raise ApprovalError(f"Request #{approval_id} asks for an unknown operation; reject it.")
    if hash_for(store, req) != req.target_hash:
        done = store.transition_approval(approval_id, ApprovalStatus.STALE, decided_by, {"reason": STALE_REASON})
        if done is None:
            raise ApprovalError(f"Request #{approval_id} was decided in the meantime.")
        return done
    done = store.transition_approval(approval_id, ApprovalStatus.APPROVED, decided_by)
    if done is None:
        raise ApprovalError(f"Request #{approval_id} was decided in the meantime.")
    return done


def reject(store, approval_id: int, decided_by: str, reason: str = "") -> ApprovalRequest:
    req = _get(store, approval_id)
    if req.status != ApprovalStatus.PENDING:
        raise ApprovalError(f"Request #{approval_id} is {req.status}, not waiting for a decision.")
    done = store.transition_approval(approval_id, ApprovalStatus.REJECTED, decided_by,
                                     {"reason": reason} if reason else None)
    if done is None:
        raise ApprovalError(f"Request #{approval_id} was decided in the meantime.")
    return done


def execute_approved(pipeline) -> list[ApprovalRequest]:
    """In the running app: carry out every approved request, once each. Returns what was handled."""
    store = pipeline.store
    handled = []
    for req in store.list_approvals(status=ApprovalStatus.APPROVED, limit=50)[::-1]:     # oldest first
        if req.result and req.result.get("started_at"):
            # Reelay stopped while doing this last time: never do it twice.
            done = store.transition_approval(req.id, ApprovalStatus.FAILED, result={
                "error": "Reelay stopped while carrying this out, so it may be partly done. Check the ticket.",
                "started_at": req.result["started_at"]})
            handled.append(done or req)
            continue
        if hash_for(store, req) != req.target_hash:
            done = store.transition_approval(req.id, ApprovalStatus.STALE, result={"reason": STALE_REASON})
            handled.append(done or req)
            continue
        handler = OPERATIONS.get(req.operation)
        if handler is None or not store.mark_approval_started(req.id):
            continue
        try:
            with store.origin("approval"):
                result = handler(pipeline, req.payload)
        except PipelineError as exc:
            done = store.transition_approval(req.id, ApprovalStatus.FAILED,
                                             result={"error": str(exc), "code": exc.code, "problems": exc.problems})
        except Exception as exc:  # noqa: BLE001 — record it; never crash the dispatcher
            log.exception("Approved request #%s failed", req.id)
            done = store.transition_approval(req.id, ApprovalStatus.FAILED,
                                             result={"error": f"Unexpected error ({type(exc).__name__})"})
        else:
            done = store.transition_approval(req.id, ApprovalStatus.EXECUTED, result=result)
            log.info("Carried out approved request #%s (%s).", req.id, req.operation)
        handled.append(done or req)
    return handled


# ---------------------------------------------------------------------- terminal
def describe(store, req: ApprovalRequest) -> str:
    titles = []
    for vid in req.target_ids[:5]:
        row = store.get_video(vid)
        post = store.get_post(vid)
        titles.append(f"#{vid} " + ((post.title if post and post.title else row.item.hook[:50]) if row else "(gone)"))
    more = f" (+{len(req.target_ids) - 5} more)" if len(req.target_ids) > 5 else ""
    args = {k: v for k, v in req.payload.items() if k not in ("video_ids", "video_id", "rows", "headers")}
    lines = [f"#{req.id}  {OPERATION_LABELS.get(req.operation, req.operation)}  [{req.status}]",
             f"    asked by {req.requested_by} at {req.created_at}; expires {req.expires_at}"]
    if titles:
        lines.append("    videos: " + "; ".join(titles) + more)
    if args:
        lines.append(f"    details: {args}")
    if req.decided_by or req.result:
        lines.append(f"    decided by {req.decided_by or '-'} at {req.decided_at or '-'}; result: {req.result}")
    return "\n".join(lines)


def _decider() -> str:
    import getpass
    import socket
    try:
        return f"{getpass.getuser()} on {socket.gethostname().removesuffix('.local')} (terminal)"
    except Exception:  # noqa: BLE001
        return "terminal"


def main(argv: list[str] | None = None, store=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reelay.approvals",
                                     description="Review what AI agents asked Reelay to do.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="requests waiting for a decision (--all for every request)")
    ls.add_argument("--all", action="store_true")
    for name in ("show", "approve"):
        sub.add_parser(name).add_argument("id", type=int)
    rj = sub.add_parser("reject")
    rj.add_argument("id", type=int)
    rj.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    own = store is None
    if own:
        from . import dblocation
        from .store import Store
        lease = dblocation.Lease(dblocation.current_path(), "approvals-cli")
        other = lease.other()
        import socket
        if other is not None and other.host != socket.gethostname().removesuffix(".local"):
            print(f"{other.describe()} is editing this shared database on another computer. Decide there.")
            return 2
        store = Store(dblocation.current_path())
    try:
        if args.cmd == "list":
            store.expire_approvals(utc_iso(datetime.now(timezone.utc)))
            reqs = store.list_approvals(status=None if args.all else ApprovalStatus.PENDING, limit=200)
            if not reqs:
                print("Nothing waiting for approval." if not args.all else "No approval requests yet.")
            for req in reqs:
                print(describe(store, req))
            return 0
        if args.cmd == "show":
            print(describe(store, _get(store, args.id)))
            return 0
        if args.cmd == "approve":
            req = approve(store, args.id, _decider())
            if req.status == ApprovalStatus.STALE:
                print(f"#{req.id} is stale: {STALE_REASON}")
                return 1
            print(f"Approved #{req.id}. Reelay carries it out within a minute while it's open (or when it next "
                  "opens).")
            return 0
        if args.cmd == "reject":
            req = reject(store, args.id, _decider(), args.reason)
            print(f"Rejected #{req.id}. Nothing was done.")
            return 0
    except ApprovalError as exc:
        print(exc)
        return 1
    finally:
        if own:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
