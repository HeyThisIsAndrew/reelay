"""Settings → Brand voice: how Repurpose drafts and the AI helper should sound for this creator."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from .. import assist
from ..models import EMOJI_LEVELS, BrandProfile
from .theme import set_prop
from .tips import tip


class BrandVoiceCard(QFrame):
    """One or more voices per profile (e.g. the creator and a brand partner); one is in use."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.store = store
        self.profile_id: int | None = None
        self.brand: BrandProfile | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Brand voice", objectName="DetailTitle"))
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.intro.setProperty("muted", True)
        lay.addWidget(self.intro)
        top = QHBoxLayout()
        self.picker = tip(QComboBox(), "Voices for this profile. The one ticked “Use this voice” guides rewrites.")
        self.picker.setMinimumWidth(220)
        self.picker.currentIndexChanged.connect(self._pick)
        new = tip(QPushButton("New Voice"), "Add another voice, e.g. for a brand partnership")
        new.clicked.connect(self._new)
        self.delete_btn = tip(QPushButton("Delete"), "Delete this voice")
        set_prop(self.delete_btn, "flat")
        self.delete_btn.clicked.connect(self._delete)
        self.import_btn = tip(QPushButton("Import…"), "Add a voice from a Reelay voice file (.json)")
        set_prop(self.import_btn, "flat")
        self.import_btn.clicked.connect(self._import)
        self.export_btn = tip(QPushButton("Export…"), "Save this voice as a file, to use it on another computer "
                                                       "or profile")
        set_prop(self.export_btn, "flat")
        self.export_btn.clicked.connect(self._export)
        top.addWidget(self.picker, 1)
        top.addWidget(new)
        top.addWidget(self.import_btn)
        top.addWidget(self.export_btn)
        top.addWidget(self.delete_btn)
        lay.addLayout(top)
        form = QFormLayout()
        form.setSpacing(8)
        self.name = tip(QLineEdit(), "Whose voice this is, e.g. “Lydia Livingood” or “Blenders × Lydia”")
        self.tone = tip(QLineEdit(), "A few words, e.g. “warm, encouraging, a little playful, never salesy”")
        self.tone.setPlaceholderText("e.g. warm, encouraging, a little playful, never salesy")
        self.avoid = tip(QLineEdit(), "Words or phrases the AI must never use, separated by commas")
        self.avoid.setPlaceholderText("e.g. game-changer, unlock, delve, journey")
        self.hashtags = tip(QLineEdit(), "Hashtags this creator usually uses. Rewrites prefer these over made-up "
                                         "ones.")
        self.hashtags.setPlaceholderText("e.g. #livingood #healing #growth")
        self.guidance = tip(QPlainTextEdit(), "Anything else: who the audience is, emoji habits, a couple of "
                                              "captions that sound exactly right")
        self.guidance.setPlaceholderText("Audience, emoji habits, example captions that sound like you…")
        self.guidance.setFixedHeight(90)
        self.emoji = tip(QComboBox(), "How many emoji drafts may use. “None” removes any the AI adds.")
        self.emoji.addItem("No rule", "")
        self.emoji.addItem("None", "none")
        self.emoji.addItem("Sparing (one or two, never in titles)", "sparing")
        self.emoji.addItem("Natural", "natural")
        self.hashtag_style = tip(QLineEdit(), "How many hashtags and where, e.g. “3 to 5 specific tags, #Shorts "
                                              "first on Shorts, none on Threads”")
        self.hashtag_style.setPlaceholderText("e.g. 3 to 5 specific tags, #Shorts first on Shorts")
        self.cta = tip(QLineEdit(), "How posts end, e.g. “one real question for the comments; never ask to like "
                                    "or subscribe”")
        self.cta.setPlaceholderText("e.g. end with one real question for the comments")
        self.footer = tip(QPlainTextEdit(), "Added exactly as written to the end of every YouTube description "
                                            "Repurpose drafts: socials, gear links, an affiliate note. The AI "
                                            "never writes or changes it.")
        self.footer.setPlaceholderText("Socials, gear and affiliate links for the end of YouTube descriptions")
        self.footer.setFixedHeight(90)
        form.addRow("Name", self.name)
        form.addRow("Preferred tone", self.tone)
        form.addRow("Words to avoid", self.avoid)
        form.addRow("Emoji", self.emoji)
        form.addRow("Default hashtags", self.hashtags)
        form.addRow("Hashtag style", self.hashtag_style)
        form.addRow("Call to action", self.cta)
        form.addRow("YouTube footer", self.footer)
        form.addRow("More guidance", self.guidance)
        lay.addLayout(form)
        brow = QHBoxLayout()
        self.active = tip(QCheckBox("Use this voice"), "The AI helper writes in this voice for this profile")
        self.save_btn = tip(QPushButton("Save Voice"), "Save this voice")
        set_prop(self.save_btn, "primary")
        self.save_btn.clicked.connect(self.save)
        brow.addWidget(self.active)
        brow.addStretch()
        brow.addWidget(self.save_btn)
        lay.addLayout(brow)
        self.status = QLabel()
        self.status.setProperty("faint", True)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.preview = tip(QPushButton("Show What the AI Is Told"), "See the instructions sent to the local model")
        set_prop(self.preview, "flat")
        self.preview.clicked.connect(self._show_prompt)
        lay.addWidget(self.preview)

    def showEvent(self, event) -> None:
        self.refresh()
        super().showEvent(event)

    # ------------------------------------------------------------------ state
    def refresh(self, select_id: int | None = None) -> None:
        active = self.store.get_setting("active_profile")
        self.profile_id = int(active) if active.isdigit() else None
        profile = self.store.get_profile(self.profile_id) if self.profile_id else None
        self.intro.setText(f"How {profile.name if profile else 'this profile'} sounds. Repurpose drafts and the "
                           "Assist ▾ menu's rewrites follow it, so captions sound like you, not like generic AI. "
                           "It's saved on this computer and sent only with the requests you make to the AI "
                           "provider you set up.")
        brands = self.store.list_brand_profiles(self.profile_id) if self.profile_id else []
        keep = select_id if select_id is not None else (self.brand.id if self.brand else None)
        self.picker.blockSignals(True)
        self.picker.clear()
        for b in brands:
            self.picker.addItem(f"{b.name}{'  ✓ in use' if b.active else ''}", b.id)
        if not brands:
            self.picker.addItem("No voices yet: fill in the form and save", None)
        index = self.picker.findData(keep) if keep is not None else -1
        if index < 0:
            index = next((i for i, b in enumerate(brands) if b.active), 0)
        self.picker.setCurrentIndex(index)
        self.picker.blockSignals(False)
        self._pick()

    def _pick(self) -> None:
        brand_id = self.picker.currentData()
        self.brand = self.store.get_brand_profile(brand_id) if brand_id else None
        b = self.brand or BrandProfile(profile_id=self.profile_id or 0, name="", active=True)
        self.name.setText(b.name)
        self.tone.setText(b.tone)
        self.avoid.setText(b.avoid)
        self.hashtags.setText(b.hashtags)
        self.guidance.setPlainText(b.guidance)
        self.emoji.setCurrentIndex(max(0, self.emoji.findData(b.emoji if b.emoji in EMOJI_LEVELS else "")))
        self.hashtag_style.setText(b.hashtag_style)
        self.cta.setText(b.cta)
        self.footer.setPlainText(b.footer)
        self.active.setChecked(b.active)
        self.delete_btn.setEnabled(self.brand is not None)
        self.export_btn.setEnabled(self.brand is not None)
        self.status.setText("")

    def _new(self) -> None:
        self.brand = None
        self.picker.blockSignals(True)
        self.picker.addItem("New voice (not saved)", None)
        self.picker.setCurrentIndex(self.picker.count() - 1)
        self.picker.blockSignals(False)
        for w in (self.name, self.tone, self.avoid, self.hashtags, self.hashtag_style, self.cta):
            w.clear()
        self.guidance.clear()
        self.footer.clear()
        self.emoji.setCurrentIndex(0)
        self.active.setChecked(not self.store.active_brand_profile(self.profile_id) if self.profile_id else True)
        self.delete_btn.setEnabled(False)
        self.name.setFocus()

    def current(self) -> BrandProfile:
        b = self.brand or BrandProfile(profile_id=self.profile_id or 0, name="")
        return BrandProfile(profile_id=b.profile_id, name=self.name.text(), tone=self.tone.text(),
                            avoid=self.avoid.text(), hashtags=self.hashtags.text(),
                            guidance=self.guidance.toPlainText(), emoji=self.emoji.currentData() or "",
                            hashtag_style=self.hashtag_style.text(), cta=self.cta.text(),
                            footer=self.footer.toPlainText(), active=self.active.isChecked(), id=b.id,
                            created_at=b.created_at)

    def save(self) -> None:
        if not self.profile_id:
            return
        brand = self.current()
        if not brand.name.strip():
            self.status.setText("Give the voice a name first.")
            return
        was_active = self.brand.active if self.brand else False
        saved = self.store.save_brand_profile(brand)
        if was_active and not saved.active:
            self.store.set_active_brand_profile(self.profile_id, None)
        self.refresh(select_id=saved.id)
        self.status.setText("Saved. " + ("Rewrites now use this voice." if saved.active else
                                         "Tick “Use this voice” to have rewrites follow it."))

    def _delete(self) -> None:
        if not self.brand:
            return
        if QMessageBox.question(self, "Delete Voice", f"Delete the voice “{self.brand.name}”?") != QMessageBox.Yes:
            return
        self.store.delete_brand_profile(self.brand.id)
        self.brand = None
        self.refresh()

    def _import(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from ..voices import VoiceFileError, import_voice
        if not self.profile_id:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import a Voice", "", "Reelay voice (*.json)")
        if not path:
            return
        try:
            brand = import_voice(self.store, self.profile_id, path)
        except VoiceFileError as exc:
            self.status.setText(str(exc))
            return
        self.refresh(select_id=brand.id)
        self.status.setText(f"Imported “{brand.name}”." + ("" if brand.active else
                                                           " Tick “Use this voice” and save to use it."))

    def _export(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from ..voices import export_voice
        if not self.brand:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Voice", f"{self.brand.name}.json",
                                              "Reelay voice (*.json)")
        if path:
            export_voice(self.brand, path)
            self.status.setText(f"Saved “{self.brand.name}” to {Path(path).name}.")

    def _show_prompt(self) -> None:
        text = assist.system_prompt(self.current()) or "Nothing yet: fill in the form."
        QMessageBox.information(self, "What the AI Is Told", text)
