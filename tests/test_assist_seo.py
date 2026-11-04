"""Local writing help (spelling/grammar, local-model rewrites) and SEO help.
No real model, LanguageTool or YouTube is contacted."""

from __future__ import annotations

import pytest

from reelay import assist, seo
from reelay.vidiq_service import VidIQService
from tests.conftest import FakeResponse, FakeSession


# ------------------------------------------------------------------ checks
def test_builtin_check_flags_real_mistakes_only():
    text = ("these sunnies are the the best  sunglases i ever had , grab yours #polarizedsunglases "
            "@blenderz Blenders https://x.co/abcd")
    issues = assist.builtin_check(text, {"sunnies"})
    found = [(i.kind, text[i.offset:i.offset + i.length]) for i in issues]
    assert found == [("grammar", "the the"), ("style", "  "), ("spelling", "sunglases"), ("style", " ,")]
    fixed = issues[2].apply(text, issues[2].replacements[0])
    assert "best  sunglasses i" in fixed                            # hashtags, handles, names, links untouched


def test_dictionary_is_remembered(tmp_path):
    from reelay.store import Store
    store = Store(tmp_path / "s.db")
    assist.add_to_dictionary(store, "Sunnies")
    assert assist.dictionary(store) == {"sunnies"}
    assert not [i for i in assist.builtin_check("cute sunnies", assist.dictionary(store)) if i.kind == "spelling"]
    store.close()


def test_languagetool_is_optional():
    ok, why = assist.languagetool_available()
    if not ok:
        assert "Java" in why or "language_tool_python" in why
    issues, engine = assist.check("the the end")
    assert issues and engine


# ------------------------------------------------------------------ rewriting
def test_ollama_rewrite_with_a_fake_server():
    s = FakeSession()
    s.on("GET", "/api/tags", FakeResponse(200, {"models": [{"name": "llama3.2:3b"}]}), repeat=True)
    s.on("POST", "/api/generate", FakeResponse(200, {"response": 'Here is your caption: "Sunnies for every adventure 😎"'}))
    r = assist.OllamaRewriter("http://127.0.0.1:11434", "llama3.2:3b", session=s)
    assert r.status()[0]
    out = assist.rewrite(r, "sunnies that can keep up with all your adventures", "shorter", "TikTok", 4000)
    assert out == "Sunnies for every adventure 😎"
    sent = s.calls[-1]["json"]
    assert "at most 4000 characters" in sent["system"] and "TikTok" in sent["system"] and sent["stream"] is False
    assert sent["prompt"] == "<caption>\nsunnies that can keep up with all your adventures\n</caption>"


def test_rewriter_explains_what_is_missing(tmp_path):
    s = FakeSession()
    s.on("GET", "/api/tags", FakeResponse(200, {"models": [{"name": "other:1b"}]}))
    ok, why = assist.OllamaRewriter(session=s).status()
    assert not ok and "ollama pull llama3.2:3b" in why
    ok, why = assist.LlamaCppRewriter(str(tmp_path / "missing.gguf")).status()
    assert not ok
    with pytest.raises(assist.AssistError):
        assist.rewrite(None, "x", "fix")


# ------------------------------------------------------------------ SEO
def test_seeds_and_suggestions():
    assert seo.seeds("Sunnies for every adventure", "polarized sunglasses", "beach, summer style",
                     ["polarizedSunglasses", "beachday"])[:3] == ["polarized sunglasses", "beach", "summer style"]
    s = FakeSession()
    s.on("GET", "suggestqueries.google.com", FakeResponse(200, ["polarized sunglasses",
                                                                ["polarized sunglasses", "polarized sunglasses test"]]))
    s.on("GET", "suggestqueries.google.com", FakeResponse(200, ["beach", ["beach day", "polarized sunglasses"]]))
    assert seo.keyword_ideas(["polarized sunglasses", "beach"], s) == \
        ["polarized sunglasses", "polarized sunglasses test", "beach day"]
    assert s.calls[0]["params"]["ds"] == "yt"


