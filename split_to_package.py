#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot split of the original monolith keenetic.py into the krc package.

Does not change class/method bodies. Run from the repo root:

    python split_to_package.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "keenetic.py"
PKG = ROOT / "krc"


def main():
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    index = {}
    for i, line in enumerate(lines):
        if line.startswith("class "):
            name = line[6:].split("(")[0].split(":")[0].strip()
            index[name] = i
        elif line.startswith("def main"):
            index["main"] = i

    def block(name, end_name=None):
        start = index[name]
        end = index[end_name] if end_name else len(lines)
        return "\n".join(lines[start:end]).rstrip() + "\n"

    if "from krc.gui import KeeneticAdvancedGUI" in text and "class KeeneticAdvancedGUI" not in text:
        print("keenetic.py is already the launcher. Nothing to split.")
        print("Need the original monolith. Restore it first:")
        print("  git checkout d8483da -- keenetic.py")
        return 1

    if "KeeneticAdvancedClient" not in index or "KeeneticAdvancedGUI" not in index:
        raise SystemExit("keenetic.py does not look like the original app")

    PKG.mkdir(exist_ok=True)

    util_start = next(i for i, l in enumerate(lines) if l.startswith("DEFAULT_INTERFACE"))
    util_end = index["KeeneticAdvancedClient"]
    util_body = "\n".join(lines[util_start:util_end]).rstrip() + "\n"

    (PKG / "util.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "from typing import Optional\n\n"
        + util_body,
        encoding="utf-8",
    )

    (PKG / "client.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import asyncio\n"
        "import re\n"
        "from typing import Dict, List, Optional\n\n"
        "import telnetlib3\n\n"
        "from .util import (\n"
        "    BATCH_CHUNK_SIZE,\n"
        "    DEFAULT_INTERFACE,\n"
        "    decode_output,\n"
        "    mask_to_cidr,\n"
        "    strip_ansi,\n"
        ")\n\n"
        + block("KeeneticAdvancedClient", "SysmonRawDialog"),
        encoding="utf-8",
    )

    (PKG / "dialogs.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import threading\n"
        "import tkinter as tk\n"
        "from tkinter import filedialog, messagebox, scrolledtext, ttk\n"
        "from typing import Dict, List\n\n"
        "from .util import BATCH_CHUNK_SIZE, DEFAULT_INTERFACE\n\n"
        + block("SysmonRawDialog", "KeeneticAdvancedGUI"),
        encoding="utf-8",
    )

    (PKG / "gui.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import asyncio\n"
        "import re\n"
        "import threading\n"
        "import tkinter as tk\n"
        "from datetime import datetime\n"
        "from tkinter import filedialog, messagebox, scrolledtext, ttk\n"
        "from typing import Dict, List, Optional\n\n"
        "from .client import KeeneticAdvancedClient\n"
        "from .dialogs import (\n"
        "    AddPortForwardDialog,\n"
        "    AddRouteDialog,\n"
        "    BatchImportDialog,\n"
        "    NatSessionsDialog,\n"
        "    PFDiagnosticDialog,\n"
        "    SysmonRawDialog,\n"
        ")\n"
        "from .util import DEFAULT_INTERFACE, save_routes_to_file\n\n"
        + block("KeeneticAdvancedGUI", "main"),
        encoding="utf-8",
    )

    (PKG / "__init__.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import asyncio\n"
        "import sys\n\n"
        "if sys.platform == \"win32\":\n"
        "    try:\n"
        "        asyncio.set_event_loop_policy(\n"
        "            asyncio.WindowsSelectorEventLoopPolicy())\n"
        "    except AttributeError:\n"
        "        pass\n\n"
        "from .client import KeeneticAdvancedClient\n"
        "from .gui import KeeneticAdvancedGUI\n"
        "from .util import (\n"
        "    BATCH_CHUNK_SIZE,\n"
        "    DEFAULT_INTERFACE,\n"
        "    cidr_to_mask,\n"
        "    decode_output,\n"
        "    mask_to_cidr,\n"
        "    routes_to_bat_lines,\n"
        "    save_routes_to_file,\n"
        "    strip_ansi,\n"
        ")\n\n"
        "__all__ = [\n"
        "    \"BATCH_CHUNK_SIZE\",\n"
        "    \"DEFAULT_INTERFACE\",\n"
        "    \"KeeneticAdvancedClient\",\n"
        "    \"KeeneticAdvancedGUI\",\n"
        "    \"cidr_to_mask\",\n"
        "    \"decode_output\",\n"
        "    \"mask_to_cidr\",\n"
        "    \"routes_to_bat_lines\",\n"
        "    \"save_routes_to_file\",\n"
        "    \"strip_ansi\",\n"
        "]\n",
        encoding="utf-8",
    )

    SRC.write_text(
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n\n"
        "from krc.gui import KeeneticAdvancedGUI\n\n\n"
        "def main():\n"
        "    app = KeeneticAdvancedGUI()\n"
        "    app.run()\n\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n",
        encoding="utf-8",
    )

    print("Split complete.")
    print("Launch as before:  python keenetic.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
