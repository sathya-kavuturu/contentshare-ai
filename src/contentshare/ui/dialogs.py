"""Small dialogs: pick other content, edit the destination, preview a reply, reject."""

from __future__ import annotations

import contextlib
import logging
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import ActionError, CandidateView, Controller, RequestView
from contentshare.copy_engine import format_bytes
from contentshare.gmail.replies import ReplyDraft
from contentshare.ui.status import confidence_dots
from contentshare.ui.widgets import Label as QLabel

log = logging.getLogger(__name__)


def _buttons(dialog: QDialog, ok_text: str, cancel_text: str = "Cancel") -> QDialogButtonBox:
    box = QDialogButtonBox()
    ok = box.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
    ok.setProperty("primary", True)
    box.addButton(cancel_text, QDialogButtonBox.ButtonRole.RejectRole)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class CandidateDialog(QDialog):
    """Choose which content satisfies the request: the suggestions, or a manual search.

    A department that allows it can also search this whole PC, slowly and in the background:
    it starts by itself when nothing was found in the department's own folders."""

    _pc_looked = Signal(int)  # folders looked at so far (from the search thread)
    _pc_finished = Signal(object, bool, str)  # (matches, whole PC covered, problem)

    def __init__(self, controller: Controller, view: RequestView, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Choose content for request #{view.id}")
        self.resize(760, 480)
        self._controller, self._view = controller, view
        self.chosen: CandidateView | None = None
        self.chosen_path: str | None = None  # a folder or file picked with Browse
        self._browsed: str | None = None
        self._shown: list[CandidateView] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Requested: {view.requested}"))
        row = QHBoxLayout()
        self.query = QLineEdit(placeholderText="Search the storage server by name…")
        self.query.returnPressed.connect(self._search)
        go = QPushButton("Search")
        go.clicked.connect(self._search)
        row.addWidget(self.query, 1)
        row.addWidget(go)
        layout.addLayout(row)
        browse_row = QHBoxLayout()
        browse_row.addWidget(QLabel("Or pick it yourself:"))
        self.browse_folder = QPushButton("Browse for a folder…")
        self.browse_folder.clicked.connect(self._browse_folder)
        self.browse_file = QPushButton("Browse for a file…")
        self.browse_file.clicked.connect(self._browse_file)
        browse_row.addWidget(self.browse_folder)
        browse_row.addWidget(self.browse_file)
        self.pc_button = QPushButton("Search the whole PC")
        self.pc_button.clicked.connect(self._toggle_pc_search)
        self._pc_allowed = controller.pc_search_allowed(view.id)
        self._pc_running = False
        self._pc_stop = threading.Event()
        self.pc_button.setVisible(self._pc_allowed)
        self._pc_looked.connect(self._on_pc_progress)
        self._pc_finished.connect(self._on_pc_finished)
        browse_row.addWidget(self.pc_button)
        browse_row.addStretch(1)
        layout.addLayout(browse_row)
        self.results = QListWidget()
        self.results.setAccessibleName("Matching content")
        self.results.itemDoubleClicked.connect(lambda _i: self.accept())
        layout.addWidget(self.results, 1)
        self.hint = QLabel()
        self.hint.setProperty("role", "muted")
        layout.addWidget(self.hint)
        layout.addWidget(_buttons(self, "Use selected content"))
        self._show(list(view.candidates), "Suggestions from the email. Search to look for others.")
        if self._pc_allowed and not view.candidates:
            self._start_pc_search()  # nothing in the department's folders: look further

    def _search(self) -> None:
        text = self.query.text().strip()
        if not text:
            return
        found = self._controller.search(self._view.id, text)
        self._show(found, f"{len(found)} result(s)" if found else "Nothing found for that name.")
        if not found and self._pc_allowed:
            self._start_pc_search()

    # ------------------------------------------------------------------ the whole-PC search
    def _toggle_pc_search(self) -> None:
        if self._pc_running:
            self._pc_stop.set()
            self.pc_button.setEnabled(False)  # until the search has noticed
        else:
            self._start_pc_search()

    def _start_pc_search(self) -> None:
        if self._pc_running:
            return
        text = self.query.text().strip()
        stop = self._pc_stop = threading.Event()
        self._pc_running = True
        self.pc_button.setEnabled(True)
        self.pc_button.setText("Stop searching")
        self.hint.setText("Not found in the department's folders. Searching this PC…")

        def send(*args: object) -> None:
            with contextlib.suppress(RuntimeError):  # the dialog was closed meanwhile
                self._pc_finished.emit(*args)

        def work() -> None:
            try:
                found, complete = self._controller.search_pc(
                    self._view.id,
                    text,
                    should_stop=stop.is_set,
                    progress=lambda n, _folder: self._emit_looked(n),
                )
            except ActionError as exc:
                send([], True, str(exc))
            except Exception:
                log.exception("whole-PC search failed")
                send([], True, "The search of this PC failed. Details are in the log file.")
            else:
                send(found, complete, "")

        threading.Thread(target=work, name="pc-search", daemon=True).start()

    def _emit_looked(self, count: int) -> None:
        with contextlib.suppress(RuntimeError):
            self._pc_looked.emit(count)

    def _on_pc_progress(self, count: int) -> None:
        if self._pc_running:
            self.hint.setText(f"Searching this PC… {count:,} folders looked at so far.")

    def _on_pc_finished(self, found: list[CandidateView], complete: bool, problem: str) -> None:
        self._pc_running = False
        self.pc_button.setEnabled(True)
        self.pc_button.setText("Search the whole PC")
        if problem:
            self.hint.setText(problem)
            return
        partial = "" if complete else " The search was stopped before the whole PC was covered."
        if found:
            self._show(
                found,
                f"{len(found)} match(es) on this PC. They are only suggestions: check the "
                f"name, then choose one. Its size is measured when you choose it.{partial}",
            )
        else:
            self.hint.setText(f"Nothing like that was found on this PC either.{partial}")

    def done(self, result: int) -> None:
        self._pc_stop.set()  # a search still running has nothing left to show
        super().done(result)

    def _start_folder(self) -> str:
        config = self._controller.config
        dept = next((x for x in config.departments if x.name == self._view.department), None)
        roots = config.roots_for(dept)
        return roots[0] if roots else ""

    def _browse_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the folder to copy", self._start_folder()
        )
        if chosen:
            self._browsed_pick(chosen)

    def _browse_file(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose the file to copy", self._start_folder()
        )
        if chosen:
            self._browsed_pick(chosen)

    def _pick_hint(self) -> str:
        dept = next(
            (x for x in self._controller.config.departments if x.name == self._view.department),
            None,
        )
        if dept is not None and dept.may_pick_anywhere:
            return "Your pick is first. It can be anywhere on this PC or network."
        return "Your pick is first. It must be inside a storage folder in Settings."

    def _browsed_pick(self, chosen: str) -> None:
        self._browsed = chosen.replace("/", "\\")
        self._show(self._shown, self._pick_hint())

    def _show(self, candidates: list[CandidateView], hint: str) -> None:
        self._shown = candidates
        self.results.clear()
        if self._browsed:
            pick = QListWidgetItem(f"You chose:\n    {self._browsed}")
            pick.setData(Qt.ItemDataRole.UserRole, self._browsed)
            self.results.addItem(pick)
        for c in candidates:
            size = (
                "found on this PC"
                if c.item_id is None and not c.size
                else f"{format_bytes(c.size)} · {c.file_count} file(s)"
            )
            item = QListWidgetItem(
                f"{c.path}\n    {size} · {confidence_dots(c.score)}"
                + (f" · {'; '.join(c.notes)}" if c.notes else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, c)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)
        self.hint.setText(hint)

    def accept(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, str):
            self.chosen_path = data
        elif data.item_id is None:  # found on this PC, so not in the index: chosen by its path
            self.chosen_path = data.path
        else:
            self.chosen = data
        super().accept()


