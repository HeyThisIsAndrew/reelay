"""Archive search (full-text, including what was said) and brand voice profiles.
No model or platform is contacted."""

from __future__ import annotations

import pytest

from reelay import assist
from reelay.models import BrandProfile, Post, VideoItem
from reelay.store import Store, fts_query
from tests.conftest import FakeResponse, FakeSession
from tests.fakes import disable_publishing, make_world


# ------------------------------------------------------------------ search
def test_v7_database_is_indexed_on_upgrade(tmp_path, monkeypatch):
    import reelay.store as store_module
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 7)
    old = store_module.Store(tmp_path / "v7.db")
    profile = old.create_profile("Lydia", tmp_path / "lib")
    row = old.save_video(profile.id, VideoItem(platform="TikTok", video_id="1", url="u", description="beach day"))
    old.conn.execute("UPDATE videos SET transcript='we love polarized sunglasses' WHERE id=?", (row,))
    old.conn.commit()
    old.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    store = store_module.Store(tmp_path / "v7.db")
    [hit] = store.search(profile.id, "sunglasses")
    assert hit.row_id == row and hit.spoken and "«sunglasses»" in hit.snippet
    assert store.search(profile.id, "beach")[0].row_id == row
    store.close()


def test_index_follows_every_write(tmp_path):
    store = Store(tmp_path / "s.db")
    p = store.create_profile("Lydia", tmp_path / "lib")
    a = store.save_video(p.id, VideoItem(platform="TikTok", video_id="1", url="u", description="first caption"))
    b = store.save_video(p.id, VideoItem(platform="TikTok", video_id="2", url="u", description="second caption"))
    assert {h.row_id for h in store.search(p.id, "caption")} == {a, b}
    store.update_ticket(a, {"transcript": "Grab these sunnies before summer", "topic": "eyewear"})
    store.save_post(Post(video_row=b, title="Beach", tags=["polarized lenses"], tiktok_text="new drop"))
    store.set_notes(b, "film a follow-up")
    assert [h.row_id for h in store.search(p.id, "sunnies")] == [a]
    assert [h.row_id for h in store.search(p.id, "eyewear")] == [a]
    assert [h.row_id for h in store.search(p.id, "lenses drop")] == [b]
    assert [h.row_id for h in store.search(p.id, "follow")] == [b]
    assert store.search(p.id, '"before summer"')[0].spoken and not store.search(p.id, '"summer before"')
    store.set_archived([a], True)
    assert not store.search(p.id, "sunnies") and store.search(p.id, "sunnies", archived=None)
    store.delete_video(b)
    assert not store.search(p.id, "lenses", archived=None)
    assert store.rebuild_search_index() == 1
    store.close()


def test_queries_are_safe():
    assert fts_query('sun-glasses "polarized   lenses" #beach') == \
        '"sun"* AND "glasses"* AND "polarized lenses" AND "beach"*'
    assert fts_query('" ) AND OR NOT (') == '"AND"* AND "OR"* AND "NOT"*'
    assert fts_query("   ") is None and fts_query("!!!") is None