def test_score_explains_itself():
    rep = seo.score("Polarized Sunglasses That Keep Up With You", ["sunglasses", "polarized", "beach", "summer", "style"],
                    "Polarized sunglasses for every adventure.", ["polarized sunglasses"])
    assert rep.score == 100 and all(c.ok for c in rep.checks)
    weak = seo.score("WOW!!! MUST SEE", [], "", ["polarized sunglasses"])
    assert weak.score < 40 and any(not c.ok and "Not shouting" == c.label for c in weak.checks)


def test_vidiq_stays_off_without_partner_access(monkeypatch):
    monkeypatch.delenv("REELAY_VIDIQ_API_URL", raising=False)
    ok, why = VidIQService().available()
    assert not ok and "public API" in why
    assert isinstance(seo.provider_from(), seo.BuiltInSEO)
    with pytest.raises(seo.SEOError):
        VidIQService().report("t", [], "", ["x"])


# ------------------------------------------------------------------ in the ticket
@pytest.fixture
def ticket(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.models import Post, Stage
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    items, ids = seed_board(store, window.profile, None)
    row = ids[items[0].key]
    store.save_post(Post(video_row=row, title="Sunnies", description="d", tags=["sunglasses"],
                         tiktok_text="these sunglases are the the best"))
    window.go("board")
    b = window.board
    col = b.columns[Stage.BACKLOG]
    b._on_select(col, next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == row))
    yield window, store, b.panel
    window.close()
    store.close()


def test_assist_menu_fixes_text_in_the_ticket(ticket, monkeypatch):
    from reelay.gui import assist_dialogs
    _window, store, panel = ticket
    block = panel.blocks["tiktok_text"]
    assert block.assist_btn.menu().actions()[0].text() == "Check Spelling && Grammar…"

    def auto_fix(self):
        for issue in list(self.issues):
            if issue.replacements:
                self._fix(issue, issue.replacements[0])
                break
        self._fix(self.issues[0], self.issues[0].replacements[0])
        return assist_dialogs.GrammarDialog.Accepted
    monkeypatch.setattr(assist_dialogs.GrammarDialog, "exec", auto_fix)
    panel._assist(block, "check")
    assert block.text() == "these sunglasses are the best"


def test_rewrite_and_seo_in_the_ticket(ticket, monkeypatch):
    from reelay.gui import assist_dialogs
    from reelay.seo import SEOReport
    _window, store, panel = ticket

    class FakeRewriter:
        name = "fake"

        def status(self):
            return True, "ready"

        def complete(self, prompt):
            return "Shorter caption"

    monkeypatch.setattr(assist, "rewriter_from", lambda s, session=None: FakeRewriter())

    def run_and_accept(self):
        self.result.setPlainText(assist.rewrite(self.rewriter, self.text, "shorter"))
        return assist_dialogs.RewriteDialog.Accepted
    monkeypatch.setattr(assist_dialogs.RewriteDialog, "exec", run_and_accept)
    panel._assist(panel.blocks["tiktok_text"], "rewrite")
    assert panel.blocks["tiktok_text"].text() == "Shorter caption"

    class FakeSEO:
        name = "fake"

        def report(self, title, tags, description, seeds):
            return SEOReport(score=50, keywords=["polarized sunglasses", "sunglasses"])
    monkeypatch.setattr("reelay.seo.provider_from", lambda store=None, session=None: FakeSEO())

    def pick_first(self):
        self._show(self.provider.report("", [], "", []))
        self.ideas.item(0).setCheckState(assist_dialogs.Qt.Checked)
        return assist_dialogs.SEODialog.Accepted
    monkeypatch.setattr(assist_dialogs.SEODialog, "exec", pick_first)
    panel._seo()
    assert panel.blocks["tags"].text() == "sunglasses, polarized sunglasses"
    assert panel.seo_btn.text() == "Get SEO Suggestions"
