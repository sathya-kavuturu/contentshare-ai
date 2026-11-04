"""The menu on the left folds to icons; request cards fold to one short line."""

from __future__ import annotations

from typing import Any

import pytest

from contentshare.config.store import load_config
from contentshare.ui.main_window import SIDEBAR_NARROW, SIDEBAR_WIDE
from contentshare.ui.nav import NavDelegate
from test_ui import Rig, rig  # noqa: F401  (the window fixture)


def _two_requests(rig: Rig) -> None:  # noqa: F811
    rig.mail(message_id="f1", subject="Sunrise Talk 12 Oct 2026", body="English recording please")
    rig.mail(message_id="f2", subject="Quantum Lecture", body="need it")


def _text_of(card: Any) -> str:
    return " ".join(
        label.text() for label in card.findChildren(type(card.status)) if label.isVisibleTo(card)
    )


# --------------------------------------------------------------------------- the menu
def test_the_menu_folds_to_icons_and_opens_again(rig: Rig) -> None:  # noqa: F811
    window = rig.window
    assert not window.sidebar_collapsed and window.side.width() == SIDEBAR_WIDE
    assert not window._brand_name.isHidden()
    assert window.top_toggle.accessibleName() == "Fold the menu"
    window.top_toggle.click()
    assert window.sidebar_collapsed and window.side.width() == SIDEBAR_NARROW
    assert window._brand_name.isHidden()
    assert window.top_toggle.toolTip() == "Show the menu names"
    assert window.top_toggle.accessibleName() == "Show the menu"
    window.top_toggle.click()
    assert not window.sidebar_collapsed and window.side.width() == SIDEBAR_WIDE
    assert not window._brand_name.isHidden()


def test_a_folded_menu_still_switches_screens_and_names_them_in_tooltips(
    rig: Rig,  # noqa: F811
) -> None:
    window = rig.window
    rig.ready_mail()
    window.set_sidebar_collapsed(True)
    window.update_chrome()
    delegate = window.nav.itemDelegate()
    assert isinstance(delegate, NavDelegate) and delegate.collapsed
    assert (
        window.nav.item(0).toolTip() == "Requests (1)"
    )  # the name and count, since only the icon shows
    window.nav.setCurrentRow(3)
    assert window.stack.currentWidget() is window.settings
    window.set_sidebar_collapsed(False)
    assert window.nav.item(0).toolTip() == ""  # the names are on screen again


def test_a_folded_menu_paints_icons_and_a_count_without_errors(rig: Rig) -> None:  # noqa: F811
    rig.ready_mail()
    window = rig.window
    window.set_sidebar_collapsed(True)
    window.update_chrome()
    assert not window.nav.grab().isNull()
    window.set_sidebar_collapsed(False)
    assert not window.nav.grab().isNull()


def test_the_menu_choice_is_remembered_and_restored(rig: Rig) -> None:  # noqa: F811
    rig.window.toggle_sidebar()
    assert rig.controller.config.sidebar_collapsed
    assert load_config(rig.controller._config_path).sidebar_collapsed  # on disk
    from contentshare.ui.main_window import MainWindow

    again = MainWindow(rig.controller, rig.bridge)
    try:
        assert again.sidebar_collapsed and again.side.width() == SIDEBAR_NARROW
    finally:
        again.deleteLater()


def test_the_update_box_is_put_away_while_folded_and_opens_the_menu_when_it_matters(
    rig: Rig,  # noqa: F811
) -> None:
    window = rig.window
    window.set_sidebar_collapsed(True)
    assert window._update_holder.isHidden()
    window._update_found("9.9.9")
    assert not window.sidebar_collapsed and not window._update_holder.isHidden()
    assert rig.controller.config.sidebar_collapsed  # still the saved choice: opened only to show it
    window.set_sidebar_collapsed(True, remember=False)
    window.check_for_updates(
        manual=True
    )  # no updater in this window: nothing to show, nothing opens
    assert window.sidebar_collapsed


def test_changing_a_window_option_does_not_throw_away_unsaved_settings(rig: Rig) -> None:  # noqa: F811
    window = rig.window
    page = window.settings
    page.default_reply.setPlainText("typed but not saved")
    window.set_sidebar_collapsed(True)  # saves a window option, which replaces the config object
    window.nav.setCurrentRow(0)
    window.nav.setCurrentRow(3)
    assert page.default_reply.toPlainText() == "typed but not saved"


def test_a_window_option_cannot_be_used_to_change_other_settings(rig: Rig) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="not a window option"):
        rig.controller.set_ui_option("dry_run", False)


