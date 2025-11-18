import tkinter as tk
from tkinter import ttk, messagebox, filedialog
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

# Цвета (nekobox-style)
COLOR_BG = "#101421"
COLOR_PANEL = "#151a24"
COLOR_PANEL_DARK = "#111827"
COLOR_TEXT = "#ffffff"
COLOR_SUBTEXT = "#9ca3af"
COLOR_ACCENT = "#00d4ff"
COLOR_ACCENT_MUTED = "#00a6cc"
COLOR_BAD = "#e11d48"
COLOR_GOOD = "#22c55e"
COLOR_BORDER = "#1f2933"

GREEN_BTN = "#22c55e"
RED_BTN = "#ef4444"
GRAY_BTN = "#4b5563"

APP_TITLE = "VLF VPN по подписке"
PROFILES_FILE = "profiles.json"
EXCLUSIONS_FILE = "exclusions.json"
SING_BOX_EXE = "sing-box.exe"


def resource_path(relative: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class Profile:
    def __init__(self, name, sub_url):
        self.name = name
        self.sub_url = sub_url
        self.type = ""
        self.address = ""
        self.remark = ""

    def to_dict(self):
        return {
            "name": self.name,
            "sub_url": self.sub_url,
            "type": self.type,
            "address": self.address,
            "remark": self.remark,
        }

    @staticmethod
    def from_dict(d):
        p = Profile(d.get("name", ""), d.get("sub_url", ""))
        p.type = d.get("type", "")
        p.address = d.get("address", "")
        p.remark = d.get("remark", "")
        return p


class Exclusions:
    def __init__(self):
        self.sites = []
        self.apps = []
        self.ru_mode = True

    def to_dict(self):
        return {
            "sites": self.sites,
            "apps": self.apps,
            "ru_mode": self.ru_mode,
        }

    @staticmethod
    def from_dict(d):
        ex = Exclusions()
        ex.sites = d.get("sites", [])
        ex.apps = d.get("apps", [])
        ex.ru_mode = d.get("ru_mode", True)
        return ex


class SingBoxRunner(threading.Thread):
    def __init__(self, exe_path, config_path, log_callback, on_exit):
        super().__init__(daemon=True)
        self.exe_path = exe_path
        self.config_path = config_path
        self.log_callback = log_callback
        self.on_exit = on_exit
        self.proc = None
        self._stop_flag = threading.Event()

    def stop(self):
        self._stop_flag.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def run(self):
        try:
            # ВАЖНО: включаем поддержку legacy special outbounds,
            # чтобы sing-box 1.12.12 не падал с FATAL.
            env = os.environ.copy()
            env["ENABLE_DEPRECATED_SPECIAL_OUTBOUNDS"] = "true"

            self.proc = subprocess.Popen(
                [self.exe_path, "run", "-c", self.config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                env=env,
            )
        except Exception as e:
            self.log_callback(f"[ERROR] Не удалось запустить sing-box: {e}\n")
            self.on_exit()
            return

        for line in self.proc.stdout:
            if self._stop_flag.is_set():
                break
            self.log_callback(line)

        rc = self.proc.wait()
        self.on_exit()
        self.log_callback(f"\n[INFO] sing-box завершился с кодом {rc}\n")


class VlfGui(tk.Tk):
    def __init__(self):
        super().__init__()

        self.base_dir = Path(sys.argv[0]).resolve().parent

        icon_path = self.base_dir / "vlf.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.title(APP_TITLE)
        self.geometry("820x620")
        self.resizable(False, False)

        self.profiles_path = self.base_dir / PROFILES_FILE
        self.exclusions_path = self.base_dir / EXCLUSIONS_FILE
        self.config_path = self.base_dir / "config.json"
        self.sing_box_path = self.base_dir / SING_BOX_EXE

        self.profiles = []  # list[Profile]
        self.exclusions = Exclusions()

        self.current_profile = None
        self.runner = None
        self.sing_box_running = False

        self.profile_var = tk.StringVar()
        self.status_var = tk.StringVar(value="отключен")
        self.ip_var = tk.StringVar(value="IP: -")

        self.ru_mode_var = tk.BooleanVar(value=True)

        self.logo_img = None
        self.logo_img_small = None

        self._build_ui()
        self._load_data()
        self._refresh_profile_ui()
        self._refresh_exclusions_ui()
        self._update_status_view()

    # ---------- helpers ----------

    def _load_logo_image(self):
        # Логотип больше не используем в центре, но оставляю функцию,
        # на случай если захочешь вернуть картинку.
        try:
            from PIL import Image, ImageTk

            img_path = self.base_dir / "vlf_logo_big.png"
            if not img_path.exists():
                return
            img = Image.open(img_path)
            img = img.resize((220, 220), Image.LANCZOS)
            self.logo_img = ImageTk.PhotoImage(img)
        except Exception:
            self.logo_img = None

    def _load_logo_image_small(self):
        try:
            from PIL import Image, ImageTk

            img_path = self.base_dir / "vlf_logo_small.png"
            if not img_path.exists():
                img_path = self.base_dir / "vlf_logo_big.png"
                if not img_path.exists():
                    return
            img = Image.open(img_path)
            img = img.resize((24, 24), Image.LANCZOS)
            self.logo_img_small = ImageTk.PhotoImage(img)
        except Exception:
            self.logo_img_small = None

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str, good: bool | None = None):
        self.status_var.set(text)
        if good is None:
            color = COLOR_SUBTEXT
        elif good:
            color = COLOR_GOOD
        else:
            color = COLOR_BAD
        # label у нас есть, но он не показывается – это ок
        self.status_label.configure(fg=color)

    def _set_ip(self, ip: str):
        self.ip_var.set(f"IP: {ip}")

    # ---------- небольшие фабрики виджетов ----------

    def _create_pill_button(self, parent, text, bg, command=None):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg="white",
            activebackground=bg,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            font=("Segoe UI", 10, "bold"),
        )
        btn.configure(
            highlightthickness=0,
        )
        return btn

    def _create_icon_button(self, parent, text, command=None):
        """Компактная кнопка для редактирования/удаления в блоках исключений."""
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg="#1b2430",
            fg=COLOR_TEXT,
            activebackground="#243447",
            activeforeground=COLOR_TEXT,
            relief="solid",
            bd=1,
            width=8,
            height=1,
            highlightthickness=0,
            font=("Segoe UI", 9),
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
        style.configure("Panel.TFrame", background=COLOR_PANEL)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure(
            "Secondary.TLabel",
            background=COLOR_BG,
            foreground=COLOR_SUBTEXT,
        )
        style.configure(
            "Panel.TLabel",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=6,
        )
        style.configure(
            "Small.TCheckbutton",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
            font=("Segoe UI", 9),
        )

        # ---------- верхняя полоса с кнопками и логотипом ----------
        header = tk.Frame(self, bg=COLOR_BG)
        header.pack(fill="x", padx=10, pady=(8, 4))

        # Левая часть шапки: три большие кнопки
        left_header = tk.Frame(header, bg=COLOR_BG)
        left_header.pack(side="left", padx=0, pady=4)

        self.btn_tun_on = self._create_pill_button(
            left_header,
            "Туннель ВКЛ",
            GREEN_BTN,
            command=self.on_tun_on,
        )
        self.btn_tun_on.pack(side="left")

        self.btn_tun_off = self._create_pill_button(
            left_header,
            "ВЫКЛ",
            RED_BTN,
            command=self.on_tun_off,
        )
        self.btn_tun_off.pack(side="left", padx=4)

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

        # Правая часть шапки – колонка с логотипом и @ботом
        right_header = tk.Frame(header, bg=COLOR_BG)
        right_header.pack(side="right", padx=0, pady=4)

        self._load_logo_image_small()
        if self.logo_img_small is not None:
            self.logo_label = tk.Label(
                right_header,
                image=self.logo_img_small,
                bg=COLOR_BG,
                bd=0,
            )
        else:
            self.logo_label = tk.Label(
                right_header,
                text="VLF",
                bg=COLOR_BG,
                fg=COLOR_ACCENT,
                font=("Segoe UI", 14, "bold"),
            )
        self.logo_label.pack(side="top", anchor="e")

        # кликабельная ссылка на бота
        bot_label = tk.Label(
            right_header,
            text="@vltfунAT_bot" if False else "@vlftunAT_bot",
            bg=COLOR_BG,
            fg=COLOR_ACCENT,
            cursor="hand2",
            font=("Segoe UI", 10),
        )
        bot_label.pack(side="top", anchor="e", pady=(4, 0))
        bot_label.bind(
            "<Button-1>",
            lambda e: self._open_telegram_bot(),
        )

        # ---------- центральный блок ----------
        # Убираем логотип и надпись "VLF / отключен".
        # Оставляем только IP, как ты просил.
        center_frame = tk.Frame(self, bg=COLOR_BG)
        center_frame.pack(fill="x", padx=10, pady=(0, 8))

        # status_label нужен для логики, но не показываем его.
        self.status_label = tk.Label(
            center_frame,
            textvariable=self.status_var,
            bg=COLOR_BG,
            fg=COLOR_BAD,
            font=("Segoe UI", 10),
        )
        # НЕ делаем pack() => визуально не видно

        self.ip_label = tk.Label(
            center_frame,
            textvariable=self.ip_var,
            bg=COLOR_BG,
            fg=COLOR_SUBTEXT,
            font=("Segoe UI", 9),
        )
        self.ip_label.pack(pady=(2, 0))

        # ---------- основной низ: слева профили, справа исключения ----------
        main_frame = tk.Frame(self, bg=COLOR_BG)
        main_frame.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        # Левая панель – профили
        left_panel = tk.Frame(main_frame, bg=COLOR_PANEL, bd=1, relief="solid")
        left_panel.pack(side="left", fill="both", expand=True)

        left_header_frame = tk.Frame(left_panel, bg=COLOR_PANEL)
        left_header_frame.pack(fill="x", padx=8, pady=(6, 2))

        tk.Label(
            left_header_frame,
            text="Профили",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        profile_controls = tk.Frame(left_header_frame, bg=COLOR_PANEL)
        profile_controls.pack(side="right")

        ttk.Button(
            profile_controls,
            text="Добавить",
            style="Accent.TButton",
            command=self.on_add_profile,
        ).pack(side="left", padx=2)

        ttk.Button(
            profile_controls,
            text="Изменить",
            style="Accent.TButton",
            command=self.on_edit_profile,
        ).pack(side="left", padx=2)

        ttk.Button(
            profile_controls,
            text="Удалить",
            style="Accent.TButton",
            command=self.on_delete_profile,
        ).pack(side="left", padx=2)

        # выпадающий список профилей над списком
        combo_frame = tk.Frame(left_panel, bg=COLOR_PANEL)
        combo_frame.pack(fill="x", padx=8, pady=(0, 4))

        self.profile_combo = ttk.Combobox(
            combo_frame,
            textvariable=self.profile_var,
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.profile_combo.pack(fill="x")

        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        # список профилей (как в некобоксе)
        self.profile_list = tk.Listbox(
            left_panel,
            height=6,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            selectbackground=COLOR_ACCENT,
            selectforeground="#000000",
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
        )
        self.profile_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.profile_list.bind("<<ListboxSelect>>", self.on_profile_list_selected)

        # информационная строка по профилю
        info_frame = tk.Frame(left_panel, bg=COLOR_PANEL_DARK, height=48)
        info_frame.pack(fill="x", padx=0, pady=(0, 0))
        info_frame.pack_propagate(False)

        self.lbl_profile_type = tk.Label(
            info_frame,
            text="Тип: -",
            bg=COLOR_PANEL_DARK,
            fg=COLOR_SUBTEXT,
            anchor="w",
        )
        self.lbl_profile_type.pack(fill="x", padx=8, pady=(4, 0))

        self.lbl_profile_addr = tk.Label(
            info_frame,
            text="Адрес: -",
            bg=COLOR_PANEL_DARK,
            fg=COLOR_SUBTEXT,
            anchor="w",
        )
        self.lbl_profile_addr.pack(fill="x", padx=8)

        self.lbl_profile_name = tk.Label(
            info_frame,
            text="Имя: -",
            bg=COLOR_PANEL_DARK,
            fg=COLOR_SUBTEXT,
            anchor="w",
        )
        self.lbl_profile_name.pack(fill="x", padx=8, pady=(0, 4))

        # Правая панель – исключения
        right_panel = tk.Frame(main_frame, bg=COLOR_PANEL, bd=1, relief="solid")
        right_panel.pack(side="left", fill="both", expand=True, padx=(8, 0))

        exc_top = tk.Frame(right_panel, bg=COLOR_PANEL)
        exc_top.pack(fill="x", padx=8, pady=(6, 4))

        tk.Label(
            exc_top,
            text="Исключения",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

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

        # Кнопка менеджера исключений
        self.btn_manager = ttk.Button(
            exc_top,
            text="Менеджер",
            style="Accent.TButton",
            command=self.on_manage_exclusions,
        )
        self.btn_manager.pack(side="right", padx=2)

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
        site_btns.rowconfigure(0, weight=1)
        site_btns.rowconfigure(3, weight=1)
        self._create_icon_button(site_btns, "Изм.", self.on_edit_site).grid(
            row=1, column=0, pady=2, sticky="n"
        )
        self._create_icon_button(site_btns, "Удал.", self.on_delete_site).grid(
            row=2, column=0, pady=2, sticky="n"
        )

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

        apps_btns = tk.Frame(apps_frame, bg=COLOR_PANEL)
        apps_btns.pack(side="left", fill="y", padx=(4, 0))
        apps_btns.rowconfigure(0, weight=1)
        apps_btns.rowconfigure(3, weight=1)
        self._create_icon_button(apps_btns, "Изм.", self.on_edit_app).grid(
            row=1, column=0, pady=2, sticky="n"
        )
        self._create_icon_button(apps_btns, "Удал.", self.on_delete_app).grid(
            row=2, column=0, pady=2, sticky="n"
        )

        # Режим РФ чекбокс
        ru_frame = tk.Frame(right_panel, bg=COLOR_PANEL)
        ru_frame.pack(fill="x", padx=8, pady=(0, 6))

        self.chk_ru_mode = ttk.Checkbutton(
            ru_frame,
            text="Режим РФ: русские сайты напрямую",
            variable=self.ru_mode_var,
            style="Small.TCheckbutton",
            command=self.on_ru_mode_changed,
        )
        self.chk_ru_mode.pack(anchor="w")

        # ---------- лог ----------
        log_frame = tk.Frame(self, bg=COLOR_BG, bd=1, relief="solid")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        tk.Label(
            log_frame,
            text="Лог sing-box",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=6, pady=(4, 0))

        self.log_text = tk.Text(
            log_frame,
            bg=COLOR_BG,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            wrap="none",
            height=8,
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 8),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        x_scroll = tk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        x_scroll.pack(fill="x", side="bottom")
        self.log_text.configure(xscrollcommand=x_scroll.set)

    # ---------- логика загрузки/сохранения ----------

    def _load_data(self):
        profiles_data = load_json(self.profiles_path, [])
        self.profiles = [Profile.from_dict(d) for d in profiles_data]

        excl_data = load_json(self.exclusions_path, self.exclusions.to_dict())
        self.exclusions = Exclusions.from_dict(excl_data)
        self.ru_mode_var.set(self.exclusions.ru_mode)

        if self.profiles:
            self.current_profile = self.profiles[0]
            self.profile_var.set(self.current_profile.name)
            self.profile_list.selection_set(0)

    def _save_profiles(self):
        data = [p.to_dict() for p in self.profiles]
        save_json(self.profiles_path, data)

    def _save_exclusions(self):
        self.exclusions.ru_mode = self.ru_mode_var.get()
        save_json(self.exclusions_path, self.exclusions.to_dict())

    def _refresh_profile_ui(self):
        names = [p.name for p in self.profiles]
        self.profile_combo["values"] = names
        self.profile_list.delete(0, "end")
        for name in names:
            self.profile_list.insert("end", name)
        if self.current_profile and self.current_profile.name in names:
            idx = names.index(self.current_profile.name)
            self.profile_combo.current(idx)
            self.profile_list.selection_clear(0, "end")
            self.profile_list.selection_set(idx)
        elif names:
            self.profile_combo.current(0)
            self.profile_list.selection_clear(0, "end")
            self.profile_list.selection_set(0)
            self.current_profile = self.profiles[0]
        else:
            self.profile_combo.set("")
            self.current_profile = None
        self._update_profile_info()

    def _refresh_exclusions_ui(self):
        self.site_list.delete(0, "end")
        for s in self.exclusions.sites:
            self.site_list.insert("end", s)
        self.app_list.delete(0, "end")
        for a in self.exclusions.apps:
            self.app_list.insert("end", a)

    def _update_profile_info(self):
        if not self.current_profile:
            self.lbl_profile_type.config(text="Тип: -")
            self.lbl_profile_addr.config(text="Адрес: -")
            self.lbl_profile_name.config(text="Имя: -")
            return
        self.lbl_profile_type.config(text=f"Тип: {self.current_profile.type or '-'}")
        self.lbl_profile_addr.config(text=f"Адрес: {self.current_profile.address or '-'}")
        self.lbl_profile_name.config(text=f"Имя: {self.current_profile.remark or '-'}")

    def _update_status_view(self):
        if self.sing_box_running:
            self._set_status("подключен", good=True)
        else:
            self._set_status("отключен", good=False)

    # ---------- действия UI ----------

    def _open_telegram_bot(self):
        import webbrowser

        webbrowser.open("https://t.me/vlftunAT_bot")

    def on_profile_selected(self, event=None):
        name = self.profile_var.get()
        for p in self.profiles:
            if p.name == name:
                self.current_profile = p
                break
        else:
            self.current_profile = None
        self._update_profile_info()

    def on_profile_list_selected(self, event=None):
        sel = self.profile_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.profiles):
            self.current_profile = self.profiles[idx]
            self.profile_var.set(self.current_profile.name)
            self._update_profile_info()

    def on_add_profile(self):
        self._edit_profile_dialog(None)

    def on_edit_profile(self):
        if not self.current_profile:
            messagebox.showwarning(APP_TITLE, "Сначала выберите профиль.")
            return
        self._edit_profile_dialog(self.current_profile)

    def on_delete_profile(self):
        if not self.current_profile:
            messagebox.showwarning(APP_TITLE, "Сначала выберите профиль.")
            return
        if (
            messagebox.askyesno(
                APP_TITLE,
                "Удалить выбранный профиль?",
                icon="question",
            )
            is False
        ):
            return
        self.profiles = [p for p in self.profiles if p is not self.current_profile]
        self.current_profile = self.profiles[0] if self.profiles else None
        self._save_profiles()
        self._refresh_profile_ui()

    def on_add_site(self):
        site = self._ask_string("Добавить сайт", "Введите домен или URL:")
        if site:
            self.exclusions.sites.append(site)
            self._save_exclusions()
            self._refresh_exclusions_ui()

    def on_edit_site(self):
        sel = self.site_list.curselection()
        if not sel:
            return
        idx = sel[0]
        cur = self.exclusions.sites[idx]
        new_val = self._ask_string("Редактировать сайт", "Измените домен или URL:", cur)
        if new_val:
            self.exclusions.sites[idx] = new_val
            self._save_exclusions()
            self._refresh_exclusions_ui()

    def on_delete_site(self):
        sel = self.site_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if not messagebox.askyesno(APP_TITLE, "Удалить выбранный сайт?"):
            return
        self.exclusions.sites.pop(idx)
        self._save_exclusions()
        self._refresh_exclusions_ui()

    def on_add_app(self):
        path = filedialog.askopenfilename(title="Выберите exe-файл программы")
        if path:
            self.exclusions.apps.append(path)
            self._save_exclusions()
            self._refresh_exclusions_ui()

    def on_edit_app(self):
        sel = self.app_list.curselection()
        if not sel:
            return
        idx = sel[0]
        cur = self.exclusions.apps[idx]
        new_val = self._ask_string(
            "Редактировать программу", "Измените путь к exe-файлу:", cur
        )
        if new_val:
            self.exclusions.apps[idx] = new_val
            self._save_exclusions()
            self._refresh_exclusions_ui()

    def on_delete_app(self):
        sel = self.app_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if not messagebox.askyesno(APP_TITLE, "Удалить выбранную программу?"):
            return
        self.exclusions.apps.pop(idx)
        self._save_exclusions()
        self._refresh_exclusions_ui()

    def on_manage_exclusions(self):
        messagebox.showinfo(
            APP_TITLE,
            "Список исключений показан в этом блоке.\n"
            "Добавить: кнопки сверху.\n"
            "Редактировать/удалить: выбери элемент и используй кнопки 'Изм.' / 'Удал.'",
        )

    def on_ru_mode_changed(self):
        self._save_exclusions()

    # ---------- диалоги ----------

    def _ask_string(self, title, prompt, initial=""):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 150
        y = self.winfo_y() + (self.winfo_height() // 2) - 60
        dialog.geometry(f"300x120+{x}+{y}")

        tk.Label(
            dialog,
            text=prompt,
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(8, 2))

        var = tk.StringVar(value=initial)
        entry = tk.Entry(
            dialog,
            textvariable=var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        )
        entry.pack(fill="x", padx=8, pady=(0, 8))
        entry.focus_set()

        btn_frame = tk.Frame(dialog, bg=COLOR_BG)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))

        result = {"value": None}

        def on_ok():
            result["value"] = var.get().strip()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Отмена", command=on_cancel).pack(side="right")

        dialog.grab_set()
        self.wait_window(dialog)
        return result["value"]

    def _edit_profile_dialog(self, profile: Profile | None):
        from dark_messagebox import dark_showerror, dark_showinfo

        dialog = tk.Toplevel(self)
        dialog.title("Новый профиль" if profile is None else "Изменить профиль")
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 90
        dialog.geometry(f"400x180+{x}+{y}")

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

        tk.Label(
            dialog,
            text="Ссылка-подписка:",
            bg=COLOR_BG,
            fg=COLOR_TEXT,
        ).pack(anchor="w", padx=8, pady=(0, 2))

        sub_var = tk.StringVar(value=profile.sub_url if profile else "")
        sub_entry = tk.Entry(
            dialog,
            textvariable=sub_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
        )
        sub_entry.pack(fill="x", padx=8, pady=(0, 8))

        btn_frame = tk.Frame(dialog, bg=COLOR_BG)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))

        result = {"saved": False}

        def on_ok():
            name = name_var.get().strip()
            sub = sub_var.get().strip()
            if not name or not sub:
                dark_showerror(APP_TITLE, "Нужно заполнить и имя, и ссылку.")
                return
            if profile is None:
                p = Profile(name, sub)
                self.profiles.append(p)
                self.current_profile = p
            else:
                profile.name = name
                profile.sub_url = sub
                self.current_profile = profile
            self._save_profiles()
            self._refresh_profile_ui()
            result["saved"] = True
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        def on_from_qr():
            try:
                from PIL import Image
                from pyzbar.pyzbar import decode
            except Exception:
                dark_showerror(
                    APP_TITLE,
                    "Для чтения QR нужно установить pillow и pyzbar.",
                )
                return
            path = filedialog.askopenfilename(
                title="Выберите картинку с QR",
                filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.gif"), ("All", "*.*")],
            )
            if not path:
                return
            try:
                img = Image.open(path)
                codes = decode(img)
                if not codes:
                    dark_showerror(APP_TITLE, "QR-код не найден на изображении.")
                    return
                data = codes[0].data.decode("utf-8")
                sub_var.set(data)
                dark_showinfo(APP_TITLE, "Ссылка из QR успешно считана.")
            except Exception as e:
                dark_showerror(APP_TITLE, f"Ошибка чтения QR: {e}")

        btn_qr = ttk.Button(btn_frame, text="Из QR", command=on_from_qr)
        btn_qr.pack(side="left")

        ttk.Button(btn_frame, text="Отмена", command=on_cancel).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="right")

        dialog.grab_set()
        name_entry.focus_set()
        self.wait_window(dialog)

    # ---------- генерация конфига sing-box ----------

    def _parse_sub_link(self, sub_url: str):
        try:
            if sub_url.strip().startswith("vless://"):
                return self._parse_single_vless(sub_url.strip())

            if "://" not in sub_url:
                with open(sub_url, "r", encoding="utf-8") as f:
                    first = f.read().strip()
                if first.startswith("vless://"):
                    return self._parse_single_vless(first)
                sub_url = first

            parsed = urlparse(sub_url)
            if parsed.scheme in ("http", "https"):
                resp = urllib.request.urlopen(sub_url, timeout=10)
                data = resp.read().decode("utf-8", errors="ignore").strip()
            else:
                data = sub_url.strip()

            try:
                raw = base64.b64decode(data).decode("utf-8", errors="ignore")
            except Exception:
                raw = data

            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            vless_lines = [ln for ln in lines if ln.startswith("vless://")]
            if not vless_lines:
                raise ValueError("Не найдены vless:// строки в подписке.")
            return self._parse_single_vless(vless_lines[0])
        except Exception as e:
            raise ValueError(f"Не удалось разобрать подписку: {e}") from e

    def _parse_single_vless(self, link: str):
        if not link.startswith("vless://"):
            raise ValueError("Строка не является VLESS URL.")
        without_scheme = link[len("vless://") :]
        userinfo, rest = without_scheme.split("@", 1)
        user_id = userinfo.split(":")[0]

        if "#" in rest:
            rest, remark = rest.split("#", 1)
            remark = unquote(remark)
        else:
            remark = ""

        if "?" in rest:
            hostport, query = rest.split("?", 1)
        else:
            hostport, query = rest, ""
        if ":" not in hostport:
            raise ValueError("Нет порта в адресе VLESS.")
        host, port = hostport.split(":", 1)

        q = parse_qs(query)
        encryption = q.get("encryption", ["none"])[0]
        flow = q.get("flow", [""])[0]
        security = q.get("security", ["none"])[0]
        sni = q.get("sni", [""])[0]
        fp = q.get("fp", [""])[0]
        alpn = q.get("alpn", [""])[0]
        mode = q.get("mode", [""])[0]
        network = q.get("type", ["tcp"])[0]

        return {
            "id": user_id,
            "host": host,
            "port": int(port),
            "remark": remark,
            "encryption": encryption,
            "flow": flow,
            "security": security,
            "sni": sni,
            "fp": fp,
            "alpn": alpn,
            "mode": mode,
            "network": network,
        }

