"""Writing help in the ticket: spelling & grammar, local-AI rewrites, SEO."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import assist
from .theme import set_prop
from .tips import tip
from .workers import CallWorker


def _label(text: str = "", prop: str | None = None) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    if prop:
        lab.setProperty(prop, True)
    return lab


# ====================================================================== spelling & grammar
class GrammarDialog(QDialog):
    """Lists possible mistakes; click a suggestion to fix it in the text."""

    def __init__(self, text: str, store, label: str, parent=None, check=None):
        super().__init__(parent)
        self.setWindowTitle(f"Check: {label}")
        self.resize(560, 460)
        self.text, self.store = text, store
        self._check = check or (lambda t: assist.check(t, assist.dictionary(store)))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        self.engine = _label(prop="faint")
        lay.addWidget(self.engine)
        self.preview = QPlainTextEdit(text)
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(90)
        tip(self.preview, "The text as it will be after your fixes")
        lay.addWidget(self.preview)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.list = QVBoxLayout(self.body)
        self.list.setSpacing(8)
        scroll.setWidget(self.body)
        lay.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("Use Fixed Text")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        issues, engine = self._check(self.text)
        ok, why = assist.languagetool_available()
        self.engine.setText(engine + ("" if ok else f" · {why}"))
        self.preview.setPlainText(self.text)
        self.issues = issues
        if not issues:
            self.list.addWidget(_label("No mistakes found. ✓", "success"))
        for issue in issues:
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            rl.addWidget(_label(issue.message, "danger" if issue.kind != "style" else "muted"))
            buttons = QHBoxLayout()
            for rep in issue.replacements[:4]:
                b = tip(QPushButton(rep or "(remove)"), f"Replace with “{rep}”")
                b.clicked.connect(lambda _=False, i=issue, r=rep: self._fix(i, r))
                buttons.addWidget(b)
            if issue.kind == "spelling" and issue.word:
                add = tip(QPushButton("Add to Dictionary"), f"Never flag “{issue.word}” again (for slang and names)")
                set_prop(add, "flat")
                add.clicked.connect(lambda _=False, w=issue.word: self._learn(w))
                buttons.addWidget(add)
            buttons.addStretch()
            rl.addLayout(buttons)
            self.list.addWidget(row)
        self.list.addStretch()

    def _fix(self, issue, replacement: str) -> None:
        self.text = issue.apply(self.text, replacement)
        self.refresh()

    def _learn(self, word: str) -> None:
        assist.add_to_dictionary(self.store, word)
        self.refresh()


# ====================================================================== rewrite
class RewriteDialog(QDialog):
    """Rewrite with a local model. The result is a suggestion: edit it, then use it."""

    def __init__(self, text: str, store, label: str, limit: int | None, counter, parent=None, rewriter=None,
                 brand=None):
        super().__init__(parent)
        self.setWindowTitle(f"Rewrite: {label}")
        self.resize(600, 520)
        self.text, self.store, self.label, self.limit, self.counter = text, store, label, limit, counter
        self.brand = brand
        self.rewriter = rewriter if rewriter is not None else assist.rewriter_from(store)
        self.worker: CallWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        self.status = _label(prop="muted")
        lay.addWidget(self.status)
        self.voice = _label(prop="faint")
        self.voice.setText(f"Brand voice: <b>{self.brand.name}</b> (Settings → Brand voice)" if self.brand else
                           "No brand voice set up. Add one in Settings → Brand voice so rewrites sound like you.")
        lay.addWidget(self.voice)
        row = QHBoxLayout()
        self.style = tip(QComboBox(), "How to rewrite it")
        for key, name in assist.STYLE_LABELS.items():
            if key == "voice" and not self.brand:
                continue
            self.style.addItem(name, key)
        row.addWidget(self.style, 1)
        self.run_btn = tip(QPushButton("Rewrite"), "Ask the model on this computer for a new version. Free, offline.")
        set_prop(self.run_btn, "primary")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)
        lay.addLayout(row)
        lay.addWidget(_label("<b>Now</b>", "faint"))
        original = QPlainTextEdit(text)
        original.setReadOnly(True)
        original.setFixedHeight(90)
        lay.addWidget(original)
        lay.addWidget(_label("<b>Suggestion</b> (you can edit it)", "faint"))
        self.result = QPlainTextEdit()
        self.result.setPlaceholderText("The rewritten caption appears here")
        self.result.textChanged.connect(self._count)
        lay.addWidget(self.result, 1)
        self.length = _label(prop="faint")
        lay.addWidget(self.length)
        self.install = tip(QPushButton("Get Ollama (free)…"), "Ollama runs small AI models on your own computer")
        set_prop(self.install, "flat")
        self.install.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ollama.com/download")))
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.use_btn = buttons.button(QDialogButtonBox.Ok)
        self.use_btn.setText("Use This")
        self.use_btn.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.install)
        bottom.addStretch()
        bottom.addWidget(buttons)
        lay.addLayout(bottom)
        self._check_engine()

    def _check_engine(self) -> None:
        if self.rewriter is None:
            self.status.setText("Rewriting is off. Turn it on in Settings → Local AI helper.")
            self.run_btn.setEnabled(False)
            return
        ok, why = self.rewriter.status()
        self.status.setText(why)
        set_prop(self.status, "danger", not ok)
        self.run_btn.setEnabled(ok)
        self.install.setVisible(not ok and isinstance(self.rewriter, assist.OllamaRewriter))

    def _run(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.run_btn.setEnabled(False)
        self.status.setText("Rewriting on this computer… (the first run can take a moment)")
        self.worker = CallWorker(assist.rewrite, self.rewriter, self.text, self.style.currentData(), self.label,
                                 self.limit, brand=self.brand, parent=self)
        self.worker.succeeded.connect(self._done)
        self.worker.failed.connect(lambda msg: (self.status.setText(msg), self.run_btn.setEnabled(True)))
        self.worker.start()

    def _done(self, text: str) -> None:
        self.result.setPlainText(text)
        self.status.setText("Here's a suggestion. Edit it if you like, then click Use This.")
        self.run_btn.setEnabled(True)

    def _count(self) -> None:
        text = self.result.toPlainText()
        n = self.counter(text) if self.counter else len(text)
        over = bool(self.limit and n > self.limit)
        banned = assist.avoided_words(text, self.brand)
        self.length.setText(f"{n:,}" + (f"/{self.limit:,}" if self.limit else "") +
                            (" · too long for this platform: shorten it before using" if over else "") +
                            (f" · uses words {self.brand.name} avoids: {', '.join(banned)}" if banned else ""))
        set_prop(self.length, "danger", over or bool(banned))
        self.use_btn.setEnabled(bool(text.strip()))

    def value(self) -> str:
        return self.result.toPlainText().strip()

    def reject(self) -> None:
        if self.worker:
            self.worker.wait(2000)
        super().reject()


# ====================================================================== SEO
class SEODialog(QDialog):
    """Keyword ideas from YouTube search, a score with tips, and one-click tags."""

    def __init__(self, title: str, tags: list[str], description: str, seed_list: list[str], parent=None,
                 provider=None):
        super().__init__(parent)
        from ..seo import provider_from
        self.setWindowTitle("SEO Suggestions")
        self.resize(620, 580)
        self.title, self.tags, self.description = title, list(tags), description
        self.provider = provider or provider_from()
        self.worker: CallWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(_label("Keyword ideas come from what people type into YouTube's search box. Tick the ones "
                             "that fit this video to add them as tags. Nothing changes until you save the ticket.",
                             "muted"))
        row = QHBoxLayout()
        self.seed = tip(QLineEdit(", ".join(seed_list)), "Search phrases to start from, separated by commas")
        self.seed.setPlaceholderText("e.g. polarized sunglasses, beach day")
        row.addWidget(self.seed, 1)
        self.run_btn = tip(QPushButton("Get Suggestions"), "Ask YouTube's search box for related phrases")
        set_prop(self.run_btn, "primary")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)
        lay.addLayout(row)
        self.score = _label(prop="muted")
        lay.addWidget(self.score)
        self.checks = _label()
        lay.addWidget(self.checks)
        self.ideas = QListWidget()
        tip(self.ideas, "Tick ideas to add them as tags")
        lay.addWidget(self.ideas, 1)
        self.status = _label(prop="faint")
        lay.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.add_btn = buttons.button(QDialogButtonBox.Ok)
        self.add_btn.setText("Add Ticked as Tags")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.status.setText(f"Source: {self.provider.name}")

    def _run(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        seed_list = [s.strip() for s in self.seed.text().split(",") if s.strip()]
        if not seed_list:
            self.status.setText("Type a phrase to start from.")
            return
        self.run_btn.setEnabled(False)
        self.status.setText("Asking YouTube…")
        self.worker = CallWorker(self.provider.report, self.title, self.tags, self.description, seed_list, parent=self)
        self.worker.succeeded.connect(self._show)
        self.worker.failed.connect(lambda msg: (self.status.setText(msg), self.run_btn.setEnabled(True)))
        self.worker.start()

    def _show(self, report) -> None:
        self.run_btn.setEnabled(True)
        self.score.setText(f"<b>Score: {report.score}/100</b> for the current title and tags")
        self.checks.setText("<br>".join(f"{'✓' if c.ok else '⚠'} <b>{c.label}</b>: {c.tip}" for c in report.checks))
        self.ideas.clear()
        have = {t.lower() for t in self.tags}
        for idea in report.keywords:
            item = QListWidgetItem(idea)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            if idea.lower() in have:
                item.setText(f"{idea}  (already a tag)")
                item.setFlags(Qt.NoItemFlags)
            self.ideas.addItem(item)
        self.status.setText(f"{len(report.keywords)} ideas · Source: {report.provider}")

    def chosen(self) -> list[str]:
        return [self.ideas.item(i).text() for i in range(self.ideas.count())
                if self.ideas.item(i).checkState() == Qt.Checked]

    def reject(self) -> None:
        if self.worker:
            self.worker.wait(2000)
        super().reject()