# --------------------------------------------------------------------------- the cards
def test_a_card_folds_to_the_title_the_sender_and_the_department(rig: Rig) -> None:  # noqa: F811
    rig.ready_mail()
    (card,) = rig.cards()
    assert card.fold.text() == "▾" and not card.details.isHidden() and card.summary.isHidden()
    assert "Approve and copy" in " ".join(b.text() for b in card.findChildren(type(card.fold)))
    card.fold.click()
    assert card.state.collapsed and card.fold.text() == "▸"
    assert card.details.isHidden() and not card.summary.isHidden()
    shown = _text_of(card)
    assert "Anita R." in shown and "Media Team" in shown and "Sunrise Talk" in shown
    assert "Subject:" not in shown and "FOUND" not in shown and "anita@dept.org" not in shown
    assert card.fold.accessibleName().startswith("Show details")
    assert card.minimumHeight() < 100  # a short line, not the whole card
    folded_height = card.minimumHeight()
    card.fold.click()
    assert card.minimumHeight() > folded_height + 100  # the list gave the room back
    assert not card.state.collapsed and not card.details.isHidden() and card.summary.isHidden()
    assert card.fold.accessibleName().startswith("Hide details")


def test_folding_keeps_what_was_ticked_and_survives_a_refresh(rig: Rig) -> None:  # noqa: F811
    rig.ready_mail()
    (card,) = rig.cards()
    card.confirm.setChecked(True)
    card.select.setChecked(True)
    card.fold.click()
    rig.window.requests.refresh()  # the list is rebuilt whenever anything changes
    (again,) = rig.cards()
    assert again.state.collapsed and again.details.isHidden()
    assert again.select.isChecked()
    again.fold.click()
    assert again.confirm.isChecked()  # still ticked


def test_one_button_folds_every_request_and_opens_them_all_again(rig: Rig) -> None:  # noqa: F811
    _two_requests(rig)
    page = rig.window.requests
    cards = rig.cards()
    assert len(cards) == 2 and page.fold_all.text() == "Collapse all" and page.fold_all.isEnabled()
    page.fold_all.click()
    assert all(c.state.collapsed for c in cards) and page.fold_all.text() == "Expand all"
    page.fold_all.click()
    assert not any(c.state.collapsed for c in cards) and page.fold_all.text() == "Collapse all"


def test_the_button_follows_cards_folded_one_by_one(rig: Rig) -> None:  # noqa: F811
    _two_requests(rig)
    page = rig.window.requests
    first, second = rig.cards()
    first.fold.click()
    assert page.fold_all.text() == "Collapse all"  # one is still open
    second.fold.click()
    assert page.fold_all.text() == "Expand all"  # now all are folded
    first.fold.click()
    assert page.fold_all.text() == "Collapse all"
    page.fold_all.click()  # something is open: the button folds the rest
    assert all(c.state.collapsed for c in rig.cards())


def test_new_requests_follow_the_last_choice_and_it_is_remembered(rig: Rig) -> None:  # noqa: F811
    rig.ready_mail()
    page = rig.window.requests
    page.fold_all.click()
    assert rig.controller.config.compact_requests
    rig.mail(message_id="f3", subject="Quantum Lecture", body="need it")  # arrives later
    assert len(rig.cards()) == 2 and all(c.state.collapsed for c in rig.cards())
    assert load_config(rig.controller._config_path).compact_requests
    page.fold_all.click()
    assert not rig.controller.config.compact_requests
    assert not any(c.state.collapsed for c in rig.cards())


def test_the_button_is_off_when_there_is_nothing_to_fold(rig: Rig) -> None:  # noqa: F811
    page = rig.window.requests
    assert rig.cards() == [] and not page.fold_all.isEnabled()
    page.toggle_all_collapsed()  # harmless
    assert not page.fold_all.isEnabled()


def test_folded_cards_still_support_selecting_and_bulk_actions(rig: Rig) -> None:  # noqa: F811
    rig.ready_mail()
    page = rig.window.requests
    page.fold_all.click()
    page.select_all.setChecked(True)
    assert all(c.select.isChecked() for c in rig.cards())
    assert page.bulk_skip.isEnabled()


def test_the_menu_icon_at_the_top_folds_and_opens_the_menu(rig: Rig) -> None:  # noqa: F811
    window = rig.window
    assert window.top_toggle.toolTip() == "Fold the menu to icons"
    assert not hasattr(window, "side_toggle")  # no second fold button at the bottom
    window.top_toggle.click()
    assert window.sidebar_collapsed and window.side.width() == SIDEBAR_NARROW
    assert window.top_toggle.accessibleName() == "Show the menu"
    assert not window.top_toggle.isHidden()  # still there to open it again
    window.top_toggle.click()
    assert not window.sidebar_collapsed and window.top_toggle.accessibleName() == "Fold the menu"
