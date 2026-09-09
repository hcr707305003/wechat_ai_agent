from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ThemePalette:
    name: str
    background: str
    surface: str
    surface_alt: str
    text: str
    text_muted: str
    border: str
    primary: str
    primary_hover: str
    primary_soft: str
    info: str
    success: str
    warning: str
    error: str


LIGHT = ThemePalette(
    name="light",
    background="#F5F6FA",
    surface="#FFFFFF",
    surface_alt="#F0F1F7",
    text="#171821",
    text_muted="#5E6272",
    border="#DCDDE7",
    primary="#6D5CE7",
    primary_hover="#5C4BD5",
    primary_soft="#EEEAFE",
    info="#087F8C",
    success="#17834B",
    warning="#9A6300",
    error="#BE3144",
)

DARK = ThemePalette(
    name="dark",
    background="#15161D",
    surface="#1D1F28",
    surface_alt="#262934",
    text="#F4F4F7",
    text_muted="#B7BAC7",
    border="#3B3E4A",
    primary="#9A8BFF",
    primary_hover="#AA9EFF",
    primary_soft="#302B52",
    info="#64C9D4",
    success="#64CF91",
    warning="#E2B35E",
    error="#FF8291",
)


def resolve_theme(mode: str, *, system_dark: bool) -> ThemePalette:
    if mode == "dark" or (mode == "system" and system_dark):
        return DARK
    return LIGHT


