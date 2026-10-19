"""Slider window modelled on the NVIDIA Control Panel's desktop colour page."""

from __future__ import annotations

import sys

from . import autostart
from .backends import Backend, BackendError
from .colormath import DEFAULT_LEVEL, MAX_LEVEL, MIN_LEVEL
from .config import Config

ALL_DISPLAYS = "__all__"
# Saturated primaries plus skin and earth tones: easy to judge vibrance on.
SWATCHES = [
    "#d62728", "#ff7f0e", "#f2c12e", "#2ca02c", "#17becf",
    "#1f77b4", "#9467bd", "#e377c2", "#e8b89a", "#8c6d4f",
]


def _qt():
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PySide6 import QtCore, QtGui, QtWidgets
        except ImportError:
            return None
    return QtCore, QtGui, QtWidgets


def show_error(message: str) -> None:
    """Report a startup error when launched from the app menu (no terminal to print to)."""
    qt = _qt()
    if qt is None:
        return
    QtWidgets = qt[2]
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    QtWidgets.QMessageBox.critical(None, "Digital Vibrance", message)


def run_gui(backend: Backend, config: Config) -> int:
    qt = _qt()
    if qt is None:
        raise BackendError("the window needs PyQt6: sudo pacman -S python-pyqt6")
    QtCore, QtGui, QtWidgets = qt

    class TestPattern(QtWidgets.QWidget):
        """Reference colours. The whole screen is adjusted, so these change live too."""

        def __init__(self):
            super().__init__()
            self.setMinimumSize(360, 96)

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            rect = self.rect()
            half = rect.height() // 2
            hues = QtGui.QLinearGradient(QtCore.QPointF(rect.left(), 0), QtCore.QPointF(rect.right(), 0))
            for i in range(7):
                hues.setColorAt(i / 6, QtGui.QColor.fromHsvF((i / 6) % 1.0, 0.85, 0.95))
            painter.fillRect(QtCore.QRect(rect.left(), rect.top(), rect.width(), half - 2), hues)
            width = rect.width() / len(SWATCHES)
            for i, colour in enumerate(SWATCHES):
                left = rect.left() + round(i * width)
                right = rect.left() + round((i + 1) * width)
                painter.fillRect(QtCore.QRect(left, rect.top() + half + 2, right - left, rect.height() - half - 2), QtGui.QColor(colour))
            painter.end()

    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.outputs = [o.name for o in backend.outputs()]
            if not self.outputs:
                raise BackendError("no active displays found")
            self.applied: int | None = None

            self.setWindowTitle("Digital Vibrance")
            self.setWindowIcon(QtGui.QIcon.fromTheme("preferences-desktop-color"))
            layout = QtWidgets.QVBoxLayout(self)
            layout.setSpacing(12)

            title = QtWidgets.QLabel("Adjust desktop colour settings")
            font = title.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.2)
            title.setFont(font)
            layout.addWidget(title)

            form = QtWidgets.QFormLayout()
            self.display = QtWidgets.QComboBox()
            if not backend.per_output:
                self.display.addItem("All displays", self.outputs[0])
                self.display.setEnabled(False)
            else:
                if len(self.outputs) > 1:
                    self.display.addItem("All displays", ALL_DISPLAYS)
                for name in self.outputs:
                    self.display.addItem(name, name)
            form.addRow("Display:", self.display)

            self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.slider.setRange(MIN_LEVEL, MAX_LEVEL)
            self.slider.setPageStep(10)
            self.slider.setTickInterval(10)
            self.slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
            self.value = QtWidgets.QLabel()
            self.value.setMinimumWidth(QtGui.QFontMetrics(self.value.font()).horizontalAdvance("100%") + 6)
            self.value.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(self.slider, 1)
            row.addWidget(self.value)
            form.addRow("Digital vibrance:", row)
            layout.addLayout(form)

            layout.addWidget(TestPattern())

            self.notes = QtWidgets.QLabel()
            self.notes.setWordWrap(True)
            self.notes.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.notes.hide()
            layout.addWidget(self.notes)

            self.login = QtWidgets.QCheckBox("Apply at login")
            self.login.setChecked(autostart.is_enabled())
            layout.addWidget(self.login)

            footer = QtWidgets.QHBoxLayout()
            source = QtWidgets.QLabel(backend.label)
            source.setEnabled(False)
            footer.addWidget(source, 1)
            defaults = QtWidgets.QPushButton("Restore Defaults")
            close = QtWidgets.QPushButton("Close")
            footer.addWidget(defaults)
            footer.addWidget(close)
            layout.addLayout(footer)

            # Applying spawns a helper process, so wait until the slider rests briefly.
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.setInterval(150)
            self.timer.timeout.connect(self.apply)

            self.slider.valueChanged.connect(self.on_value)
            self.slider.sliderReleased.connect(self.apply)
            self.display.currentIndexChanged.connect(self.load_level)
            self.login.toggled.connect(self.on_login)
            defaults.clicked.connect(self.restore_defaults)
            close.clicked.connect(self.close)

            self.load_level()

        def targets(self) -> list[str]:
            data = self.display.currentData()
            return self.outputs if data == ALL_DISPLAYS else [data]

        def show_notes(self, lines: list[str], error: bool = False):
            self.notes.setStyleSheet("color: #d03030;" if error else "")
            self.notes.setText("\n".join(lines))
            self.notes.setVisible(bool(lines))

        def load_level(self):
            try:
                level = backend.current_level(self.targets()[0])
            except BackendError as exc:
                level = config.level(backend.id, self.targets()[0])
                self.show_notes([str(exc)], error=True)
            self.slider.blockSignals(True)
            self.slider.setValue(level)
            self.slider.blockSignals(False)
            self.value.setText(f"{level}%")
            self.applied = level

        def on_value(self, level: int):
            self.value.setText(f"{level}%")
            self.timer.start()

        def apply(self):
            self.timer.stop()
            level = self.slider.value()
            if level == self.applied:
                return
            notes: list[str] = []
            try:
                for name in self.targets():
                    notes += backend.apply(name, level)
                    config.set_level(backend.id, name, level)
            except BackendError as exc:
                self.show_notes([str(exc)], error=True)
                return
            finally:
                config.save()
            self.applied = level
            self.show_notes(notes)

        def restore_defaults(self):
            self.slider.setValue(DEFAULT_LEVEL)
            self.apply()

        def on_login(self, enabled: bool):
            if enabled:
                autostart.enable()
                hint = backend.persistence_hint()
                self.show_notes([hint] if hint else [])
            else:
                autostart.disable()

        def closeEvent(self, event):
            if self.timer.isActive():
                self.apply()
            super().closeEvent(event)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("linux-vibrancy")
    app.setDesktopFileName("linux-vibrancy")
    try:
        window = Window()
    except BackendError as exc:
        QtWidgets.QMessageBox.critical(None, "Digital Vibrance", str(exc))
        return 1
    window.show()
    return app.exec()
