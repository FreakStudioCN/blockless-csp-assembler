import json
from pathlib import Path

import pcbnew
import wx

from .board_builder import build_board_from_plan


class AiModularBackplanePlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "AI Modular Backplane"
        self.category = "AI PCB"
        self.description = "Build a real KiCad PCB from an AI module plan JSON."
        self.show_toolbar_button = True

    def Run(self):
        default_dir = Path.cwd()
        dialog = wx.FileDialog(
            None,
            "Open AI module plan JSON",
            str(default_dir),
            "",
            "JSON files (*.json)|*.json",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() != wx.ID_OK:
            return

        path = Path(dialog.GetPath())
        with path.open("r", encoding="utf-8") as handle:
            plan = json.load(handle)

        board = pcbnew.GetBoard()
        build_board_from_plan(board, plan)
        pcbnew.Refresh()
        wx.MessageBox(
            f"Built AI modular backplane:\n{plan.get('title', path.name)}",
            "AI Modular Backplane",
            wx.OK | wx.ICON_INFORMATION,
        )
