import tkinter as tk
from tkinter import ttk, filedialog, messagebox as tk_messagebox
import json
import subprocess
import threading
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
import base64
import socket
import webbrowser

import dark_messagebox as messagebox  # тёмные messagebox'ы

# Цвета (nekobox-style)
COLOR_BG = "#101421"
COLOR_PANEL = "#151a24"
COLOR_ACCENT = "#00C6FF"
COLOR_TEXT = "#E5E7EB"

GREEN_BTN = "#16a34a"
RED_BTN = "#dc2626"
GRAY_BTN = "#4b5563"

try:
    from PIL import Image, ImageTk
    from pyzbar.pyzbar import decode as qr_decode
    QR_AVAILABLE = True
except Exception:
    Image = None
    ImageTk = None
    qr_decode = None
    QR_AVAILABLE = False

APP_TITLE = "VLF VPN по подписке"
CONFIG_FILE = "vlf_gui_config.json"


class Profile:
    def __init__(self, name, url, ptype="VLESS", address="", remark=""):
        self.name = name
        self.url = url
        self.ptype = ptype      # Тип (VLESS)
        self.address = address  # host:port
        self.remark = remark    # имя/label из #fragment

    def to_dict(self):
        return {
            "name": self.name,
            "url": self.url,
            "ptype": self.ptype,
            "address": self.address,
            "remark": self.remark,
        }

    @staticmethod
    def from_dict(data: dict):
        return Profile(
            data.get("name", "Без имени"),
            data.get("url", ""),
            data.get("ptype", "VLESS"),
            data.get("address", ""),
            data.get("remark", ""),
        )


def decode_subscription_to_vless(sub_bytes: bytes) -> str:
    """
    Берём содержимое подписки и вытаскиваем первую vless:// ссылку.
    Поддерживаем:
      - текст с vless:// строкой;
      - base64 от одной/нескольких ссылок.
    """
    text = sub_bytes.decode("utf-8", errors="ignore").strip()
    if not text:
        raise ValueError("Subscription is empty")

    # 1) прямой vless в тексте
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("vless://"):
            return s

    # 2) base64
    compact = "".join(text.split())
    try:
        decoded = base64.b64decode(compact)
        decoded_text = decoded.decode("utf-8", errors="ignore")
    except Exception as e:
        raise ValueError("Cannot decode subscription as base64") from e

    for line in decoded_text.splitlines():
        s = line.strip()
        if s.startswith("vless://"):
            return s

    raise ValueError("No vless:// URL in subscription")


def build_singbox_config(vless_url: str, ru_mode: bool, site_excl, app_excl):
    """На основе одной VLESS-ссылки собираем config.json для sing-box (логика из рабочего файла)."""
    u = urlparse(vless_url)
    if u.scheme != "vless":
        raise ValueError("Not a vless:// URL")

    uuid = u.username or ""
    server = u.hostname or ""
    port = u.port or 443

    q = parse_qs(u.query)

    def qget(key, default=""):
        vals = q.get(key)
        return vals[0] if vals else default

    flow = qget("flow", "")
    security = qget("security", "")
    fp = qget("fp", "") or "chrome"
    pbk = qget("pbk", "")
    sid = qget("sid", "")
    sni = qget("sni", "") or server
    network = qget("type", "tcp")

    tls = {
        "enabled": True,
        "server_name": sni,
        "utls": {"enabled": True, "fingerprint": fp},
    }
    if security == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": pbk,
            "short_id": sid,
        }

    outbound_proxy = {
        "type": "vless",
        "tag": "proxy-out",
        "server": server,
        "server_port": port,
        "uuid": uuid,
        "network": network,
        "tls": tls,
    }
    if flow:
        outbound_proxy["flow"] = flow

    outbound_direct = {"type": "direct", "tag": "direct"}
    outbound_dns = {"type": "dns", "tag": "dns-out"}
    outbound_block = {"type": "block", "tag": "block"}

    # Базовые правила маршрутизации — как в рабочем варианте
    rules = [
        {"protocol": "dns", "outbound": "dns-out"},
    ]

    # всегда не заворачиваем сам сервер через себя же
    try:
        server_ip = socket.gethostbyname(server)
        rules.append({"ip_cidr": [f"{server_ip}/32"], "outbound": "direct"})
    except Exception:
        pass

    # RU-режим
    if ru_mode:
        rules.append(
            {"domain_suffix": ["ru", "su", "рф"], "outbound": "direct"}
        )

    # Исключения по доменам
    if site_excl:
        rules.append({"domain": site_excl, "outbound": "direct"})

    # Исключения по процессам
    for name in app_excl:
        rules.append({"process_name": name, "outbound": "direct"})

    route = {
        "auto_detect_interface": True,
        "rules": rules,
        "final": "proxy-out",
    }

    # DNS как в старом рабочем файле:
    # 1.1.1.1, через direct, без DoH и без detour на proxy-out
    dns = {
        "servers": [
            {
                "tag": "dns-direct",
                "address": "1.1.1.1",
                "address_strategy": "prefer_ipv4",
                "detour": "direct",
            }
        ]
    }

    # TUN в старом формате (inet4_address) + авто-маршрутизация
    inbound_tun = {
        "type": "tun",
        "tag": "tun-in",
        "interface_name": "vlf_tun",
        "mtu": 1500,
        "inet4_address": "172.19.0.1/28",
        "auto_route": True,
        "strict_route": True,
        "sniff": True,
    }

    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": dns,
        "inbounds": [inbound_tun],
        "outbounds": [
            outbound_proxy,
            outbound_direct,
            outbound_dns,
            outbound_block,
        ],
        "route": route,
    }
    return config


