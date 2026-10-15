"""Approvals: what AI assistants asked Reelay to do, decided here instead of in a terminal.

The decisions go through ``reelay.approvals`` (``approve`` / ``reject``), the same functions the terminal
uses, so every protection still holds:

- only a request that is still pending and hasn't expired can be decided (expired ones are expired first);
- approving checks the video hasn't changed since the request (its target hash). If it has, the request
  becomes stale and nothing happens;
- this window never carries anything out. The running app's dispatcher does that once per request,
  checking the hash again right before acting, and never repeats one that was interrupted;
- two people (or a person and the terminal) can't both decide: the store moves a request atomically.

A request that asks to skip the Ready check (``force``) says so in red, shows what isn't passing now,
and approving it takes a second, explicit confirmation. A computer that has a shared database open
view only can read requests but not decide them.
"""

from __future__ import annotations

import getpass
import socket
from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .. import approvals
from ..models import ApprovalRequest, ApprovalStatus, Platform
from ..pipeline import utc_iso
from ..scoped_pipeline import OPERATION_LABELS
from ..text import local_text, short_title
from .theme import set_prop
from .tips import tip


def decided_by() -> str:
    """Who decided, as the request records it: the person at this computer, in the app."""
    try:
        return f"{getpass.getuser()} on {socket.gethostname().removesuffix('.local')} (Reelay app)"
    except Exception:  # noqa: BLE001
        return "Reelay app"


def pending_for(store, profile_id: int) -> list[ApprovalRequest]:
    """Requests still waiting for a decision for this profile, newest first (expired ones excluded)."""
    now = utc_iso(datetime.now(timezone.utc))
    if not store.read_only:
        store.expire_approvals(now)
    return [r for r in store.list_approvals(profile_id, ApprovalStatus.PENDING, 200) if r.expires_at > now]


def video_titles(store, req: ApprovalRequest, limit: int = 5) -> list[str]:
    out = []
    for vid in req.target_ids[:limit]:
        row, post = store.get_video(vid), store.get_post(vid)
        out.append(short_title((post.title if post and post.title else row.item.hook) if row else "(deleted video)", 70))
    if len(req.target_ids) > limit:
        out.append(f"and {len(req.target_ids) - limit} more")
    return out


def _label(text: str = "", prop: str | None = None, name: str | None = None) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if name:
        lab.setObjectName(name)
    if prop:
        lab.setProperty(prop, True)
    return lab


