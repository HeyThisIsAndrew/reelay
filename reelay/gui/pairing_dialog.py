"""Pairing Dialog: Authorize AI assistants (Claude Code, Cursor, Muse) via one-time code (Issue #7)."""

from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..pairing import PAIRING_TTL_SECONDS, PairingManager
from .theme import set_prop
from .tips import tip
from .widgets import label


class PairingDialog(QDialog):
    """Shows an RLY-XXXX-XXXX pairing code for AI agents, with countdown and paired client management."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair AI Client")
        self.resize(520, 480)
        self.store = store
        self.mgr = PairingManager(store=store)
        self.remaining_seconds = int(PAIRING_TTL_SECONDS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(14)

        # Header
        lay.addWidget(label("Pair an AI Client with Reelay", name="PageTitle"))
        lay.addWidget(
            label(
                "Enter this one-time code in your AI assistant (e.g. Claude Code, Cursor, Muse). "
                "The code is valid for 5 minutes and grants local access to read the board, "
                "draft copy, and submit changes for your approval.",
                "muted",
            )
        )

        # Code display card
        code_card = QFrame()
        code_card.setObjectName("PairingCodeCard")
        code_card.setStyleSheet(
            "QFrame#PairingCodeCard { background-color: #1a1a1f; border: 1px solid #33333e; border-radius: 8px; padding: 14px; }"
        )
        ccl = QVBoxLayout(code_card)
        ccl.setAlignment(Qt.AlignCenter)
        ccl.setSpacing(8)

        self.code_label = QLabel("RLY-....-....")
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code_label.setStyleSheet("font-family: monospace; font-size: 26px; font-weight: bold; color: #5aa7ff; letter-spacing: 3px;")
        ccl.addWidget(self.code_label)

        self.timer_label = QLabel("Valid for 05:00")
        self.timer_label.setAlignment(Qt.AlignCenter)
        self.timer_label.setStyleSheet("font-size: 13px; color: #888899;")
        ccl.addWidget(self.timer_label)

        lay.addWidget(code_card)

        # Action row
        act_row = QHBoxLayout()
        self.new_code_btn = tip(QPushButton("Generate New Code"), "Generate a fresh 5-minute pairing code")
        self.new_code_btn.clicked.connect(self._generate_code)
        act_row.addWidget(self.new_code_btn)
        act_row.addStretch()
        lay.addLayout(act_row)

        # Paired clients section
        lay.addWidget(label("Authorized Clients", name="SectionTitle"))
        self.clients_list = QListWidget()
        self.clients_list.setFixedHeight(140)
        lay.addWidget(self.clients_list)

        # Client actions row
        btn_row = QHBoxLayout()
        self.revoke_btn = tip(QPushButton("Revoke Selected Client"), "Revoke access for the selected client immediately")
        set_prop(self.revoke_btn, "danger")
        self.revoke_btn.clicked.connect(self._revoke_selected)
        btn_row.addWidget(self.revoke_btn)
        btn_row.addStretch()

        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        # Timer
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        # Initial code generation & client load
        self._generate_code()
        self._refresh_clients()

    def _generate_code(self) -> None:
        code, ttl = self.mgr.create_pairing_code(store=self.store)
        self.remaining_seconds = int(ttl)
        self.code_label.setText(code)
        self._update_timer_label()

    def _tick(self) -> None:
        if self.remaining_seconds > 0:
            self.remaining_seconds -= 1
            self._update_timer_label()
        else:
            self.code_label.setText("EXPIRED")
            self.timer_label.setText("Code expired. Click 'Generate New Code'.")

    def _update_timer_label(self) -> None:
        mins = self.remaining_seconds // 60
        secs = self.remaining_seconds % 60
        self.timer_label.setText(f"Valid for {mins:02d}:{secs:02d}")

    def _refresh_clients(self) -> None:
        self.clients_list.clear()
        clients = self.mgr.list_paired_clients(self.store)
        if not clients:
            item = QListWidgetItem("No AI clients paired yet.")
            item.setFlags(Qt.NoItemFlags)
            self.clients_list.addItem(item)
            self.revoke_btn.setEnabled(False)
            return

        self.revoke_btn.setEnabled(True)
        for c in clients:
            created = c.get("created_at", "")[:10]
            last_used = c.get("last_used_at", "")
            last_txt = f"Last active: {last_used[:10]}" if last_used else "Never used"
            text = f"{c.get('client_name', 'Client')} (ID: {c.get('client_id')}) — Added {created} · {last_txt}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, c.get("client_id"))
            self.clients_list.addItem(item)

    def _revoke_selected(self) -> None:
        item = self.clients_list.currentItem()
        if not item:
            return
        client_id = item.data(Qt.UserRole)
        if not client_id:
            return

        reply = QMessageBox.question(
            self,
            "Revoke Access?",
            f"Are you sure you want to revoke access for this client? Its very next request will be refused.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.mgr.revoke_client(self.store, client_id)
            self._refresh_clients()