class VlfGui(tk.Tk):
    def __init__(self):
        super().__init__()

        # База для sing-box.exe и config.json:
        #   - в собранном .exe → sys._MEIPASS (PyInstaller)
        #   - в исходниках → папка, где лежит vlf_gui.py
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            self.base_dir = Path(sys._MEIPASS)
        else:
            self.base_dir = Path(__file__).resolve().parent

        icon_path = self.base_dir / "vlf.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.title(APP_TITLE)
        self.geometry("820x620")
        self.resizable(False, False)

        self.config_data = {
            "profiles": [],
            "ru_mode": True,
            "site_exclusions": [],
            "app_exclusions": [],
        }
        self.current_profile_index = None

        self.proc: subprocess.Popen | None = None
        self.log_thread: threading.Thread | None = None
        self.stop_log = threading.Event()

        # Переменные для инфо по профилю
        self.profile_type_var = tk.StringVar(value="")
        self.profile_addr_var = tk.StringVar(value="")
        self.profile_name_var = tk.StringVar(value="")

        # Новый вар для IP
        self.ip_var = tk.StringVar(value="IP: -")

        self._build_ui()
        self._load_config()
        self._refresh_profiles_ui()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- конфиг GUI ----------

    def _load_config(self):
        # vlf_gui_config.json храним рядом с EXE (текущий рабочий каталог)
        path = Path(CONFIG_FILE)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.config_data.update(data)
        except Exception:
            pass

    def _save_config(self):
        try:
            Path(CONFIG_FILE).write_text(
                json.dumps(self.config_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---------- UI helpers ----------

    def _create_pill_button(self, parent, text, bg, command=None, state="normal"):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=COLOR_TEXT,
            activebackground=bg,
            activeforeground=COLOR_TEXT,
            borderwidth=0,
            highlightthickness=0,
            padx=20,
            pady=6,
            font=("Segoe UI", 10, "bold"),
            relief="flat",
        )
        btn.configure(state=state)
        return btn

    def _create_icon_button(self, parent, text, command=None):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            activebackground="#1f2933",
            activeforeground=COLOR_TEXT,
            relief="flat",
            bd=0,
            width=3,
            height=1,
            highlightthickness=0,
            font=("Segoe UI", 9, "bold"),
        )
        return btn

    # ---------- построение UI ----------

    def _build_ui(self):
        self.configure(bg=COLOR_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background=COLOR_BG)
        style.configure("Header.TFrame", background=COLOR_BG)
        style.configure(
            "Panel.TLabelframe",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
            borderwidth=0,
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
        )
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure(
            "Accent.TButton",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
            padding=4,
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#1f2933")],
            foreground=[("disabled", "#6b7280")],
        )

        main = ttk.Frame(self, padding=10, style="TFrame")
        main.pack(fill="both", expand=True)

        # ---- ШАПКА ----
        header = ttk.Frame(main, style="Header.TFrame")
        header.pack(fill="x")

        left_header = tk.Frame(header, bg=COLOR_BG)
        left_header.pack(side="left", padx=0, pady=4)

        # скрытая кнопка-тоггл (оставляем для логики)
        self.toggle_var = tk.StringVar(value="Подключить")
        self.toggle_btn = ttk.Button(
            left_header,
            textvariable=self.toggle_var,
            command=self.on_toggle,
            style="Accent.TButton",
        )

        # большие кнопки
        self.btn_tun_on = self._create_pill_button(
            left_header, "Туннель ВКЛ", GREEN_BTN, command=self.connect
        )
        self.btn_tun_on.pack(side="left", padx=(0, 8))

        self.btn_tun_off = self._create_pill_button(
            left_header,
            "ВЫКЛ",
            RED_BTN,
            command=self.disconnect,
            state="disabled",
        )
        self.btn_tun_off.pack(side="left", padx=(0, 8))

        def proxy_msg():
            messagebox.showinfo(
                APP_TITLE,
                "Режим 'Без TUN (прокси)' пока не реализован.\n"
                "Сейчас работает только режим TUN.",
            )

        self.btn_proxy = self._create_pill_button(
            left_header,
            "Без TUN (прокси)",
            GRAY_BTN,
            command=proxy_msg,
        )
        self.btn_proxy.pack(side="left")

        # Правая часть шапки – просто надпись VLF
        right_header = tk.Frame(header, bg=COLOR_BG)
        right_header.pack(side="right", padx=0, pady=4)

        self.logo_label = tk.Label(
            right_header,
            text="VLF",
            bg=COLOR_BG,
            fg=COLOR_ACCENT,
            font=("Segoe UI", 18, "bold"),
        )
        self.logo_label.pack(anchor="center", pady=(0, 2))

        self.bot_label = tk.Label(
            right_header,
            text="@vlftunAT_bot",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        self.bot_label.pack(anchor="center", pady=(2, 0))

        # кликабельный телеграм
        self.bot_label.bind(
            "<Button-1>",
            lambda e: webbrowser.open_new("https://t.me/vlftunAT_bot"),
        )

        # ---- Статус ----
        status_frame = ttk.Frame(main, style="TFrame")
        status_frame.pack(fill="x", pady=(0, 4))

        self.status_var = tk.StringVar(value="отключен")
        self.status_lbl = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            style="Status.TLabel",
            anchor="center",
        )
        self.status_lbl.pack()

        # новый лейбл IP под статусом
        self.ip_lbl = ttk.Label(
            status_frame,
            textvariable=self.ip_var,
            style="Status.TLabel",
            anchor="center",
        )
        self.ip_lbl.pack()

        # ---- Центр: профили + исключения ----
        center = ttk.Frame(main, style="TFrame")
        center.pack(fill="both", expand=True)

        # ЛЕВЫЙ БЛОК: профили
        left_panel = ttk.Labelframe(
            center, text="Профили", style="Panel.TLabelframe"
        )
        left_panel.pack(
            side="left", fill="both", expand=True, padx=(0, 6), pady=4
        )

        prof_top = ttk.Frame(left_panel, style="TFrame")
        prof_top.pack(fill="x", padx=8, pady=(6, 4))

        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(
            prof_top,
            textvariable=self.profile_var,
            state="readonly",
        )
        self.profile_combo.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Button(
            prof_top,
            text="Добавить",
            style="Accent.TButton",
            command=self.on_add_profile,
        ).pack(side="left", padx=2)
        ttk.Button(
            prof_top,
            text="Изменить",
            style="Accent.TButton",
            command=self.on_edit_profile,
        ).pack(side="left", padx=2)
        ttk.Button(
            prof_top,
            text="Удалить",
            style="Accent.TButton",
            command=self.on_delete_profile,
        ).pack(side="left", padx=2)

        prof_list_wrap = tk.Frame(left_panel, bg=COLOR_PANEL)
        prof_list_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self.profile_list = tk.Listbox(
            prof_list_wrap,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            selectbackground=COLOR_ACCENT,
            selectforeground="#000000",
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            exportselection=False,
        )
        self.profile_list.pack(fill="both", expand=True, padx=1, pady=1)
        self.profile_list.bind("<<ListboxSelect>>", self.on_profile_list_select)

        # Информация о выбранном профиле
        info_frame = tk.Frame(left_panel, bg=COLOR_PANEL)
        info_frame.pack(fill="x", padx=8, pady=(0, 8))

        def info_label(row, text, var):
            tk.Label(
                info_frame,
                text=text,
                bg=COLOR_PANEL,
                fg="#9ca3af",
                font=("Segoe UI", 9),
            ).grid(row=row, column=0, sticky="w")
            tk.Label(
                info_frame,
                textvariable=var,
                bg=COLOR_PANEL,
                fg=COLOR_TEXT,
                font=("Segoe UI", 9, "bold"),
            ).grid(row=row, column=1, sticky="w", padx=(4, 0))

        info_label(0, "Тип:", self.profile_type_var)
        info_label(1, "Адрес:", self.profile_addr_var)
        info_label(2, "Имя:", self.profile_name_var)

        # ПРАВЫЙ БЛОК: исключения
        right_panel = ttk.Labelframe(
            center, text="Исключения", style="Panel.TLabelframe"
        )
        right_panel.pack(
            side="left", fill="both", expand=True, padx=(6, 0), pady=4
        )

        exc_top = ttk.Frame(right_panel, style="TFrame")
        exc_top.pack(fill="x", padx=8, pady=(6, 4))

        ttk.Button(
            exc_top,
            text="Добавить сайт",
            style="Accent.TButton",
            command=self.on_add_site,
        ).pack(side="left", padx=2)
        ttk.Button(
            exc_top,
            text="Добавить программу",
            style="Accent.TButton",
            command=self.on_add_app,
        ).pack(side="left", padx=2)

        # Сайты
        sites_frame = tk.Frame(right_panel, bg=COLOR_PANEL)
        sites_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        sites_left = tk.Frame(sites_frame, bg=COLOR_PANEL)
        sites_left.pack(side="left", fill="both", expand=True)

        tk.Label(
            sites_left,
            text="Сайты",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).pack(anchor="w", pady=(0, 2))

        self.site_list = tk.Listbox(
            sites_left,
            height=4,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            selectbackground=COLOR_ACCENT,
            selectforeground="#000000",
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            exportselection=False,
        )
        self.site_list.pack(fill="both", expand=True, padx=1, pady=1)

        site_btns = tk.Frame(sites_frame, bg=COLOR_PANEL)
        site_btns.pack(side="left", fill="y", padx=(4, 0))
        self._create_icon_button(site_btns, "✎", self.on_edit_site).pack(pady=2)
        self._create_icon_button(site_btns, "✖", self.on_delete_site).pack(pady=2)

        # Приложения
        apps_frame = tk.Frame(right_panel, bg=COLOR_PANEL)
        apps_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        apps_left = tk.Frame(apps_frame, bg=COLOR_PANEL)
        apps_left.pack(side="left", fill="both", expand=True)

        tk.Label(
            apps_left,
            text="Программы",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).pack(anchor="w", pady=(0, 2))

        self.app_list = tk.Listbox(
            apps_left,
            height=4,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            selectbackground=COLOR_ACCENT,
            selectforeground="#000000",
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            exportselection=False,
        )
        self.app_list.pack(fill="both", expand=True, padx=1, pady=1)

        app_btns = tk.Frame(apps_frame, bg=COLOR_PANEL)
        app_btns.pack(side="left", fill="y", padx=(4, 0))
        self._create_icon_button(app_btns, "✎", self.on_edit_app).pack(pady=2)
        self._create_icon_button(app_btns, "✖", self.on_delete_app).pack(pady=2)

        # Режим РФ
        rf_frame = tk.Frame(right_panel, bg=COLOR_PANEL)
        rf_frame.pack(fill="x", padx=8, pady=(4, 8))

        self.ru_mode_var = tk.BooleanVar(value=True)
        self.ru_toggle = tk.Checkbutton(
            rf_frame,
            text="Режим РФ: русские сайты напрямую",
            variable=self.ru_mode_var,
            command=self.on_ru_mode_changed,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            activebackground=COLOR_PANEL,
            activeforeground=COLOR_TEXT,
            selectcolor=COLOR_PANEL,
            highlightthickness=0,
            bd=0,
            anchor="w",
        )
        self.ru_toggle.pack(anchor="w")

        # ---- ЛОГ ----
        log_frame = ttk.Labelframe(
            main, text="Лог sing-box", style="Panel.TLabelframe"
        )
        log_frame.pack(fill="both", expand=True, pady=(6, 0))

        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            bg="#0b0f1a",
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
            borderwidth=0,
        )
        self.log_text.pack(
            side="left", fill="both", expand=True, padx=(6, 0), pady=6
        )

        log_scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        log_scroll.pack(side="right", fill="y", pady=6)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.set_status("отключен", "red")

    # ---------- helpers ----------

    def append_log(self, text: str):
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def set_status(self, text: str, color: str):
        self.status_var.set(text)
        self.status_lbl.configure(foreground=color)

    def _update_ip_async(self):
        """Обновить IP в отдельном потоке, чтобы не блокировать GUI."""
        def worker():
            ip = "-"
            try:
                with urllib.request.urlopen(
                    "https://api.ipify.org?format=text", timeout=10
                ) as r:
                    ip = r.read().decode().strip()
            except Exception:
                pass
            self.after(0, lambda: self.ip_var.set(f"IP: {ip}"))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- profiles ----------

    def _get_profiles(self):
        return [Profile.from_dict(p) for p in self.config_data.get("profiles", [])]

    def _set_profiles(self, profiles):
        self.config_data["profiles"] = [p.to_dict() for p in profiles]
        self._save_config()
        self._refresh_profiles_ui()

    def _refresh_profile_info_ui(self):
        profiles = self._get_profiles()
        if (
            self.current_profile_index is not None
            and 0 <= self.current_profile_index < len(profiles)
        ):
            p = profiles[self.current_profile_index]
            self.profile_type_var.set(p.ptype or "VLESS")
            self.profile_addr_var.set(p.address or "")
            self.profile_name_var.set(p.remark or "")
        else:
            self.profile_type_var.set("")
            self.profile_addr_var.set("")
            self.profile_name_var.set("")

    def _refresh_profiles_ui(self):
        profiles = self._get_profiles()
        names = [p.name for p in profiles]

        self.profile_combo["values"] = names

        if profiles:
            if (
                self.current_profile_index is None
                or self.current_profile_index >= len(profiles)
            ):
                self.current_profile_index = 0
            self.profile_combo.current(self.current_profile_index)
            self.profile_var.set(profiles[self.current_profile_index].name)
        else:
            self.current_profile_index = None
            self.profile_combo.set("")
            self.profile_var.set("")

        self.profile_list.delete(0, "end")
        for n in names:
            self.profile_list.insert("end", n)
        if self.current_profile_index is not None and profiles:
            idx = self.current_profile_index
            if idx < len(profiles):
                self.profile_list.selection_clear(0, "end")
                self.profile_list.selection_set(idx)
                self.profile_list.see(idx)

        self._refresh_exclusions_ui()
        self._refresh_profile_info_ui()

    def on_profile_selected(self, event=None):
        idx = self.profile_combo.current()
        self.current_profile_index = idx if idx >= 0 else None
        if self.current_profile_index is not None:
            self.profile_list.selection_clear(0, "end")
            self.profile_list.selection_set(self.current_profile_index)
            self.profile_list.see(self.current_profile_index)
        self._refresh_profile_info_ui()

    def on_profile_list_select(self, event=None):
        if not self.profile_list.curselection():
            return
        idx = self.profile_list.curselection()[0]
        profiles = self._get_profiles()
        if idx >= len(profiles):
            return
        self.current_profile_index = idx
        self.profile_combo.current(idx)
        self.profile_var.set(profiles[idx].name)
        self._refresh_profile_info_ui()

    def _profile_dialog(self, title, profile: Profile | None = None):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg=COLOR_BG)

        # Имя
        tk.Label(
            dialog,
            text="Название профиля:",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(8, 2))

        name_var = tk.StringVar(value=profile.name if profile else "")
        name_entry = tk.Entry(
            dialog,
            textvariable=name_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        )
        name_entry.pack(fill="x", padx=8, pady=(0, 8))

        # URL
        tk.Label(
            dialog,
            text="Ссылка-подписка:",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(0, 2))

        url_var = tk.StringVar(value=profile.url if profile else "")
        url_entry = tk.Entry(
            dialog,
            textvariable=url_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        )
        url_entry.pack(fill="x", padx=8, pady=(0, 8))

        btn_row = tk.Frame(dialog, bg=COLOR_BG)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))

        res = {"ok": False}

        def on_ok():
            name = name_var.get().strip()
            url = url_var.get().strip()
            if not name:
                messagebox.showerror(APP_TITLE, "Название профиля не может быть пустым.")
                return
            if not url:
                messagebox.showerror(APP_TITLE, "Нужна ссылка-подписка.")
                return
            res["ok"] = True
            res["name"] = name
            res["url"] = url
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        # Кнопка QR только если библиотеки реально есть
        if QR_AVAILABLE:
            def load_qr():
                path = filedialog.askopenfilename(
                    title="Выбери картинку с QR-кодом",
                    filetypes=[
                        (
                            "Images",
                            "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp",
                        ),
                        ("All files", "*.*"),
                    ],
                )
                if not path:
                    return
                try:
                    img = Image.open(path)
                    codes = qr_decode(img)
                    if not codes:
                        messagebox.showerror(
                            APP_TITLE, "QR-код не найден на этой картинке."
                        )
                        return
                    data = codes[0].data.decode("utf-8")
                    url_var.set(data)
                except Exception as e:
                    messagebox.showerror(APP_TITLE, f"Ошибка чтения QR: {e}")

            self._create_pill_button(btn_row, "Из QR", GRAY_BTN, load_qr).pack(
                side="left", padx=(0, 8)
            )

        self._create_pill_button(btn_row, "Отмена", GRAY_BTN, on_cancel).pack(
            side="right", padx=(4, 0)
        )
        self._create_pill_button(btn_row, "OK", GREEN_BTN, on_ok).pack(
            side="right"
        )

        # Центруем диалог относительно окна
        dialog.update_idletasks()
        win_w = dialog.winfo_width()
        win_h = dialog.winfo_height()
        root_x = self.winfo_rootx()
        root_y = self.winfo_rooty()
        root_w = self.winfo_width()
        root_h = self.winfo_height()
        x = root_x + (root_w - win_w) // 2
        y = root_y + (root_h - win_h) // 2
        dialog.geometry(f"+{x}+{y}")

        dialog.wait_window()
        if res["ok"]:
            return Profile(res["name"], res["url"])
        return None

    def on_add_profile(self):
        p = self._profile_dialog("Новый профиль")
        if not p:
            return
        profiles = self._get_profiles()
        profiles.append(p)
        self._set_profiles(profiles)
        self.current_profile_index = len(profiles) - 1
        self._refresh_profiles_ui()

    def on_edit_profile(self):
        profiles = self._get_profiles()
        if (
            self.current_profile_index is None
            or self.current_profile_index >= len(profiles)
        ):
            messagebox.showerror(APP_TITLE, "Сначала выбери профиль.")
            return
        orig = profiles[self.current_profile_index]
        p = self._profile_dialog("Редактирование профиля", orig)
        if not p:
            return
        p.ptype = orig.ptype
        p.address = orig.address
        p.remark = orig.remark
        profiles[self.current_profile_index] = p
        self._set_profiles(profiles)

    def on_delete_profile(self):
        profiles = self._get_profiles()
        if (
            self.current_profile_index is None
            or self.current_profile_index >= len(profiles)
        ):
            messagebox.showerror(APP_TITLE, "Сначала выбери профиль.")
            return

        # dark_messagebox может не иметь askyesno — подстрахуемся стандартным
        try:
            answer = messagebox.askyesno(
                APP_TITLE, "Удалить выбранный профиль?"
            )
        except AttributeError:
            answer = tk_messagebox.askyesno(
                APP_TITLE, "Удалить выбранный профиль?"
            )

        if not answer:
            return

        del profiles[self.current_profile_index]
        self.current_profile_index = 0 if profiles else None
        self._set_profiles(profiles)

    # ---------- exclusions ----------

    def _refresh_exclusions_ui(self):
        self.ru_mode_var.set(self.config_data.get("ru_mode", True))

        self.site_list.delete(0, "end")
        for d in self.config_data.get("site_exclusions", []):
            self.site_list.insert("end", d)

        self.app_list.delete(0, "end")
        for p in self.config_data.get("app_exclusions", []):
            self.app_list.insert("end", p)

    def on_ru_mode_changed(self):
        self.config_data["ru_mode"] = bool(self.ru_mode_var.get())
        self._save_config()

    def on_add_site(self):
        self._edit_site_dialog()

    def on_edit_site(self):
        try:
            idx = self.site_list.curselection()[0]
        except IndexError:
            messagebox.showerror(APP_TITLE, "Выбери сайт в списке.")
            return
        current = self.config_data.get("site_exclusions", [])[idx]
        self._edit_site_dialog(idx, current)

    def _edit_site_dialog(self, index=None, current=""):
        dialog = tk.Toplevel(self)
        dialog.title("Сайт-исключение")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=COLOR_BG)

        tk.Label(
            dialog,
            text="Домен (example.com, bank.ru и т.п.):",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(8, 2))
        var = tk.StringVar(value=current)
        tk.Entry(
            dialog,
            textvariable=var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        ).pack(fill="x", padx=8, pady=(0, 8))

        res = {"ok": False}

        def on_ok():
            v = var.get().strip()
            if not v:
                messagebox.showerror(APP_TITLE, "Домен не может быть пустым.")
                return
            res["ok"] = True
            res["value"] = v
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        btns = tk.Frame(dialog, bg=COLOR_BG)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        self._create_pill_button(btns, "OK", GREEN_BTN, on_ok).pack(
            side="right", padx=(4, 0)
        )
        self._create_pill_button(btns, "Отмена", GRAY_BTN, on_cancel).pack(
            side="right"
        )

        dialog.update_idletasks()
        win_w = dialog.winfo_width()
        win_h = dialog.winfo_height()
        root_x = self.winfo_rootx()
        root_y = self.winfo_rooty()
        root_w = self.winfo_width()
        root_h = self.winfo_height()
        x = root_x + (root_w - win_w) // 2
        y = root_y + (root_h - win_h) // 2
        dialog.geometry(f"+{x}+{y}")

        dialog.wait_window()
        if not res["ok"]:
            return

        lst = self.config_data.get("site_exclusions", [])
        if index is None:
            lst.append(res["value"])
        else:
            lst[index] = res["value"]
        self.config_data["site_exclusions"] = lst
        self._save_config()
        self._refresh_exclusions_ui()

    def on_delete_site(self):
        try:
            idx = self.site_list.curselection()[0]
        except IndexError:
            messagebox.showerror(APP_TITLE, "Выбери сайт в списке.")
            return
        lst = self.config_data.get("site_exclusions", [])
        if idx >= len(lst):
            return
        del lst[idx]
        self.config_data["site_exclusions"] = lst
        self._save_config()
        self._refresh_exclusions_ui()

    def on_add_app(self):
        self._edit_app_dialog()

    def on_edit_app(self):
        try:
            idx = self.app_list.curselection()[0]
        except IndexError:
            messagebox.showerror(APP_TITLE, "Выбери программу в списке.")
            return
        current = self.config_data.get("app_exclusions", [])[idx]
        self._edit_app_dialog(idx, current)

    def _edit_app_dialog(self, index=None, current=""):
        dialog = tk.Toplevel(self)
        dialog.title("Программа-исключение")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=COLOR_BG)

        tk.Label(
            dialog,
            text="Имя процесса (например, bankclient.exe):",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(8, 2))
        var = tk.StringVar(value=current)
        tk.Entry(
            dialog,
            textvariable=var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        ).pack(fill="x", padx=8, pady=(0, 8))

        res = {"ok": False}

        def on_ok():
            v = var.get().strip()
            if not v:
                messagebox.showerror(
                    APP_TITLE, "Имя процесса не может быть пустым."
                )
                return
            res["ok"] = True
            res["value"] = v
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        btns = tk.Frame(dialog, bg=COLOR_BG)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        self._create_pill_button(btns, "OK", GREEN_BTN, on_ok).pack(
            side="right", padx=(4, 0)
        )
        self._create_pill_button(btns, "Отмена", GRAY_BTN, on_cancel).pack(
            side="right"
        )

        dialog.update_idletasks()
        win_w = dialog.winfo_width()
        win_h = dialog.winfo_height()
        root_x = self.winfo_rootx()
        root_y = self.winfo_rooty()
        root_w = self.winfo_width()
        root_h = self.winfo_height()
        x = root_x + (root_w - win_w) // 2
        y = root_y + (root_h - win_h) // 2
        dialog.geometry(f"+{x}+{y}")

        dialog.wait_window()
        if not res["ok"]:
            return

        lst = self.config_data.get("app_exclusions", [])
        if index is None:
            lst.append(res["value"])
        else:
            lst[index] = res["value"]
        self.config_data["app_exclusions"] = lst
        self._save_config()
        self._refresh_exclusions_ui()

    def on_delete_app(self):
        try:
            idx = self.app_list.curselection()[0]
        except IndexError:
            messagebox.showerror(APP_TITLE, "Выбери программу в списке.")
            return
        lst = self.config_data.get("app_exclusions", [])
        if idx >= len(lst):
            return
        del lst[idx]
        self.config_data["app_exclusions"] = lst
        self._save_config()
        self._refresh_exclusions_ui()

    def on_manage_exclusions(self):
        messagebox.showinfo(
            APP_TITLE,
            "Список исключений показан в этом блоке.\n"
            "Добавить: кнопки сверху.\n"
            "Редактировать/удалить: выбери элемент и используй кнопки ✎ / ✖.",
        )

    # ---------- connect / disconnect ----------

    def on_toggle(self):
        if self.proc and self.proc.poll() is None:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        profiles = self._get_profiles()
        if not profiles:
            messagebox.showerror(APP_TITLE, "Сначала создай профиль с подпиской.")
            return
        if (
            self.current_profile_index is None
            or self.current_profile_index >= len(profiles)
        ):
            messagebox.showerror(APP_TITLE, "Выбери профиль.")
            return

        profile = profiles[self.current_profile_index]
        if not profile.url.strip():
            messagebox.showerror(APP_TITLE, "У профиля нет URL подписки.")
            return

        sing_box_exe = self.base_dir / "sing-box.exe"
        if not sing_box_exe.exists():
            messagebox.showerror(
                APP_TITLE, "Не найден sing-box.exe рядом с программой."
            )
            return

        self.toggle_btn.configure(state="disabled")
        self.btn_tun_on.configure(state="disabled")
        self.btn_tun_off.configure(state="disabled")
        self.append_log(
            f"\n=== Подключение к профилю: {profile.name} ===\n"
        )
        self.set_status("подключение...", "orange")

        t = threading.Thread(
            target=self._connect_worker,
            args=(profile.url, self.base_dir, sing_box_exe, self.current_profile_index),
            daemon=True,
        )
        t.start()

    def _update_profile_info_from_vless(self, idx, vless_url):
        try:
            u = urlparse(vless_url)
            server = u.hostname or ""
            port = u.port or 443
            remark = unquote(u.fragment) if u.fragment else ""
            profiles = self._get_profiles()
            if idx is None or idx < 0 or idx >= len(profiles):
                return
            p = profiles[idx]
            p.ptype = "VLESS"
            p.address = f"{server}:{port}"
            p.remark = remark
            self._set_profiles(profiles)
            self._refresh_profile_info_ui()
        except Exception:
            pass

    def _connect_worker(self, url: str, base_dir: Path, sing_box_exe: Path, idx: int):
        try:
            self.append_log("Скачиваю подписку...\n")
            with urllib.request.urlopen(url) as resp:
                sub_bytes = resp.read()

            vless = decode_subscription_to_vless(sub_bytes)
            self.append_log(f"VLESS: {vless}\n")

            # обновим инфо по профилю
            self.after(0, lambda: self._update_profile_info_from_vless(idx, vless))

            cfg_dict = build_singbox_config(
                vless_url=vless,
                ru_mode=self.config_data.get("ru_mode", True),
                site_excl=self.config_data.get("site_exclusions", []),
                app_excl=self.config_data.get("app_exclusions", []),
            )
            cfg_path = base_dir / "config.json"
            cfg_path.write_text(
                json.dumps(cfg_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.append_log("config.json сгенерирован.\n")

            self.append_log("Запускаю sing-box...\n")
            env = os.environ.copy()
            env["ENABLE_DEPRECATED_TUN_ADDRESS_X"] = "true"
            env["ENABLE_DEPRECATED_DNS_SERVER_FORMAT"] = "true"
            env["ENABLE_DEPRECATED_SPECIAL_OUTBOUNDS"] = "true"

            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.proc = subprocess.Popen(
                [str(sing_box_exe), "run", "-c", str(cfg_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )

            self.stop_log.clear()
            self.log_thread = threading.Thread(
                target=self._log_reader, daemon=True
            )
            self.log_thread.start()

            self.after(0, self._on_connected_ok)

        except Exception as e:
            err = f"Ошибка подключения: {e}\n"
            self.after(0, lambda: self.append_log(err))
            self.after(0, lambda: self.set_status("ошибка", "red"))
            self.after(
                0,
                lambda: (
                    self.toggle_btn.configure(state="normal"),
                    self.btn_tun_on.configure(state="normal"),
                    self.btn_tun_off.configure(state="disabled"),
                ),
            )

    def _on_connected_ok(self):
        self.set_status("подключен", "green")
        self.toggle_var.set("Отключить")
        self.toggle_btn.configure(state="normal")
        self.btn_tun_on.configure(state="disabled")
        self.btn_tun_off.configure(state="normal")
        # обновляем IP при успешном подключении
        self._update_ip_async()

    def _log_reader(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            if self.stop_log.is_set():
                break
            self.after(0, lambda l=line: self.append_log(l))
        self.after(0, self._on_process_exit)

    def _on_process_exit(self):
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self.append_log(f"\nsing-box завершился с кодом {code}\n")
        self.proc = None
        self.stop_log.set()
        self.set_status("отключен", "red")
        self.toggle_var.set("Подключить")
        self.toggle_btn.configure(state="normal")
        self.btn_tun_on.configure(state="normal")
        self.btn_tun_off.configure(state="disabled")
        self.ip_var.set("IP: -")

    def disconnect(self):
        if not self.proc or self.proc.poll() is not None:
            self.append_log("\nУже отключен.\n")
            self.proc = None
            self.stop_log.set()
            self.set_status("отключен", "red")
            self.toggle_var.set("Подключить")
            self.btn_tun_on.configure(state="normal")
            self.btn_tun_off.configure(state="disabled")
            self.toggle_btn.configure(state="normal")
            self.ip_var.set("IP: -")
            return

        self.append_log("\n=== Отключение... ===\n")
        self.set_status("отключение...", "orange")
        self.toggle_btn.configure(state="disabled")
        self.btn_tun_on.configure(state="disabled")
        self.btn_tun_off.configure(state="disabled")

        t = threading.Thread(target=self._disconnect_worker, daemon=True)
        t.start()

    def _disconnect_worker(self):
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.append_log(
                        "sing-box не завершился, принудительное завершение...\n"
                    )
                    try:
                        self.proc.kill()
                    except Exception:
                        pass

            self.stop_log.set()
        finally:
            self.after(0, self._on_disconnected_manual)

    def _on_disconnected_manual(self):
        self.proc = None
        self.set_status("отключен", "red")
        self.toggle_var.set("Подключить")
        self.toggle_btn.configure(state="normal")
        self.btn_tun_on.configure(state="normal")
        self.btn_tun_off.configure(state="disabled")
        self.ip_var.set("IP: -")
        self.append_log("Туннель остановлен.\n")

    # ---------- закрытие окна ----------

    def on_close(self):
        if self.proc and self.proc.poll() is not None:
            try:
                self.append_log(
                    "\n=== Закрытие приложения, отключаю VPN... ===\n"
                )
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

        self.stop_log.set()
        self.destroy()


def main():
    app = VlfGui()
    app.mainloop()


if __name__ == "__main__":
    main()
