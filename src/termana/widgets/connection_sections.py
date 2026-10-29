"""Persistent connection tabs sharing one draft and one Save action."""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QScrollArea, QSizePolicy, QTabBar, QVBoxLayout, QWidget

from termana.i18n import tr
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel


class ConnectionSettingsScrollArea(QScrollArea):
    """Reveal the whole editable field, not only its native input-method caret."""

    def ensureWidgetVisible(self, child, xmargin=50, ymargin=50) -> None:
        super().ensureWidgetVisible(child, xmargin, ymargin)
        content = self.widget()
        if content is None or child is None or not content.isAncestorOf(child):
            return
        position = child.mapTo(self.viewport(), QPoint())
        for start, length, available, scrollbar in (
            (position.x(), child.width(), self.viewport().width(), self.horizontalScrollBar()),
            (position.y(), child.height(), self.viewport().height(), self.verticalScrollBar()),
        ):
            if length <= available:
                offset = min(start, 0) if start < 0 else max(start + length - available, 0)
                scrollbar.setValue(scrollbar.value() + offset)


class ConnectionSettingsTabBar(QTabBar):
    """Keep Profile's full-width anchor while other settings may elide to fit."""

    def minimumTabSizeHint(self, index):
        if index == 0:
            return self.tabSizeHint(index)
        return super().minimumTabSizeHint(index)


class ConnectionSections(QWidget):
    """Keep tabs outside the scroll body; only the visible page contributes size."""

    section_changed = Signal(str)

    def __init__(self, overview: QWidget, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("connectionSections")
        self.overview = overview
        self.current_section = ""
        self.summaries: dict[str, WrappingLabel] = {}
        self.pages: dict[str, QWidget] = {"": overview}
        self._controls: dict[str, QWidget] = {}
        self._groups: dict[str, str] = {}
        self._errors: dict[str, str | None] = {}
        self.tabs = ConnectionSettingsTabBar(self)
        self.tabs.setObjectName("connectionSettingsTabs")
        self.tabs.setAccessibleName(tr("Connection settings"))
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.tabs.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(overview)
        for key, title in (
            ("", tr("Profile")),
            ("ssh", tr("SSH")),
            ("network", tr("Network")),
            ("tunnels", tr("Tunnels")),
        ):
            index = self.tabs.addTab(title)
            self.tabs.setTabData(index, key)
            self.tabs.setTabToolTip(index, title)
            if key:
                page = QWidget(self)
                page.setObjectName(f"connectionSectionPage_{key}")
                layout = QVBoxLayout(page)
                layout.setContentsMargins(12, 16, 12, 14)
                layout.setSpacing(18)
                layout.addStretch()
                self._layout.addWidget(page)
                self.pages[key] = page
                page.hide()
        self._titles = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        self.tabs.currentChanged.connect(self._select_tab)

    def add_section(
        self,
        key: str,
        title: str,
        description: str,
        control: QWidget,
        *,
        group: str,
        show_summary: bool = True,
    ) -> None:
        if key in self._controls or group not in self.pages or not group:
            raise ValueError("Invalid or duplicate connection section")
        self._controls[key] = control
        self._groups[key] = group
        self._errors[key] = None
        section = QWidget(self.pages[group])
        section.setObjectName(f"connectionSection_{key}")
        section.setProperty("connectionSection", True)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(10, 12, 10, 14)
        layout.setSpacing(10)
        heading = WrappingLabel(title, section)
        heading.setObjectName("connectionSectionHeading")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(heading, 1.1, weight=QFont.Weight.DemiBold)
        layout.addWidget(heading)
        # setVisible() below must never show/polish a temporary top-level label.
        # On Windows that can allocate a caption which survives later reparenting.
        summary = WrappingLabel(parent=section)
        summary.setObjectName("connectionSectionSummary")
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        summary.setAccessibleName(title)
        summary.setProperty("showSummary", show_summary)
        summary.setVisible(show_summary)
        self.summaries[key] = summary
        layout.addWidget(summary)
        if description:
            explanation = WrappingLabel(description, section)
            explanation.setTextFormat(Qt.TextFormat.PlainText)
            explanation.setObjectName("mutedText")
            layout.addWidget(explanation)
        layout.addWidget(control)
        page_layout = self.pages[group].layout()
        page_layout.insertWidget(page_layout.count() - 1, section)

    def set_summary(self, key: str, summary: str, *, error: str | None = None) -> None:
        label = self.summaries[key]
        label.setText(error or summary)
        label.setAccessibleDescription(error or summary)
        label.setVisible(bool(label.property("showSummary")) or bool(error))
        if bool(label.property("needsAttention")) != bool(error):
            label.setProperty("needsAttention", bool(error))
            label.style().unpolish(label)
            label.style().polish(label)
            label.update()
        self._errors[key] = error
        group = self._groups[key]
        for index in range(1, self.tabs.count()):
            if self.tabs.tabData(index) == group:
                invalid = any(
                    self._errors[name] for name in self._errors if self._groups[name] == group
                )
                title = self._titles[index]
                caption = f"{title} !" if invalid else title
                self.tabs.setTabText(index, caption)
                self.tabs.setTabToolTip(index, caption)
                self.tabs.setAccessibleTabName(
                    index, f"{title}: {tr('Needs attention')}" if invalid else title
                )
                break

    def set_ssh_available(self, available: bool) -> None:
        if not available and self.current_section:
            self.show_overview()
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, available)

    def _select_tab(self, index: int) -> None:
        key = self.tabs.tabData(index)
        for name, page in self.pages.items():
            page.setVisible(name == key)
        self.current_section = key
        self.updateGeometry()
        self.section_changed.emit(key)

    def open_section(self, key: str, *, focus: QWidget | None = None) -> None:
        group = self._groups.get(key, key)
        if group not in self.pages:
            raise ValueError("Unknown connection section")
        index = next(i for i in range(self.tabs.count()) if self.tabs.tabData(i) == group)
        if not self.tabs.isTabVisible(index):
            return
        if self.tabs.currentIndex() == index:
            # Validation may reveal a control in the already-selected tab.
            # Reapply its expanded state before giving that exact field focus.
            self._select_tab(index)
        else:
            self.tabs.setCurrentIndex(index)
        (focus if focus is not None else self.tabs).setFocus(Qt.FocusReason.OtherFocusReason)

    def show_overview(self) -> None:
        self.open_section("")

    def reveal_control(self, control: QWidget) -> None:
        for key, widget in self._controls.items():
            if widget is control or widget.isAncestorOf(control):
                self.open_section(key, focus=control)
                return
        if self.overview is control or self.overview.isAncestorOf(control):
            self.open_section("", focus=control)
        else:
            # The pinned host/port header is outside the tab pages. Validation
            # must focus it without hiding the user's currently selected settings.
            control.setFocus(Qt.FocusReason.OtherFocusReason)