def _build_singbox_config(self, vless, exclusions: Exclusions):
    server_addr = vless["host"]
    server_port = vless["port"]
    user_id = vless["id"]
    security = vless["security"]
    sni = vless["sni"]
    flow = vless["flow"]
    network = (vless["network"] or "tcp").lower()

    tun_addr = "172.19.0.2/30"

    inbound_tun = {
        "type": "tun",
        "tag": "tun-in",
        "inet4_address": [tun_addr],
        "mtu": 9000,
        "auto_route": True,
        "strict_route": True,
        "stack": "gvisor",
        "sniff": True,
        "sniff_override_destination": True,
    }

    # DNS — пока старый формат (даёт только WARN, но работает)
    dns = {
        "servers": [
            {
                "tag": "local",
                "address": "udp://8.8.8.8",
                "detour": "proxy-out",
            }
        ],
        "strategy": "ipv4_only",
        "disable_cache": False,
    }

    # 🚫 НЕТ поля "transport"
    # 🚫 НЕТ fingerprint
    # ✔ "tls.enabled" = true только при security=reality
    outbound_proxy = {
        "type": "vless",
        "tag": "proxy-out",
        "server": server_addr,
        "server_port": server_port,
        "uuid": user_id,
        "flow": flow or "",
        "packet_encoding": "xudp",

        "tls": {
            "enabled": security == "reality",
            "server_name": sni or server_addr,
            "reality": {
                "enabled": security == "reality",
                "public_key": "",
                "short_id": "",
            },
        },
    }

    outbound_direct = {"type": "direct", "tag": "direct"}
    outbound_dns = {"type": "dns", "tag": "dns-out"}
    outbound_block = {"type": "block", "tag": "block"}

    rules = [
        {"protocol": "dns", "outbound": "dns-out"},
        {"rule_set": ["geoip-ru"], "outbound": "direct"},
    ]

    # Сайты
    for site in exclusions.sites:
        site = site.strip()
        if site:
            rules.append({"domain": [site], "outbound": "direct"})

    # Программы
    for app in exclusions.apps:
        exe = os.path.basename(app)
        rules.append({"process_name": exe, "outbound": "direct"})

    # geosite RU
    if exclusions.ru_mode:
        rules.append({"rule_set": ["geosite-ru"], "outbound": "direct"})

    # Сервер — в обход туннеля
    try:
        socket.inet_aton(server_addr)
        rules.append({"ip_cidr": [f"{server_addr}/32"], "outbound": "direct"})
    except OSError:
        rules.append({"domain": [server_addr], "outbound": "direct"})

    config = {
        "log": {"level": "info"},
        "dns": dns,
        "inbounds": [inbound_tun],
        "outbounds": [
            outbound_proxy,
            outbound_direct,
            outbound_dns,
            outbound_block,
        ],
        "route": {
            "rules": rules,
            "rule_set": [
                {
                    "tag": "geoip-ru",
                    "type": "geoip",
                    "country_code": ["RU"],
                },
                {
                    "tag": "geosite-ru",
                    "type": "geosite",
                    "domain": ["geosite:category-ru"],
                },
            ],
        },
    }

    return config



    # ---------- запуск / остановка sing-box ----------

    def on_tun_on(self):
        if self.sing_box_running:
            return
        if not self.current_profile:
            messagebox.showwarning(APP_TITLE, "Сначала создайте и выберите профиль.")
            return
        if not self.sing_box_path.exists():
            messagebox.showerror(
                APP_TITLE,
                f"Не найден {SING_BOX_EXE} рядом с программой.",
            )
            return

        try:
            vless = self._parse_sub_link(self.current_profile.sub_url)
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        # Обновляем инфу по профилю
        self.current_profile.type = "VLESS"
        self.current_profile.address = f'{vless["host"]}:{vless["port"]}'
        self.current_profile.remark = vless["remark"]
        self._save_profiles()
        self._update_profile_info()

        # Строим конфиг для sing-box (у тебя уже новая версия _build_singbox_config)
        config = self._build_singbox_config(vless, self.exclusions)
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Не удалось записать config.json: {e}")
            return

        # Чистим лог
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.sing_box_running = True
        self._update_status_view()

        def log_cb(line):
            self.after(0, self._append_log, line)

        def on_exit():
            self.after(0, self._on_singbox_exit)

        # Запуск sing-box в отдельном потоке
        self.runner = SingBoxRunner(
            str(self.sing_box_path),
            str(self.config_path),
            log_cb,
            on_exit,
        )
        self.runner.start()

        # Обновляем внешний IP асинхронно
        threading.Thread(target=self._update_public_ip, daemon=True).start()

    def _on_singbox_exit(self):
        self.sing_box_running = False
        self.runner = None
        self._update_status_view()
        self._set_ip("-")

    def _update_public_ip(self):
        try:
            with urllib.request.urlopen("https://api.ipify.org?format=text", timeout=10) as r:
                ip = r.read().decode().strip()
        except Exception:
            ip = "-"
        self.after(0, self._set_ip, ip)

    def on_tun_off(self):
        if self.runner:
            self.runner.stop()
        else:
            self.sing_box_running = False
            self._update_status_view()
            self._set_ip("-")



if __name__ == "__main__":
    app = VlfGui()
    app.mainloop()