def build_stylesheet(palette: ThemePalette) -> str:
    return f"""
QWidget {{
    color: {palette.text};
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 13px;
}}
QMainWindow, QWidget#root {{ background: {palette.background}; }}
QFrame#customTitleBar, QFrame#sidebar, QFrame#header, QFrame#settingsPanel {{
    background: {palette.surface};
    border: 0;
}}
QFrame#customTitleBar {{ border-bottom: 1px solid {palette.border}; }}
QFrame#sidebar {{ border-right: 1px solid {palette.border}; }}
QFrame#header {{ border-bottom: 1px solid {palette.border}; }}
QFrame#settingsPanel {{ border-left: 1px solid {palette.border}; }}
QLabel#appTitle {{ font-size: 16px; font-weight: 700; }}
QLabel#windowTitleLabel {{ font-size: 13px; font-weight: 600; }}
QLabel#sectionLabel, QLabel#mutedLabel {{ color: {palette.text_muted}; }}
QLabel#conversationTitle {{ font-size: 16px; font-weight: 700; }}
QLabel#avatar {{
    background: {palette.primary_soft};
    color: {palette.primary};
    border-radius: 18px;
    font-weight: 700;
    font-size: 14px;
}}
QLabel#agentBadge {{
    color: {palette.primary};
    background: {palette.primary_soft};
    border-radius: 8px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#replyBadge[enabled="true"] {{ color: {palette.success}; }}
QLabel#replyBadge[enabled="false"] {{ color: {palette.text_muted}; }}
QListWidget#conversationList {{
    background: transparent;
    border: 0;
    outline: 0;
    padding: 4px 6px;
}}
QListWidget#conversationList::item {{
    border-radius: 8px;
    margin: 2px 0;
    padding: 0;
}}
QListWidget#conversationList::item:hover {{ background: {palette.surface_alt}; }}
QListWidget#conversationList::item:selected {{
    background: {palette.primary_soft};
    border-left: 3px solid {palette.primary};
}}
QPushButton {{
    min-height: 32px;
    padding: 0 12px;
    border-radius: 7px;
    border: 1px solid {palette.border};
    background: {palette.surface};
}}
QPushButton:hover {{ background: {palette.surface_alt}; }}
QPushButton:pressed {{ background: {palette.primary_soft}; }}
QPushButton:disabled {{ color: {palette.text_muted}; background: {palette.surface_alt}; }}
QPushButton#primaryButton {{
    color: white;
    background: {palette.primary};
    border-color: {palette.primary};
}}
QPushButton#primaryButton:hover {{ background: {palette.primary_hover}; }}
QPushButton#dangerButton {{
    color: {palette.error};
    border-color: {palette.error};
    background: {palette.surface};
}}
QPushButton#dangerButton:hover {{
    color: white;
    background: {palette.error};
}}
QPushButton#headerDangerButton {{
    color: {palette.text_muted};
    min-width: 48px;
    max-width: 48px;
    min-height: 28px;
    max-height: 28px;
    padding: 0;
    border-color: transparent;
    background: transparent;
}}
QPushButton#headerDangerButton:hover {{
    color: {palette.error};
    border-color: {palette.error};
    background: {palette.surface_alt};
}}
QPushButton#headerDangerButton:pressed {{
    color: white;
    background: {palette.error};
}}
QFrame#queuePanel {{
    background: {palette.surface};
    border-bottom: 1px solid {palette.border};
    padding: 0;
}}
QLabel#queueTitle {{
    color: {palette.text};
    font-weight: 600;
}}
QLabel#queueCount {{
    color: {palette.primary};
    background: {palette.primary_soft};
    border-radius: 7px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 600;
}}
QScrollArea#queueScroll {{
    background: transparent;
    border: 0;
}}
QWidget#queueItems {{ background: transparent; }}
QWidget#queueItem {{
    border-radius: 7px;
    background: {palette.surface_alt};
}}
QWidget#queueItem:hover {{ background: {palette.primary_soft}; }}
QLabel#queueItemText {{ color: {palette.text}; }}
QLabel#queueEmpty {{
    color: {palette.text_muted};
    padding: 4px 6px;
}}
QPushButton#queueDeleteButton {{
    min-width: 42px;
    max-width: 42px;
    min-height: 26px;
    max-height: 26px;
    padding: 0;
    color: {palette.error};
    border-color: transparent;
    background: transparent;
}}
QPushButton#queueDeleteButton:hover {{
    color: white;
    border-color: {palette.error};
    background: {palette.error};
}}
QPushButton#queueClearButton {{
    min-height: 26px;
    padding: 0 10px;
    color: {palette.error};
    border-color: transparent;
    background: transparent;
}}
QPushButton#queueClearButton:hover {{
    color: white;
    border-color: {palette.error};
    background: {palette.error};
}}
QPushButton[titlebarRole] {{
    min-width: 38px;
    max-width: 38px;
    min-height: 36px;
    max-height: 36px;
    padding: 0;
    border-radius: 7px;
    border: 0;
    background: transparent;
}}
QPushButton[titlebarRole]:hover {{ background: {palette.surface_alt}; }}
QPushButton[titlebarRole]:pressed,
QPushButton[titlebarRole="settings"]:checked {{
    color: {palette.primary};
    background: {palette.primary_soft};
}}
QPushButton[titlebarRole="close"]:hover,
QPushButton[titlebarRole="close"]:pressed {{
    color: white;
    background: {palette.error};
}}
QPushButton#companionLauncher {{
    min-width: 44px;
    max-width: 44px;
    min-height: 44px;
    max-height: 44px;
    padding: 0;
    border: 0;
    border-radius: 13px;
    color: white;
    background: {palette.primary};
    font-size: 12px;
    font-weight: 700;
}}
QPushButton#companionLauncher:hover {{ background: {palette.primary_hover}; }}
QPushButton#companionLauncher:pressed {{ background: {palette.primary_soft}; color: {palette.primary}; }}
QCheckBox {{ spacing: 8px; min-height: 30px; }}
QCheckBox::indicator {{
    width: 34px;
    height: 18px;
    border-radius: 9px;
    border: 1px solid {palette.border};
    background: {palette.surface_alt};
}}
QCheckBox::indicator:checked {{
    background: {palette.success};
    border-color: {palette.success};
}}
QSpinBox {{
    min-height: 30px;
    border: 1px solid {palette.border};
    border-radius: 7px;
    background: {palette.surface};
    padding: 0 8px;
}}
QSpinBox:disabled {{ color: {palette.text_muted}; background: {palette.surface_alt}; }}
QScrollArea#timeline {{ background: {palette.background}; border: 0; }}
QWidget#timelineContent {{ background: {palette.background}; }}
QFrame[messageRole="inbound"] {{
    background: {palette.surface};
    border: 1px solid {palette.border};
    border-radius: 10px;
}}
QFrame[messageRole="outbound"] {{
    background: {palette.primary_soft};
    border: 1px solid {palette.primary};
    border-radius: 10px;
}}
QFrame[messageRole="system"] {{
    background: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: 8px;
}}
QFrame[quoteTarget="true"] {{
    border: 2px solid {palette.primary_hover};
    background: {palette.primary_soft};
}}
QLabel#messageMeta {{ color: {palette.text_muted}; font-size: 11px; }}
QFrame#quotePreview {{
    background: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: 7px;
}}
QFrame#quotePreview:hover {{
    background: {palette.surface};
    border-color: {palette.primary};
}}
QFrame#quotePreview:focus {{
    background: {palette.surface};
    border: 2px solid {palette.primary};
}}
QFrame#quoteAccent {{
    background: {palette.primary};
    border: 0;
    border-radius: 1px;
}}
QLabel#quoteSender {{
    color: {palette.primary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#quoteContent {{
    color: {palette.text_muted};
    font-size: 11px;
}}
QLabel#messageImagePreview {{
    color: {palette.text_muted};
    background: {palette.surface_alt};
    border: 1px solid {palette.border};
    border-radius: 8px;
}}
QLabel#messageStatus {{
    color: {palette.info};
    font-size: 11px;
    font-weight: 600;
    padding: 0;
}}
QLabel#messageStatus[statusState="success"] {{ color: {palette.success}; }}
QLabel#messageStatus[statusState="warning"] {{ color: {palette.warning}; }}
QLabel#messageStatus[statusState="error"] {{ color: {palette.error}; }}
QFrame#messageStatusDot {{
    background: {palette.info};
    border: 0;
    border-radius: 3px;
}}
QFrame#messageStatusDot[statusState="success"] {{ background: {palette.success}; }}
QFrame#messageStatusDot[statusState="warning"] {{ background: {palette.warning}; }}
QFrame#messageStatusDot[statusState="error"] {{ background: {palette.error}; }}
QPushButton[messageAction="true"] {{
    color: {palette.text_muted};
    min-height: 26px;
    max-height: 26px;
    padding: 0 9px 0 26px;
    border: 1px solid transparent;
    border-radius: 9px;
    background: {palette.surface_alt};
    font-size: 11px;
}}
QPushButton[messageAction="true"]:hover {{
    color: {palette.primary};
    background: {palette.primary_soft};
    border-color: {palette.primary};
}}
QPushButton[messageAction="true"]:pressed {{
    color: {palette.primary_hover};
    background: {palette.surface_alt};
}}
QLabel#divider {{ color: {palette.text_muted}; padding: 8px; }}
QLabel#emptyTitle {{ font-size: 16px; font-weight: 700; }}
QLabel#statusBanner {{
    color: {palette.info};
    background: {palette.surface};
    border-bottom: 1px solid {palette.border};
    padding: 7px 16px;
}}
QLabel#statusBanner[state="error"] {{ color: {palette.error}; }}
QLabel#statusBanner[state="warning"] {{ color: {palette.warning}; }}
QPushButton#newMessagesBanner {{
    min-height: 28px;
    max-height: 28px;
    padding: 0 12px;
    color: {palette.primary};
    background: {palette.primary_soft};
    border: 0;
    border-radius: 0;
    font-weight: 600;
}}
QPushButton#newMessagesBanner:hover {{
    color: {palette.primary_hover};
    background: {palette.surface_alt};
}}
QToolTip {{
    color: {palette.text};
    background: {palette.surface};
    border: 1px solid {palette.border};
    padding: 4px;
}}
"""
