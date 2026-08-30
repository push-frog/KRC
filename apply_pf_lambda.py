#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
from pathlib import Path

GUI = Path(__file__).resolve().parent / "krc" / "gui.py"


def main():
    text = GUI.read_text(encoding="utf-8")
    if "lambda m=err:" in text or "lambda m=err :" in text:
        print("Already patched:", GUI)
        return 0

    pat = re.compile(
        r"self\.root\.after\(\s*0,\s*lambda:\s*self\._log\(\s*"
        r"f\"\[PF\][^\"]*\{e\}[^\"]*\",\s*\"ERROR\"\s*\)\s*\)",
        re.S,
    )
    repl = (
        "err = f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}\"\n"
        "            self.root.after(0, lambda m=err: self._log(m, \"ERROR\"))"
    )
    new, n = pat.subn(repl, text, count=1)
    if n == 0:
        # last-resort: replace only the f-string inside existing lambda
        if '{e}", "ERROR"' in text and "[PF]" in text:
            new = text.replace(
                'f"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}", "ERROR"',
                'err, "ERROR"',
                1,
            )
            if "err = f" not in new:
                new = new.replace(
                    "except Exception as e:",
                    "except Exception as e:\n"
                    "            err = f\"[PF] \u041e\u0448\u0438\u0431\u043a\u0430: {e}\"",
                    1,
                )
            n = 1
        else:
            raise SystemExit("Could not find PF after-lambda in krc/gui.py")
    GUI.write_text(new, encoding="utf-8")
    print("Patched", GUI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