class ApprovalsDialog(QDialog):
    def __init__(self, pipeline, profile, parent=None, focus_video: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Approvals")
        self.resize(760, 480)
        self.pipeline, self.store, self.profile = pipeline, pipeline.store, profile
        self.decided = 0
        self.requests: list[ApprovalRequest] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.setSpacing(10)
        lay.addWidget(_label("AI assistants asked Reelay to do these. Nothing happens until you approve. Reelay "
                             "then checks the video hasn't changed since the request and carries it out once.",
                             "muted"))
        self.notice = _label(name="ApprovalOutcome")
        self.notice.hide()
        lay.addWidget(self.notice)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.list = QListWidget(objectName="ApprovalList")
        self.list.setFixedWidth(250)
        self.list.currentRowChanged.connect(self._show)
        body.addWidget(self.list)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(8)
        self.title = _label(name="DetailTitle")
        self.meta = _label(prop="faint")
        self.videos = _label(prop="muted")
        self.what = _label()
        self.force = QFrame(objectName="ForceNotice")
        fl = QVBoxLayout(self.force)
        fl.setContentsMargins(12, 9, 12, 9)
        fl.setSpacing(3)
        self.force_title = _label("Skips the Ready check", name="ForceNoticeTitle")
        self.force_text = _label(name="ForceNoticeText")
        fl.addWidget(self.force_title)
        fl.addWidget(self.force_text)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for rejecting (optional)")
        for w in (self.title, self.meta, self.videos, self.what, self.force, self.reason):
            dl.addWidget(w)
        dl.addStretch()
        buttons = QHBoxLayout()
        self.reject_btn = tip(QPushButton("Reject"), "Say no: nothing is done, and the request is closed")
        set_prop(self.reject_btn, "danger")
        self.reject_btn.clicked.connect(self._reject)
        self.approve_btn = tip(QPushButton("Approve"), "Let Reelay do it. It checks the video hasn't changed "
                                                       "first, then carries it out within a minute.")
        set_prop(self.approve_btn, "primary")
        self.approve_btn.clicked.connect(self._approve)
        buttons.addWidget(self.reject_btn)
        buttons.addStretch()
        buttons.addWidget(self.approve_btn)
        dl.addLayout(buttons)
        self.empty = _label("Nothing is waiting for your approval.", "faint")
        self.empty.setAlignment(Qt.AlignCenter)
        self.detail = detail
        body.addWidget(detail, 1)
        body.addWidget(self.empty, 1)
        lay.addLayout(body, 1)

        self.view_only = _label("This computer has the shared database open view only, so it can't decide. "
                                "Decide on the computer that's editing it.", "faint")
        lay.addWidget(self.view_only)
        foot = QHBoxLayout()
        foot.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)
        self.refresh(focus_video)

    # ------------------------------------------------------------------ data
    def refresh(self, focus_video: int | None = None) -> None:
        keep = self.current().id if self.current() else None
        self.requests = pending_for(self.store, self.profile.id)
        self.list.blockSignals(True)
        self.list.clear()
        for req in self.requests:
            label = OPERATION_LABELS.get(req.operation, req.operation)
            first = (video_titles(self.store, req, 1) or [""])[0]
            item = QListWidgetItem(f"{'⚠ ' if approvals.skips_ready_check(req) else ''}{label}\n{first}")
            item.setData(Qt.UserRole, req.id)
            self.list.addItem(item)
        self.list.blockSignals(False)
        has = bool(self.requests)
        self.detail.setVisible(has)
        self.empty.setVisible(not has)
        self.view_only.setVisible(self.store.read_only)
        if has:
            pick = next((i for i, r in enumerate(self.requests) if r.id == keep), None)
            if focus_video is not None:
                pick = next((i for i, r in enumerate(self.requests) if focus_video in r.target_ids), pick)
            self.list.setCurrentRow(pick or 0)
            self._show(self.list.currentRow())

    def current(self) -> ApprovalRequest | None:
        row = self.list.currentRow() if hasattr(self, "list") else -1
        return self.requests[row] if 0 <= row < len(self.requests) else None

    def _show(self, row: int) -> None:
        req = self.current()
        if req is None:
            return
        self.title.setText(OPERATION_LABELS.get(req.operation, req.operation))
        self.meta.setText(f"Asked by {req.requested_by} · {local_text(req.created_at)} · "
                          f"expires {local_text(req.expires_at)}")
        self.videos.setText("Video: " + "; ".join(video_titles(self.store, req)) if req.target_ids else "")
        self.videos.setVisible(bool(req.target_ids))
        lines = [line for line in approvals.explain(self.store, req) if not line.startswith("Skips the Ready")]
        self.what.setText("If you approve: " + " ".join(lines))
        forced = approvals.skips_ready_check(req)
        self.force.setVisible(forced)
        if forced:
            self.force_text.setText("Platforms whose checklist isn't passing would be scheduled anyway. "
                                    + self._not_ready_now(req))
        decidable = not self.store.read_only
        self.approve_btn.setEnabled(decidable)
        self.reject_btn.setEnabled(decidable)
        self.reason.setEnabled(decidable)
        self.approve_btn.setText("Approve Anyway…" if forced else "Approve")

    def _not_ready_now(self, req: ApprovalRequest) -> str:
        try:
            row = self.store.get_video(req.payload["video_id"])
            readiness = self.pipeline.readiness(row.id, self.store.get_post(row.id))
        except Exception:  # noqa: BLE001 — the notice still stands without the detail
            return ""
        if readiness is None or not readiness.blocking:
            return "Right now every platform it's going to passes."
        return "Not passing now: " + ", ".join(Platform.LABELS.get(n, n) for n in readiness.blocking) + "."

    # ------------------------------------------------------------------ decisions
    def _say(self, text: str, tone: str) -> None:
        self.notice.setText(text)
        set_prop(self.notice, "tone", tone)
        self.notice.show()

    def _approve(self) -> None:
        req = self.current()
        if req is None or self.store.read_only:
            return
        if approvals.skips_ready_check(req):
            answer = QMessageBox.question(
                self, "Approve without the Ready check?",
                "This request asks to schedule even where the Ready check isn't passing. "
                + self._not_ready_now(req) + "\n\nApprove it anyway?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return
        try:
            done = approvals.approve(self.store, req.id, decided_by())
        except approvals.ApprovalError as exc:
            self._say(str(exc), "danger")
        else:
            self.decided += 1
            if done.status == ApprovalStatus.STALE:
                self._say(approvals.STALE_REASON, "danger")
            else:
                self._say("Approved. Reelay carries it out within a minute while it's open.", "neutral")
        self.refresh()

    def _reject(self) -> None:
        req = self.current()
        if req is None or self.store.read_only:
            return
        try:
            approvals.reject(self.store, req.id, decided_by(), self.reason.text().strip())
        except approvals.ApprovalError as exc:
            self._say(str(exc), "danger")
        else:
            self.decided += 1
            self.reason.clear()
            self._say("Rejected. Nothing was done.", "neutral")
        self.refresh()