def test_board_search_finds_spoken_words(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.gui.board_page import CardDelegate
    from reelay.paths import db_path
    from reelay.tokens import MemoryTokenStore
    from tests.test_gui import seed_board
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    items, ids = seed_board(store, window.profile, None)
    row = ids[items[1].key]
    store.update_ticket(row, {"transcript": "honestly these polarized sunglasses changed my summer"})
    window.go("board")
    b = window.board
    b.search.setText("polarized sungl")
    b.reload()
    shown = [c for col in b.columns.values() for c in col.model.cards]
    assert [c.id for c in shown] == [row]
    text, _color = CardDelegate._status(shown[0])
    assert text.startswith("🗣 “") and "polarized sunglasses" in text
    b.search.setText("")
    b.reload()
    assert len([c for col in b.columns.values() for c in col.model.cards]) == 3
    window.close()
    store.close()


# ------------------------------------------------------------------ brand voice
def test_one_voice_in_use_per_profile(tmp_path):
    store = Store(tmp_path / "s.db")
    p = store.create_profile("Lydia", tmp_path / "lib")
    other = store.create_profile("Other", tmp_path / "lib2")
    a = store.save_brand_profile(BrandProfile(p.id, "Lydia", tone="warm", active=True))
    b = store.save_brand_profile(BrandProfile(p.id, "Blenders × Lydia", tone="sunny", active=True))
    store.save_brand_profile(BrandProfile(other.id, "Other", active=True))
    assert store.active_brand_profile(p.id).id == b.id and not store.get_brand_profile(a.id).active
    store.set_active_brand_profile(p.id, a.id)
    assert store.active_brand_profile(p.id).id == a.id and store.active_brand_profile(other.id).name == "Other"
    store.set_active_brand_profile(p.id, None)
    assert store.active_brand_profile(p.id) is None
    store.delete_brand_profile(b.id)
    assert [x.name for x in store.list_brand_profiles(p.id)] == ["Lydia"]
    store.close()


BRAND = BrandProfile(1, "Lydia Livingood", tone="warm, encouraging, never salesy", avoid="game-changer, delve",
                     hashtags="#livingood #healing", guidance="Speaks to women rebuilding after hard seasons.")


def test_system_prompt_carries_the_voice():
    system = assist.system_prompt(BRAND)
    assert "Lydia Livingood" in system and "warm, encouraging" in system
    assert "“game-changer”, “delve”" in system and "#livingood #healing" in system
    assert "rebuilding after hard seasons" in system
    assert assist.system_prompt(None) == ""
    assert assist.avoided_words("A real Game-Changer! Let's delve in", BRAND) == ["game-changer", "delve"]
    assert assist.avoided_words("delved", BRAND) == []


def test_rewrite_sends_the_voice_and_retries_banned_words():
    s = FakeSession()
    s.on("POST", "/api/generate", FakeResponse(200, {"response": "This is a game-changer for summer"}))
    s.on("POST", "/api/generate", FakeResponse(200, {"response": "Made for every sunny day"}))
    r = assist.OllamaRewriter(session=s)
    assert assist.rewrite(r, "sunnies for summer", "voice", "TikTok", 4000, brand=BRAND) == "Made for every sunny day"
    first, second = s.calls[-2]["json"], s.calls[-1]["json"]
    assert first["system"] == second["system"] == assist.system_prompt(BRAND)
    assert "brand's own voice" in first["prompt"] and "game-changer" in second["prompt"]
    with pytest.raises(assist.AssistError, match="brand voice"):
        assist.rewrite(r, "x", "voice")


def test_fill_uses_the_voice_hashtags(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, ready=False)
    w.store.save_brand_profile(BrandProfile(w.profile.id, "Lydia", hashtags="#livingood #healing", active=True))
    no_tags = w.rows[0]
    item = w.store.get_video(no_tags).item
    item.description = "no tags here"          # hashtags are read from the caption
    w.store.save_videos(w.profile.id, [item])
    w.pipeline.fill_from_original([no_tags, w.rows[1]])
    assert w.store.get_post(no_tags).hashtags == "#livingood #healing"
    assert w.store.get_post(w.rows[1]).hashtags == "#growth #tag1"        # the original's own tags win
    w.store.close()


def test_settings_card_and_rewrite_dialog(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.gui.assist_dialogs import RewriteDialog
    from reelay.paths import db_path
    from reelay.tokens import MemoryTokenStore
    store = Store(db_path())
    profile = store.create_profile("Lydia", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    window.go("settings")
    card = window.settings_page.brand_card
    card.refresh()
    assert card.picker.currentData() is None and card.active.isChecked()
    card.name.setText("Lydia Livingood")
    card.tone.setText("warm")
    card.avoid.setText("delve")
    card.save()
    brand = store.active_brand_profile(profile.id)
    assert brand.name == "Lydia Livingood" and brand.avoid == "delve" and "✓ in use" in card.picker.currentText()

    class Fake:
        name = "fake"

        def status(self):
            return True, "ready"

    d = RewriteDialog("text", store, "TikTok caption", 4000, len, rewriter=Fake(), brand=brand)
    assert "Lydia Livingood" in d.voice.text()
    assert d.style.findData("voice") >= 0
    d.result.setPlainText("let's delve in")
    assert "avoids: delve" in d.length.text()
    plain = RewriteDialog("text", store, "TikTok caption", 4000, len, rewriter=Fake(), brand=None)
    assert plain.style.findData("voice") < 0
    window.close()
    store.close()
