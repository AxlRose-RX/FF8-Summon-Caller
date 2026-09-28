#!/usr/bin/env python3
"""
ff8_summon_caller.py

Summon-caller for FF8 (OG PC build: 2000 / 2013, FF8_EN.exe, with FFNx).
Companion to the battle teleport tool.

How it works
------------
FF8 fills a small "pending action" buffer when you pick a command in the
battle menu. This tool writes an entry into that buffer (0x1D28D44), so the
game runs the action on the chosen character's turn, exactly as if you had
selected it. A button = "that character summons X".

    - GF buttons use the GF command (command_id 0x03) with the kernel GF id
      (0x40..0x4F = the 16 junctionable GFs). Verified in the exe. Each attacker
      slot is written at PENDING_BUFFER + slot*0x18.

Not here (yet)
--------------
The special summons (Boko, Phoenix, Moomba, MiniMog, Odin, Gilgamesh) are NOT
player commands in FF8: Boko/Phoenix/Moomba fire from items, MiniMog is a
learned command, and Odin/Gilgamesh appear at random on a timer. So the
command-buffer trick that summons a GF can't fire them; they come out of the
item system and the random-appearance code, which needs a different hook.

Use it
------
1. Start FF8 (OG PC build) with FFNx, get into a battle.
2. Attach to FF8.
3. Pick the attacker slot (0, 1 or 2 = the party member, top to bottom).
4. When it is that character's turn (their command menu is up), click a summon.

Notes
-----
- OG PC build only (image base 0x400000). It refuses to write on any other
  build (e.g. the Remaster), so it can't crash the wrong exe.
- The pending entry is consumed on the character's turn. If nothing happens,
  it usually was not that character's turn yet, click again when the menu is up.
- Live memory writes need pymem and are Windows-only.

    pip install pymem
    python ff8_summon_caller.py
"""

import os
import sys
import struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk

APP_VERSION = "2026.0927"   # release version (YYYY.MMDD); the GitHub build reads it from here


