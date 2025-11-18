# dark_messagebox.py
import tkinter as tk
from tkinter import ttk

BG = "#0d1117"
FG = "#ffffff"
ACCENT = "#00c2ff"
BTN_BG = "#21262d"
BTN_FG = "#ffffff"


def _center_window(win, parent=None):
    win.update_idletasks()
    if parent is None:
        x = (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2
        y = (win.winfo_screenheight() - win.winfo_reqheight()) // 2
    else:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        x = px + (pw - win.winfo_reqwidth()) // 2
        y = py + (ph - win.winfo_reqheight()) // 2
    win.geometry(f"+{x}+{y}")


def _dark_messagebox(title, message, icon="info", buttons=("OK",), default_index=0, parent=None):
    root = parent or tk._default_root
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()

    try:
        win.iconbitmap("vlf_icon.ico")
    except Exception:
        pass

    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("Dark.TFrame", background=BG)
    style.configure("Dark.TLabel", background=BG, foreground=FG)
    style.configure("Dark.TButton",
                    background=BTN_BG, foreground=BTN_FG,
                    borderwidth=0, focusthickness=0, padding=(10, 4))
    style.map("Dark.TButton",
              background=[("active", "#30363d")],
              foreground=[("disabled", "#777777")])

    frame = ttk.Frame(win, style="Dark.TFrame", padding=12)
    frame.grid(row=0, column=0, sticky="nsew")

    # Иконка
    icon_chars = {
        "info": "i",
        "warning": "!",
        "error": "×",
        "question": "?"
    }
    icon_lbl = ttk.Label(frame, text=icon_chars.get(icon, "i"),
                         style="Dark.TLabel",
                         font=("Segoe UI", 20, "bold"), foreground=ACCENT)
    icon_lbl.grid(row=0, column=0, padx=(0, 10), sticky="n")

    # Текст
    msg_lbl = ttk.Label(frame, text=message, style="Dark.TLabel",
                        justify="left", wraplength=360)
    msg_lbl.grid(row=0, column=1, sticky="w")

    # Кнопки
    btn_frame = ttk.Frame(frame, style="Dark.TFrame")
    btn_frame.grid(row=1, column=0, columnspan=2, pady=(12, 0), sticky="e")

    result = {"index": default_index}

    def on_click(idx):
        result["index"] = idx
        win.destroy()

    for idx, text in enumerate(buttons):
        b = ttk.Button(btn_frame, text=text, style="Dark.TButton",
                       command=lambda i=idx: on_click(i))
        b.grid(row=0, column=idx, padx=5)
        if idx == default_index:
            b.focus_set()
            win.bind("<Return>", lambda e, i=idx: on_click(i))

    _center_window(win, parent)
    win.wait_window()
    return result["index"]


def dark_showinfo(title, message, parent=None):
    _dark_messagebox(title, message, icon="info", buttons=("OK",), parent=parent)


def dark_showwarning(title, message, parent=None):
    _dark_messagebox(title, message, icon="warning", buttons=("OK",), parent=parent)


def dark_showerror(title, message, parent=None):
    _dark_messagebox(title, message, icon="error", buttons=("OK",), parent=parent)


def dark_askyesno(title, message, parent=None):
    idx = _dark_messagebox(
        title,
        message,
        icon="question",
        buttons=("Да", "Нет"),
        default_index=0,
        parent=parent,
    )
    return idx == 0