class DestinationDialog(QDialog):
    def __init__(self, root: str, current: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Copy to")
        self.resize(640, 140)
        self._root = root
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Must be inside the department folder:\n{root}"))
        row = QHBoxLayout()
        self.path = QLineEdit(current)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addWidget(_buttons(self, "Use this folder"))

    def _browse(self) -> None:
        start = self.path.text() or self._root
        chosen = QFileDialog.getExistingDirectory(self, "Choose the destination folder", start)
        if chosen:
            self.path.setText(chosen.replace("/", "\\"))

    def value(self) -> str:
        return self.path.text().strip()


class ReplyDialog(QDialog):
    """The “content shared” message, editable before it is sent."""

    def __init__(
        self,
        draft: ReplyDraft,
        *,
        summary: str,
        dry_run: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Reply to the requester")
        self.resize(640, 460)
        self._draft = draft
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("To:", QLabel(draft.to_addr))
        form.addRow("Subject:", QLabel(draft.subject))
        layout.addLayout(form)
        self.body = QPlainTextEdit(draft.body)
        self.body.setAccessibleName("Reply text")
        layout.addWidget(self.body, 1)
        note = QLabel(summary)
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        if dry_run:
            banner = QLabel("Dry-run mode is on: nothing can be sent.")
            banner.setObjectName("banner")
            layout.addWidget(banner)
        box = _buttons(self, "Send reply", "Not now")
        if dry_run:
            box.buttons()[0].setEnabled(False)
        layout.addWidget(box)

    def draft(self) -> ReplyDraft:
        return self._draft.with_body(self.body.toPlainText())


class RejectDialog(QDialog):
    def __init__(
        self, view: RequestView, parent: QWidget | None = None, *, summary: str | None = None
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Reject request #{view.id}")
        self.resize(520, 300)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"{view.sender_name or view.sender_email}: {summary or view.requested}")
        )
        self.reason = QLineEdit(placeholderText="Reason (kept in the history)")
        layout.addWidget(self.reason)
        self.notify = QCheckBox("Also send a reply to the requester")
        layout.addWidget(self.notify)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Message to send (leave empty for the standard wording)")
        self.text.setEnabled(False)
        self.notify.toggled.connect(self.text.setEnabled)
        layout.addWidget(self.text, 1)
        layout.addWidget(_buttons(self, "Reject request"))

    def result_values(self) -> tuple[str, bool, str]:
        return self.reason.text().strip(), self.notify.isChecked(), self.text.toPlainText().strip()


class UserNameDialog(QDialog):
    """Asked when the app is opened: who is using it. The name is kept in the History beside
    what this person approves. It cannot be left empty."""

    def __init__(self, current: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Who is using ContentShare?")
        self.resize(460, 190)
        self._had_name = bool(current)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Type your name. The History shows it beside every request you approve, "
                "so the team can see who did what."
            )
        )
        self.name = QLineEdit(current, placeholderText="Your name")
        self.name.setAccessibleName("Your name")
        self.name.setMaxLength(40)
        layout.addWidget(self.name)
        self._box = _buttons(self, "Continue")
        self._ok = self._box.buttons()[0]
        self._keep = self._box.buttons()[1]
        self._keep.setVisible(self._had_name)
        layout.addWidget(self._box)
        self.name.textChanged.connect(self._check)
        self.name.returnPressed.connect(self.accept)
        self._check()

    def _check(self) -> None:
        self._ok.setEnabled(bool(self.name.text().strip()))

    def value(self) -> str:
        return self.name.text().strip()

    def reject(self) -> None:
        """Escape or the close button: fine when a name is already saved, ignored otherwise."""
        if self._had_name:
            super().reject()

    def accept(self) -> None:
        if self.value():
            super().accept()