def get_exe_dir():
    """Folder of the running .exe (when frozen by PyInstaller) or of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------- addresses
# FF8 OG PC (FF8_EN.exe) is a non-relocatable exe (loads at 0x400000), so this
# is an absolute virtual address, not a module-relative offset.
PENDING_BUFFER      = 0x1D28D44   # battle pending-action buffer
ENTRY_SIZE          = 8           # one action entry is 8 bytes
SLOT_STRIDE         = 0x18        # each attacker slot owns a 3-entry (0x18) block
IMAGE_BASE_EXPECTED = 0x400000
PROC_NAMES          = ["FF8.exe", "FF8_EN.exe"]

# pending entry layout (little-endian):
#   <H target_mask><B attacker_slot><B command_id><B command_arg><B pad><B pad><B active>
CMD_GF   = 0x03               # GF command: arg = kernel GF id 0x40..0x4F
CMD_ITEM = 0x04               # item-effect command (the exe reads summon items here)
CMD_NJGF = 0xF4               # non-junctionable GF attack: arg = table index 0..15
TARGET_ALL_ENEMIES = 0x8008   # from the verified "GF Ifrit, slot 0" example

# junctionable GFs: command_arg = kernel GF id (0x40..0x4F)
GFS = [
    (0x40, "Quezacotl"), (0x41, "Shiva"),     (0x42, "Ifrit"),     (0x43, "Siren"),
    (0x44, "Brothers"),  (0x45, "Diablos"),   (0x46, "Carbuncle"), (0x47, "Leviathan"),
    (0x48, "Pandemona"), (0x49, "Cerberus"),  (0x4A, "Alexander"), (0x4B, "Doomtrain"),
    (0x4C, "Bahamut"),   (0x4D, "Cactuar"),   (0x4E, "Tonberry"),  (0x4F, "Eden"),
]

# item summons: command_id 0x04, command_arg = kernel item id (the exe resolves the summon)
ITEM_SUMMONS = [
    (0x1F, "Phoenix", "Phoenix Pinion"),
    (0x20, "Moomba",  "Friendship"),
]

# Boko needs to be "available": [0x1CFEFB8] low bit on, then [0x1CFEFE5] picks the tier
BOKO_ITEM_ID   = 0x1E             # Gysahl Greens
BOKO_AVAIL_ADDR = 0x1CFEFB8       # byte: low bit on = Boko available (tier byte then used)
BOKO_TIER_ADDR  = 0x1CFEFE5       # byte: 0..3 = ChocoFire/Flare/Meteor/ChocoBocle
BOKO_TIERS = [("ChocoFire", 0), ("ChocoFlare", 1), ("ChocoMeteor", 2), ("Fat Chocobo", 3)]

# rare summons via the direct type-0xF4 path; command_arg = non-junctionable GF attack index
RARE_SUMMONS = [
    ("Odin", 0),
    ("MiniMog", 6),
    ("Gilg: Excalibur", 7),
    ("Gilg: Excalipoor", 8),
    ("Gilg: Masamune", 9),
    ("Gilg: Zantetsuken", 10),
]


# ---------------------------------------------------------------- memory
class Mem:
    def __init__(self, log):
        self.pm = None
        self.log = log
        self.base = None
        self.name = None

    def attached(self):
        return self.pm is not None

    def attach(self):
        try:
            import pymem
        except ImportError:
            self.log("pymem is not installed. Run:  pip install pymem")
            return False

        last = None
        for name in PROC_NAMES:
            try:
                self.pm = pymem.Pymem(name)
                self.name = name
                break
            except Exception as e:
                last = e
                self.pm = None

        if not self.pm:
            self.log(f"FF8 not found ({', '.join(PROC_NAMES)}). Is it running? ({last})")
            return False

        try:
            self.base = self.pm.base_address
        except Exception:
            self.base = None

        self.log(f"Attached: {self.name} (PID {self.pm.process_id}).")
        if self.base not in (None, IMAGE_BASE_EXPECTED):
            self.log(f"WARNING: image base is 0x{self.base:X}, expected 0x{IMAGE_BASE_EXPECTED:X}. "
                     "This is not the OG PC build; writes are disabled to avoid a crash.")
        return True

    def detach(self):
        self.pm = None
        self.base = None
        self.name = None

    def can_write(self):
        return self.pm is not None and self.base in (None, IMAGE_BASE_EXPECTED)

    def write_byte(self, addr, value):
        if not self.pm:
            self.log("Attach to FF8 first.")
            return False
        if not self.can_write():
            self.log("Writes disabled: this is not the OG PC build (image base 0x400000).")
            return False
        try:
            self.pm.write_bytes(addr, bytes([value & 0xFF]), 1)
            return True
        except Exception as e:
            self.log(f"Write failed: {e}")
            return False

    def write_pending(self, slot, command_id, command_arg, target_mask=TARGET_ALL_ENEMIES):
        if not self.pm:
            self.log("Attach to FF8 first.")
            return False
        if not self.can_write():
            self.log("Writes disabled: this is not the OG PC build (image base 0x400000).")
            return False
        entry = struct.pack("<HBBBBBB", target_mask & 0xFFFF, slot & 0xFF,
                            command_id & 0xFF, command_arg & 0xFF, 0, 0, 1)
        addr = PENDING_BUFFER + slot * SLOT_STRIDE
        try:
            self.pm.write_bytes(addr, entry, len(entry))
            return True
        except Exception as e:
            self.log(f"Write failed: {e}")
            return False


# ---------------------------------------------------------------- UI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"FF8 Summon Caller v{APP_VERSION} - by AxlRose")
        self.geometry("560x820")
        self.minsize(520, 720)

        # Set custom icon (icon.ico lives in the _internal bundle folder)
        try:
            base = Path(getattr(sys, "_MEIPASS", get_exe_dir()))
            icon_path = base / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass

        self.mem = Mem(self._log)
        self.slot_var = tk.IntVar(value=0)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Attach to FF8", command=self._attach).pack(side="left")
        self.status_var = tk.StringVar(value="Not attached.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)

        slot = ttk.LabelFrame(self, text="Attacker (party member, top to bottom)", padding=8)
        slot.pack(fill="x", padx=8)
        for i in range(3):
            ttk.Radiobutton(slot, text=f"Slot {i}", value=i,
                            variable=self.slot_var).pack(side="left", padx=(0, 12))

        gf = ttk.LabelFrame(self, text="Guardian Forces", padding=8)
        gf.pack(fill="x", padx=8, pady=(8, 0))
        for idx, (gid, name) in enumerate(GFS):
            b = ttk.Button(gf, text=name, width=12,
                           command=lambda g=gid, n=name: self._summon_gf(g, n))
            b.grid(row=idx // 4, column=idx % 4, padx=3, pady=3, sticky="ew")
        for c in range(4):
            gf.columnconfigure(c, weight=1)

        it = ttk.LabelFrame(self, text="Item summons (command 0x04)", padding=8)
        it.pack(fill="x", padx=8, pady=(8, 0))
        for idx, (iid, name, item) in enumerate(ITEM_SUMMONS):
            ttk.Button(it, text=name, width=12,
                       command=lambda i=iid, n=name, t=item: self._summon_item(i, n, t)).grid(
                           row=0, column=idx, padx=3, pady=3, sticky="ew")
        for c in range(len(ITEM_SUMMONS)):
            it.columnconfigure(c, weight=1)

        boko = ttk.LabelFrame(self, text="Boko (Gysahl Greens) -- sets availability + tier, then summons", padding=8)
        boko.pack(fill="x", padx=8, pady=(8, 0))
        for idx, (name, val) in enumerate(BOKO_TIERS):
            ttk.Button(boko, text=name, width=12,
                       command=lambda n=name, v=val: self._summon_boko(n, v)).grid(
                           row=0, column=idx, padx=3, pady=3, sticky="ew")
        for c in range(len(BOKO_TIERS)):
            boko.columnconfigure(c, weight=1)

        rare = ttk.LabelFrame(self, text="Rare summons (direct, command 0xF4)", padding=8)
        rare.pack(fill="x", padx=8, pady=(8, 0))
        for idx, (name, arg) in enumerate(RARE_SUMMONS):
            ttk.Button(rare, text=name, width=14,
                       command=lambda n=name, a=arg: self._summon_rare(n, a)).grid(
                           row=idx // 3, column=idx % 3, padx=3, pady=3, sticky="ew")
        for c in range(3):
            rare.columnconfigure(c, weight=1)

        logf = ttk.LabelFrame(self, text="Log", padding=6)
        logf.pack(fill="both", expand=True, padx=8, pady=8)
        self.logbox = tk.Text(logf, height=7, wrap="word")
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")

        ttk.Label(self, padding=(8, 0), foreground="#666",
                  text="Click a summon when it is that slot's turn (command menu up).").pack(fill="x")

    # ---- helpers
    def _log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _attach(self):
        if self.mem.attach():
            self.status_var.set(f"Attached: {self.mem.name}")
        else:
            self.status_var.set("Not attached (see log).")

    def _summon_gf(self, gf_id, name):
        slot = self.slot_var.get()
        if self.mem.write_pending(slot, CMD_GF, gf_id):
            self._log(f"Queued {name} from slot {slot}. Fires on that character's turn.")

    def _summon_item(self, item_id, name, item):
        slot = self.slot_var.get()
        if self.mem.write_pending(slot, CMD_ITEM, item_id):
            self._log(f"Queued {name} ({item}, cmd 0x04 arg 0x{item_id:02X}) from slot {slot}. "
                      f"Fires on that character's turn.")

    def _summon_boko(self, name, tier):
        slot = self.slot_var.get()
        # make Boko available (low bit on, clear the rest) and set the tier, then summon
        self.mem.write_byte(BOKO_AVAIL_ADDR, 1)
        self.mem.write_byte(BOKO_TIER_ADDR, tier)
        if self.mem.write_pending(slot, CMD_ITEM, BOKO_ITEM_ID):
            self._log(f"Queued Boko {name} (tier {tier}) from slot {slot}. Fires on that character's turn.")

    def _summon_rare(self, name, arg):
        slot = self.slot_var.get()
        if self.mem.write_pending(slot, CMD_NJGF, arg):
            self._log(f"Queued {name} (cmd 0xF4 index {arg}) from slot {slot}. Fires on that character's turn.")


    def _on_close(self):
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
