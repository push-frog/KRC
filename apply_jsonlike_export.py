#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

GUI = Path(__file__).resolve().parent / "krc" / "gui.py"

IMPORT_OLD_A = "from .util import BATCH_CHUNK_SIZE, DEFAULT_INTERFACE, save_routes_to_file"
IMPORT_OLD_B = "from .util import DEFAULT_INTERFACE, save_routes_to_file"
IMPORT_NEW = "from .util import BATCH_CHUNK_SIZE, DEFAULT_INTERFACE, save_routes_jsonlike, save_routes_to_file"

NEEDLE = "        self.save_routes_btn.pack(side=tk.LEFT, padx=(0, 4))"

BUTTON_LINES = [
    "        self.save_routes_json_btn = ttk.Button(",
    "            cf, text=\"\U0001f4be Сохранить в JSON-like\",",
    "            command=self._save_routes_jsonlike, state=tk.DISABLED)",
    "        self.save_routes_json_btn.pack(side=tk.LEFT, padx=(0, 4))",
]

METHOD_LINES = [
    "    def _save_routes_jsonlike(self):",
    "        if not self._current_routes:",
    "            messagebox.showwarning(\"Нет данных\", \"Список маршрутов пуст.\")",
    "            return",
    "        filepath = filedialog.asksaveasfilename(",
    "            title=\"Сохранить маршруты (JSON-like)\",",
    "            defaultextension=\".txt\",",
    "            filetypes=[(\"Text\", \"*.txt\"), (\"JSON-like\", \"*.json\"),",
    "                       (\"All\", \"*.*\")],",
    "            initialfile=\"routes.jsonlike.txt\")",
    "        if not filepath:",
    "            return",
    "        try:",
    "            count = save_routes_jsonlike(self._current_routes, filepath)",
    "            self._log(f\"JSON-like: {count} -> {filepath}\", \"SUCCESS\")",
    "            messagebox.showinfo(",
    "                \"Сохранено\",",
    "                f\"Сетей: {count}\\n{filepath}\")",
    "        except Exception as e:",
    "            self._log(f\"Ошибка: {e}\", \"ERROR\")",
    "            messagebox.showerror(\"Ошибка\", str(e))",
    "",
]


def main():
    text = GUI.read_text(encoding="utf-8")
    if "def _save_routes_jsonlike" in text and "save_routes_json_btn" in text:
        print("Already patched.")
        return 0

    if IMPORT_NEW not in text:
        if IMPORT_OLD_A in text:
            text = text.replace(IMPORT_OLD_A, IMPORT_NEW, 1)
        elif IMPORT_OLD_B in text:
            text = text.replace(IMPORT_OLD_B, IMPORT_NEW, 1)
        else:
            raise SystemExit("Could not update util import in krc/gui.py")

    if "save_routes_json_btn" not in text:
        if NEEDLE not in text:
            raise SystemExit("Could not find save_routes_btn.pack in krc/gui.py")
        button = NEEDLE + "\n" + "\n".join(BUTTON_LINES)
        text = text.replace(NEEDLE, button, 1)

    if "self.save_routes_json_btn" not in text.split("_set_buttons_connected", 1)[-1][:1200]:
        text = text.replace(
            "self.save_routes_btn,",
            "self.save_routes_btn, self.save_routes_json_btn,",
            1,
        )

    if "def _save_routes_jsonlike" not in text:
        marker = "    def _save_routes_to_file(self):"
        if marker not in text:
            raise SystemExit("Could not find _save_routes_to_file")
        method = "\n".join(METHOD_LINES) + "\n"
        text = text.replace(marker, method + marker, 1)

    GUI.write_text(text, encoding="utf-8")
    print("Patched", GUI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
