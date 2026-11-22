"""Dark / Light palettes + Qt stylesheet built around a user accent colour."""
from dataclasses import dataclass

from PySide6.QtGui import QColor

ACCENT_PRESETS = ["#D6006A", "#7C4DFF", "#2979FF", "#00B8D4", "#00C853", "#FF9100", "#FF1744"]


@dataclass
class Palette:
    window: str
    card: str
    header: str
    text: str
    subtext: str
    track: str
    border: str
    chip: str
    hover: str
    accent: str
    success: str = "#2ECC71"
    warn: str = "#FFB300"
    danger: str = "#FF5252"

    def accent_soft(self, alpha: int = 40) -> str:
        c = QColor(self.accent)
        return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def make_palette(theme: str, accent: str) -> Palette:
    if theme == "light":
        return Palette(window="#F2F3F5", card="#FFFFFF", header="#FFFFFF", text="#15161A",
                       subtext="#6B6F7A", track="#E4E6EB", border="#E1E3E8", chip="#EEF0F3",
                       hover="#F5F6F8", accent=accent)
    return Palette(window="#1A1A1A", card="#333333", header="#0B0B0B", text="#FFFFFF",
                   subtext="#A9A9A9", track="#555555", border="#444444", chip="#454545",
                   hover="#3B3B3B", accent=accent)


def stylesheet(p: Palette) -> str:
    return f"""
    QWidget {{ color: {p.text}; font-family: 'Segoe UI', 'Tahoma', 'Segoe Fluent Icons', 'Segoe MDL2 Assets'; font-size: 13px; }}
    QMainWindow, #Root {{ background: {p.window}; }}
    QScrollArea, #ScrollBody {{ background: transparent; border: none; }}
    #Card {{ background: {p.card}; border-radius: 16px; }}
    #Header {{ background: {p.header}; border-radius: 14px; }}
    #Title {{ font-size: 18px; font-weight: 700; }}
    #Version {{ color: {p.subtext}; font-size: 10px; }}
    #Sub, #Muted {{ color: {p.subtext}; font-size: 11px; }}
    #CardCaption {{ color: {p.subtext}; font-size: 11px; letter-spacing: 0.5px; }}
    #SectionTitle {{ font-size: 15px; font-weight: 700; }}
    #DateValue {{ font-size: 15px; font-weight: 700; }}
    #Badge {{ background: {p.chip}; border-radius: 9px; padding: 1px 8px; font-size: 11px; }}
    #LiveBadge {{ background: rgba(46,204,113,30); color: {p.success}; border-radius: 9px;
                  padding: 2px 8px; font-size: 11px; }}
    #OffBadge {{ background: rgba(255,82,82,30); color: #FF5252; border-radius: 9px;
                 padding: 2px 8px; font-size: 11px; }}
    #AppIcon {{ background: {p.accent}; border-radius: 9px; color: white; font-size: 16px; }}
    #IconTile {{ background: {p.chip}; border-radius: 7px; font-size: 14px; }}
    #Divider {{ background: {p.border}; max-height: 1px; min-height: 1px; }}
    QPushButton#Ghost {{ background: transparent; border: 1px solid {p.subtext};
                        border-radius: 15px; padding: 5px 14px; }}
    QPushButton#Ghost:hover {{ border-color: {p.accent}; color: {p.accent}; }}
    QToolButton#IconBtn {{ background: transparent; border: none; font-size: 18px; padding: 4px; }}
    QToolButton#IconBtn:hover {{ color: {p.accent}; }}
    #SectionHeader:hover {{ background: {p.hover}; border-radius: 12px; }}
    #Row {{ border-radius: 10px; }}
    #Row:hover {{ background: {p.accent_soft(30)}; }}
    QMenu {{ background: {p.card}; border: 1px solid {p.border}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {p.accent_soft(60)}; color: {p.text}; }}
    QDialog {{ background: {p.card}; }}
    QLineEdit, QAbstractSpinBox, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{ color: {p.text}; background: {p.window}; border: 1px solid {p.border};
        border-radius: 8px; padding: 6px 8px; selection-background-color: {p.accent}; }}
    QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus {{ border-color: {p.accent}; }}
    QComboBox QAbstractItemView {{ background: {p.card}; selection-background-color: {p.accent}; }}
    QPushButton {{ background: {p.chip}; border: none; border-radius: 8px; padding: 7px 16px; }}
    QPushButton:hover {{ background: {p.hover}; }}
    QPushButton#Primary {{ background: {p.accent}; color: white; font-weight: 600; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ background: transparent; padding: 8px 16px; color: {p.subtext}; }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
    QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {p.accent}; }}
    QCalendarWidget QAbstractItemView {{ background: {p.card}; selection-background-color: {p.accent};
        selection-color: white; }}
    #DevCard {{ background: transparent; border: 1px solid {p.border}; border-radius: 14px; }}
    #DevHeader {{ border-radius: 14px; }}
    #DevHeader:hover {{ background: {p.accent_soft(38)}; border: 1px solid {p.accent}; }}
    #VSep {{ background: {p.border}; min-height: 30px; max-height: 30px; }}
    #DevName {{ font-size: 15px; font-weight: 600; }}
    #DevUsage {{ font-size: 15px; font-weight: 700; }}
    #OnPill {{ background: rgba(46,204,113,32); color: {p.success}; border-radius: 11px; padding: 3px 10px; font-size: 12px; }}
    #OffPill {{ background: {p.chip}; color: {p.subtext}; border-radius: 11px; padding: 3px 10px; font-size: 12px; }}
    #CountBadge {{ border: 1px solid {p.border}; border-radius: 13px; padding: 3px 10px; font-size: 13px; }}
    #Seg {{ background: {p.window}; border-radius: 11px; }}
    QPushButton#SegBtn {{ background: transparent; border-radius: 8px; padding: 7px 14px; color: {p.subtext}; }}
    QPushButton#SegBtn:hover {{ color: {p.text}; }}
    QPushButton#SegBtn:checked {{ background: {p.accent}; color: white; font-weight: 600; }}
    #Tile {{ background: {p.window}; border-radius: 12px; }}
    #TileValue {{ font-size: 17px; font-weight: 700; }}
    #Search {{ border-radius: 10px; padding: 7px 10px; }}
    #Toast {{ background: {p.text}; border-radius: 12px; }}
    #Toast QLabel {{ color: {p.card}; font-weight: 600; }}
    #ToastErr {{ background: {p.danger}; border-radius: 12px; }}
    #ToastErr QLabel {{ color: white; font-weight: 600; }}
    QPushButton#ToastBtn {{ background: transparent; color: {p.accent}; font-weight: 700; padding: 4px 10px; }}
    #ToastErr QPushButton#ToastBtn {{ color: white; }}
    #CapOn {{ color: {p.text}; }}
    #CapOff {{ color: {p.subtext}; }}
    #Hero {{ font-size: 24px; font-weight: 700; }}
    #Lead {{ color: {p.subtext}; font-size: 14px; }}
    #Error {{ color: {p.danger}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; }}
    QScrollBar::handle:vertical {{ background: {p.track}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """
