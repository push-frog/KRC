#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

GUI = Path(__file__).resolve().parent / "krc" / "gui.py"

OLD = '''            self.root.after(
                0, lambda: self._log(
                    f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}", "ERROR"))
'''

NEW = '''            err = str(e)
            self.root.after(
                0, lambda m=err: self._log(
                    f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {m}", "ERROR"))
'''

# looser fallbacks
OLD2 = 'f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}", "ERROR"'
NEW2 = 'f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {m}", "ERROR"'


def main():
    text = GUI.read_text(encoding="utf-8")
    if "lambda m=err:" in text and "[PF]" in text:
        print("GUI PF lambda already patched")
        return 0
    if OLD in text:
        text = text.replace(OLD, NEW, 1)
    elif OLD2 in text:
        text = text.replace(
            "self.root.after(\n                0, lambda: self._log(\n                    f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}\", \"ERROR\"))",
            "err = str(e)\n            self.root.after(\n                0, lambda m=err: self._log(\n                    f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {m}\", \"ERROR\"))",
            1,
        )
        if OLD2 in text and "lambda m=err" not in text:
            text = text.replace(
                "lambda: self._log(\n                    f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}\", \"ERROR\")",
                "lambda m=str(e): self._log(\n                    f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {m}\", \"ERROR\")",
                1,
            )
    else:
        # generic: bind except e used in after-lambda
        needle = 'f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}"'
        if needle not in text:
            raise SystemExit("PF error log line not found in gui.py")
        text = text.replace(needle, 'f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {str(e)}"', 1)
    GUI.write_text(text, encoding="utf-8")
    print("Patched", GUI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
