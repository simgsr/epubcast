#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# A simple wxWidgets UI for epubcast

import numpy as np
import soundfile
import threading
import platform
import subprocess
import traceback
import io
import os
import wx
from wx.lib.newevent import NewEvent
from wx.lib.scrolledpanel import ScrolledPanel
from PIL import Image
from tempfile import NamedTemporaryFile

from epubcast import DEFAULT_OUTPUT_FOLDER
from epubcast.backends import edge_available, mlx_available, resolve_backend, torch_available
from epubcast.voices import (voices, flags, edge_voices, edge_flags, lang_code_for,
                             default_languages, is_catalog_voice)

EVENTS = {
    'CORE_STARTED': NewEvent(),
    'CORE_PROGRESS': NewEvent(),
    'CORE_CHAPTER_STARTED': NewEvent(),
    'CORE_CHAPTER_FINISHED': NewEvent(),
    'CORE_FINISHED': NewEvent(),
    # Synthesis raised. Without this the worker thread died with its traceback on a terminal
    # nobody is looking at, leaving the window frozen mid-run with no way back.
    'CORE_FAILED': NewEvent(),
}

border = 5

# The pixel sizes in this file were picked on a 14" laptop; ui_scale stretches or shrinks
# them so the same layout fits a small screen and still uses the room on a large one.
REFERENCE_SCREEN_WIDTH = 1512


def current_display():
    """The display the pointer is on, falling back to the primary one."""
    index = wx.Display.GetFromPoint(wx.GetMousePosition())
    return wx.Display(index if index != wx.NOT_FOUND else 0)


class MainWindow(wx.Frame):
    def __init__(self, parent, title):
        # The *usable* rectangle of the display: unlike wx.GetDisplaySize() this leaves out
        # the macOS menu bar and dock, and the Windows taskbar.
        self.work_area = current_display().GetClientArea()
        self.ui_scale = min(max(self.work_area.width / REFERENCE_SCREEN_WIDTH, 0.72), 1.5)
        window_width = min(int(self.work_area.width * 0.9), self.scaled(1400))
        window_height = min(int(self.work_area.height * 0.9), window_width * 3 // 4)
        super().__init__(parent, title=title, size=(window_width, window_height))
        # The floor only ever shrinks: a big monitor is no reason to stop someone making
        # the window small, but a small screen must be able to hold the whole frame.
        shrink = min(self.ui_scale, 1.0)
        self.SetMinSize((min(int(820 * shrink), self.work_area.width),
                         min(int(560 * shrink), self.work_area.height)))
        self.chapters_panel = None
        self.preview_threads = []
        self.selected_chapter = None
        self.selected_book = None
        self.synthesis_in_progress = False
        self.selected_backend = 'auto'  # the params panel only exists once a book is open

        self.Bind(EVENTS['CORE_STARTED'][1], self.on_core_started)
        self.Bind(EVENTS['CORE_CHAPTER_STARTED'][1], self.on_core_chapter_started)
        self.Bind(EVENTS['CORE_CHAPTER_FINISHED'][1], self.on_core_chapter_finished)
        self.Bind(EVENTS['CORE_PROGRESS'][1], self.on_core_progress)
        self.Bind(EVENTS['CORE_FINISHED'][1], self.on_core_finished)
        self.Bind(EVENTS['CORE_FAILED'][1], self.on_core_failed)

        self.create_menu()
        self.create_layout()
        self.centre_on_work_area()
        self.Show(True)

    def scaled(self, size):
        """A pixel size from the reference layout, adjusted for this screen."""
        return int(round(size * self.ui_scale))

    def centre_on_work_area(self):
        """Centre in the usable area, so the title bar never hides under the menu bar."""
        width, height = self.GetSize()
        self.SetPosition((self.work_area.x + max(0, (self.work_area.width - width) // 2),
                          self.work_area.y + max(0, (self.work_area.height - height) // 2)))

    def fit_to_work_area(self):
        """Grow to what the loaded book's layout needs, but never past the usable screen.

        Opening a book adds the chapter table and the parameters column, which need more
        room than the empty window; without this the extra panels are simply cut off.
        """
        best, current = self.GetBestSize(), self.GetSize()
        height_wanted = best.height
        if self.chapters_panel is not None:
            # The parameters column can always scroll, so it reports a tiny best height and
            # never asks the frame for room. Ask its sizer what it would rather have, and
            # give it that much whenever the screen has it to spare.
            chrome = current.height - self.right_panel.GetSize().height
            height_wanted = max(height_wanted, self.right_panel.GetSizer().CalcMin().height + chrome)
        width = min(max(best.width, current.width), self.work_area.width)
        height = min(max(height_wanted, current.height), self.work_area.height)
        if (width, height) != tuple(current):
            self.SetSize(width, height)
            self.Layout()
        # Only pull the window back if it now hangs off the screen: otherwise leave it
        # wherever the user dragged it.
        x, y = self.GetPosition()
        self.SetPosition((min(max(x, self.work_area.x), self.work_area.x + self.work_area.width - width),
                          min(max(y, self.work_area.y), self.work_area.y + self.work_area.height - height)))

    def create_menu(self):
        menubar = wx.MenuBar()
        file_menu = wx.Menu()
        open_item = wx.MenuItem(file_menu, wx.ID_OPEN, "&Open\tCtrl+O")
        file_menu.Append(open_item)
        self.Bind(wx.EVT_MENU, self.on_open, open_item)  # Bind the event

        exit_item = wx.MenuItem(file_menu, wx.ID_EXIT, "&Exit\tCtrl+Q")
        file_menu.Append(exit_item)
        self.Bind(wx.EVT_MENU, self.on_exit, exit_item)

        menubar.Append(file_menu, "&File")
        self.SetMenuBar(menubar)

    def on_core_started(self, event):
        self.progress_bar_label.Show()
        self.progress_bar.Show()
        self.progress_bar.SetValue(0)
        self.progress_bar.Layout()
        self.eta_label.Show()
        self.params_panel.Layout()
        self.synth_panel.Layout()

    def on_core_chapter_started(self, event):
        self.set_table_chapter_status(event.chapter_index, "⏳ In Progress")

    def on_core_chapter_finished(self, event):
        self.set_table_chapter_status(event.chapter_index, "✅ Done")
        self.start_button.Show()

    def on_core_progress(self, event):
        self.progress_bar.SetValue(event.stats.progress)
        self.progress_bar_label.SetLabel(f"Synthesis Progress: {event.stats.progress}%")
        self.eta_label.SetLabel(f"Estimated Time Remaining: {event.stats.eta}")
        self.synth_panel.Layout()

    def on_core_finished(self, event):
        self.synthesis_in_progress = False
        self.open_folder_with_explorer(self.output_folder_text_ctrl.GetValue())

    def on_core_failed(self, event):
        """Show what went wrong and hand the window back.

        on_start disables the start button, the params panel and the chapter checkboxes for
        the duration of the run; if synthesis raises, nothing else re-enables them, so the
        error has to undo that too or the only way out is to quit the app.
        """
        print(event.details)
        self.synthesis_in_progress = False
        self.re_enable_after_synthesis()
        wx.MessageBox(f'Synthesis failed:\n\n{event.message}\n\n'
                      'The full traceback is on the terminal.',
                      'Epubcast', wx.OK | wx.ICON_ERROR, self)

    def re_enable_after_synthesis(self):
        """Undo the disabling on_start did. Safe to call before any book is open."""
        if self.chapters_panel is None:
            return
        self.start_button.Enable()
        self.start_button.Show()
        self.params_panel.Enable()
        self.table.EnableCheckBoxes(True)

    def create_layout(self):
        # Panels layout looks like this:
        # splitter
        #     splitter_left
        #         chapters_panel
        #     splitter_right
        #         center_panel
        #             text_area
        #         right_panel
        #             book_info_panel_box
        #                 book_info_panel
        #                     cover_bitmap
        #                     book_details_panel
        #             param_panel_box
        #                  param_panel
        #                      ...
        #             synth_panel_box
        #                  synth_panel
        #                      start_button
        #                      ...

        top_panel = wx.Panel(self)
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        top_panel.SetSizer(top_sizer)

        # Open Epub button
        open_epub_button = wx.Button(top_panel, label="📁 Open EPUB")
        open_epub_button.Bind(wx.EVT_BUTTON, self.on_open)
        top_sizer.Add(open_epub_button, 0, wx.ALL, 5)

        # About button
        help_button = wx.Button(top_panel, label="ℹ️ About")
        help_button.Bind(wx.EVT_BUTTON, lambda event: self.about_dialog())
        top_sizer.Add(help_button, 0, wx.ALL, 5)

        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.main_sizer)

        self.splitter = wx.Panel(self)
        self.splitter_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.splitter.SetSizer(self.splitter_sizer)

        self.main_sizer.Add(top_panel, 0, wx.ALL | wx.EXPAND, 5)
        self.main_sizer.Add(self.splitter, 1, wx.EXPAND)

    def create_layout_for_ebook(self, splitter):
        splitter_left = wx.Panel(splitter, -1)
        splitter_right = wx.Panel(self.splitter)
        self.splitter_left, self.splitter_right = splitter_left, splitter_right
        self.splitter_sizer.Add(splitter_left, 1, wx.ALL | wx.EXPAND, 5)
        self.splitter_sizer.Add(splitter_right, 2, wx.ALL | wx.EXPAND, 5)

        self.left_sizer = wx.BoxSizer(wx.VERTICAL)
        splitter_left.SetSizer(self.left_sizer)

        # add center panel with large text area
        self.center_panel = wx.Panel(splitter_right)
        self.center_sizer = wx.BoxSizer(wx.VERTICAL)
        self.center_panel.SetSizer(self.center_sizer)
        self.text_area = wx.TextCtrl(self.center_panel, style=wx.TE_MULTILINE)
        # A minimum, not a fixed size. The old hard-coded 40% of the window width was a
        # floor the sizer had to honour, and it squeezed the parameters column to zero.
        self.text_area.SetMinSize((self.scaled(280), self.scaled(160)))
        font = wx.Font(min(max(self.scaled(13), 10), 16), wx.MODERN, wx.NORMAL, wx.NORMAL)
        self.text_area.SetFont(font)
        # On text change, update the extracted_text attribute of the selected_chapter:
        self.text_area.Bind(wx.EVT_TEXT, lambda event: setattr(self.selected_chapter, 'extracted_text', self.text_area.GetValue()))

        self.chapter_label = wx.StaticText(self.center_panel)
        self.set_chapter_label(self.selected_chapter)
        preview_button = wx.Button(self.center_panel, label="🔊 Preview")
        preview_button.Bind(wx.EVT_BUTTON, self.on_preview_chapter)

        self.center_sizer.Add(self.chapter_label, 0, wx.ALL, 5)
        self.center_sizer.Add(preview_button, 0, wx.ALL, 5)
        self.center_sizer.Add(self.text_area, 1, wx.ALL | wx.EXPAND, 5)

        splitter_right_sizer = wx.BoxSizer(wx.HORIZONTAL)
        splitter_right.SetSizer(splitter_right_sizer)

        self.create_right_panel(splitter_right)
        splitter_right_sizer.Add(self.center_panel, 1, wx.ALL | wx.EXPAND, 5)
        splitter_right_sizer.Add(self.right_panel, 1, wx.ALL | wx.EXPAND, 5)

    def set_chapter_label(self, chapter):
        """Long section names wrap: as a single line they widen the whole centre column."""
        self.chapter_label.SetLabel(f'Edit / Preview content for section "{chapter.short_name}":')
        self.chapter_label.Wrap(max(self.scaled(260), self.center_panel.GetSize().width - 4 * border))
        self.center_panel.Layout()

    def about_dialog(self):
        msg = ("A simple tool to generate audiobooks from EPUB files using Kokoro-82M models\n" +
               "Distributed under the MIT License.\n\n" +
               "by Claudio Santini 2025\nand many contributors.\n\n" +
               "https://claudio.uk\n\n")
        wx.MessageBox(msg, "Epubcast")

    def create_right_panel(self, splitter_right):
        self.right_panel = ScrolledPanel(splitter_right, style=wx.TAB_TRAVERSAL)
        self.right_sizer = wx.BoxSizer(wx.VERTICAL)
        self.right_panel.SetSizer(self.right_sizer)

        self.book_info_panel_box = wx.Panel(self.right_panel, style=wx.SUNKEN_BORDER)
        book_info_panel_box_sizer = wx.StaticBoxSizer(wx.VERTICAL, self.book_info_panel_box, "Book Details")
        self.book_info_panel_box.SetSizer(book_info_panel_box_sizer)
        # Proportion 0: a wx.BoxSizer sizes stretchable children alike, so three boxes at
        # proportion 1 asked for three times the tallest one's height and overflowed.
        self.right_sizer.Add(self.book_info_panel_box, 0, wx.ALL | wx.EXPAND, border)

        self.book_info_panel = wx.Panel(self.book_info_panel_box, style=wx.BORDER_NONE)
        self.book_info_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.book_info_panel.SetSizer(self.book_info_sizer)
        book_info_panel_box_sizer.Add(self.book_info_panel, 1, wx.ALL | wx.EXPAND, border)

        # Add cover image
        self.cover_bitmap = wx.StaticBitmap(self.book_info_panel, -1)
        self.book_info_sizer.Add(self.cover_bitmap, 0, wx.ALL, 5)

        self.cover_bitmap.Refresh()
        self.book_info_panel.Refresh()
        self.book_info_panel.Layout()
        self.cover_bitmap.Layout()

        self.create_book_details_panel()
        self.create_params_panel()
        self.create_synthesis_panel()
        # The parameters are the tallest thing in the window: on a short screen, or in a
        # window the user has shrunk, this column scrolls rather than losing the Start
        # button off the bottom or the controls off the side.
        self.right_panel.SetupScrolling(scroll_x=True, scroll_y=True, scrollToTop=False)

    def create_book_details_panel(self):
        book_details_panel = wx.Panel(self.book_info_panel)
        book_details_sizer = wx.GridBagSizer(self.scaled(8), self.scaled(8))
        book_details_panel.SetSizer(book_details_sizer)
        self.book_info_sizer.Add(book_details_panel, 1, wx.ALL | wx.EXPAND, border)

        def add_row(row, label, value):
            """A label and a value that ellipsizes.

            Laid out at its natural width a long title made this box the widest thing in the
            window, which stretched the whole right column and squeezed the chapter list.
            """
            label_text = wx.StaticText(book_details_panel, label=label)
            book_details_sizer.Add(label_text, pos=(row, 0), flag=wx.ALL, border=border)
            value_text = wx.StaticText(book_details_panel, label=value, style=wx.ST_ELLIPSIZE_END)
            value_text.SetMinSize((self.scaled(95), -1))
            value_text.SetToolTip(value)
            book_details_sizer.Add(value_text, pos=(row, 1), flag=wx.ALL | wx.EXPAND, border=border)

        if not hasattr(self, 'document_chapters'):
            total_len = 0
        else:
            total_len = sum([len(c.extracted_text) for c in self.document_chapters])

        add_row(0, "Title:", self.selected_book_title)
        add_row(1, "Author:", self.selected_book_author)
        add_row(2, "Total Length:", f'{total_len:,} characters')
        book_details_sizer.AddGrowableCol(1)

    def create_params_panel(self):
        panel_box = wx.Panel(self.right_panel, style=wx.SUNKEN_BORDER)
        panel_box_sizer = wx.StaticBoxSizer(wx.VERTICAL, panel_box, "Audiobook Parameters")
        panel_box.SetSizer(panel_box_sizer)

        panel = self.params_panel = wx.Panel(panel_box)
        panel_box_sizer.Add(panel, 1, wx.ALL | wx.EXPAND, border)
        self.right_sizer.Add(panel_box, 1, wx.ALL | wx.EXPAND, border)
        sizer = wx.GridBagSizer(self.scaled(8), self.scaled(8))
        panel.SetSizer(sizer)

        # Backend: which TTS engine runs. MLX is Apple Silicon only and much faster.
        backend_label = wx.StaticText(panel, label="Backend:")
        self.selected_backend = 'auto'
        backend_choices = ['auto'] + (['mlx'] if mlx_available() else []) + \
                          (['torch'] if torch_available() else []) + \
                          (['edge'] if edge_available() else [])
        backend_dropdown = wx.ComboBox(panel, choices=backend_choices, value='auto', style=wx.CB_READONLY)
        backend_dropdown.Bind(wx.EVT_COMBOBOX, self.on_select_backend)
        sizer.Add(backend_label, pos=(0, 0), flag=wx.ALL, border=border)
        sizer.Add(backend_dropdown, pos=(0, 1), flag=wx.ALL, border=border)

        resolved = resolve_backend('auto')
        if torch_available():
            backend_note = f'"auto" uses the torch backend (portable)'
        elif mlx_available():
            backend_note = 'torch not installed — "auto" will use mlx'
        else:
            backend_note = f'"auto" will use {resolved}'
        self.backend_note = wx.StaticText(panel, label=backend_note)
        self.backend_note.SetForegroundColour(wx.Colour(110, 110, 110))
        self.backend_note.Wrap(self.scaled(200))
        sizer.Add(self.backend_note, pos=(1, 1), flag=wx.ALL, border=border)

        # Device only affects the torch backend; MLX always runs on the Apple GPU.
        self.device_label = wx.StaticText(panel, label="Torch device:")
        engine_radio_panel = self.device_panel = wx.Panel(panel)
        cpu_radio = wx.RadioButton(engine_radio_panel, label="CPU", style=wx.RB_GROUP)
        cuda_radio = self.cuda_radio = wx.RadioButton(engine_radio_panel, label="CUDA")
        cpu_radio.SetValue(True)
        cuda_radio.Disable()
        if torch_available():
            import torch
            if torch.cuda.is_available():
                cuda_radio.Enable()
                cuda_radio.SetValue(True)
            cpu_radio.Bind(wx.EVT_RADIOBUTTON, lambda event: torch.set_default_device('cpu'))
            cuda_radio.Bind(wx.EVT_RADIOBUTTON, lambda event: torch.set_default_device('cuda'))
        sizer.Add(self.device_label, pos=(2, 0), flag=wx.ALL, border=border)
        sizer.Add(engine_radio_panel, pos=(2, 1), flag=wx.ALL, border=border)
        engine_radio_panel_sizer = wx.BoxSizer(wx.HORIZONTAL)
        engine_radio_panel.SetSizer(engine_radio_panel_sizer)
        engine_radio_panel_sizer.Add(cpu_radio, 0, wx.ALL, 5)
        engine_radio_panel_sizer.Add(cuda_radio, 0, wx.ALL, 5)
        self.update_device_row()

        # Languages: multi-select, so the voice dropdown only shows the languages you care
        # about. The list depends on the backend (Kokoro codes vs Edge locales).
        language_label = wx.StaticText(panel, label="Languages:")
        self.language_listbox = wx.CheckListBox(panel, size=(self.scaled(180), self.scaled(110)))
        self.language_listbox.Bind(wx.EVT_CHECKLISTBOX, self.on_select_languages)
        sizer.Add(language_label, pos=(3, 0), flag=wx.ALL, border=border)
        sizer.Add(self.language_listbox, pos=(3, 1), flag=wx.ALL | wx.EXPAND, border=border)

        voice_label = wx.StaticText(panel, label="Voice:")
        self.selected_voice = ''
        # Editable: a voice can also be a blend ("af_heart,af_bella"), a path to a .pt
        # pack, or an Edge voice name typed in full.
        self.voice_dropdown = wx.ComboBox(panel, choices=[], value='')
        self.voice_dropdown.Bind(wx.EVT_COMBOBOX, self.on_select_voice)
        self.voice_dropdown.Bind(wx.EVT_TEXT, self.on_select_voice)
        sizer.Add(voice_label, pos=(4, 0), flag=wx.ALL, border=border)
        sizer.Add(self.voice_dropdown, pos=(4, 1), flag=wx.ALL | wx.EXPAND, border=border)

        voice_note = wx.StaticText(panel, label='Blend voices with commas, or type a path to a .pt voice')
        voice_note.SetForegroundColour(wx.Colour(110, 110, 110))
        voice_note.Wrap(self.scaled(200))
        sizer.Add(voice_note, pos=(5, 1), flag=wx.ALL, border=border)

        # Add dropdown for speed
        speed_label = wx.StaticText(panel, label="Speed:")
        speed_text_input = wx.TextCtrl(panel, value="1.0")
        self.selected_speed = '1.0'
        speed_text_input.Bind(wx.EVT_TEXT, self.on_select_speed)
        sizer.Add(speed_label, pos=(6, 0), flag=wx.ALL, border=border)
        sizer.Add(speed_text_input, pos=(6, 1), flag=wx.ALL, border=border)

        # Add file dialog selector to select output folder
        output_folder_label = wx.StaticText(panel, label="Output Folder:")
        default_output = os.path.abspath(DEFAULT_OUTPUT_FOLDER)
        os.makedirs(default_output, exist_ok=True)
        self.output_folder_text_ctrl = wx.TextCtrl(panel, value=default_output)
        self.output_folder_text_ctrl.SetEditable(False)
        # Without a minimum the control asks to be as wide as the whole path, which is what
        # used to push the parameters column past the edge of the screen.
        self.output_folder_text_ctrl.SetMinSize((self.scaled(150), -1))
        self.output_folder_text_ctrl.SetToolTip(default_output)
        output_folder_button = wx.Button(panel, label="📂 Select")
        output_folder_button.Bind(wx.EVT_BUTTON, self.open_output_folder_dialog)
        sizer.Add(output_folder_label, pos=(7, 0), flag=wx.ALL, border=border)
        sizer.Add(self.output_folder_text_ctrl, pos=(7, 1), flag=wx.ALL | wx.EXPAND, border=border)
        sizer.Add(output_folder_button, pos=(8, 1), flag=wx.ALL, border=border)

        # Only valid once the cells exist: the controls take any spare width, the labels
        # keep theirs.
        sizer.AddGrowableCol(1)

        self.rebuild_languages()

    def create_synthesis_panel(self):
        panel_box = wx.Panel(self.right_panel, style=wx.SUNKEN_BORDER)
        panel_box_sizer = wx.StaticBoxSizer(wx.VERTICAL, panel_box, "Audiobook Generation Status")
        panel_box.SetSizer(panel_box_sizer)

        panel = self.synth_panel = wx.Panel(panel_box)
        panel_box_sizer.Add(panel, 1, wx.ALL | wx.EXPAND, border)
        self.right_sizer.Add(panel_box, 0, wx.ALL | wx.EXPAND, border)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        # Add Start button
        self.start_button = wx.Button(panel, label="🚀 Start Audiobook Synthesis")
        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)
        sizer.Add(self.start_button, 0, wx.ALL, 5)

        # Add Progress Bar label:
        self.progress_bar_label = wx.StaticText(panel, label="Synthesis Progress:")
        sizer.Add(self.progress_bar_label, 0, wx.ALL, 5)
        self.progress_bar = wx.Gauge(panel, range=100, style=wx.GA_PROGRESS)
        self.progress_bar.SetMinSize((-1, self.scaled(26)))
        sizer.Add(self.progress_bar, 0, wx.ALL | wx.EXPAND, 5)
        self.progress_bar_label.Hide()
        self.progress_bar.Hide()

        # Add ETA Label
        self.eta_label = wx.StaticText(panel, label="Estimated Time Remaining: ")
        self.eta_label.Hide()
        sizer.Add(self.eta_label, 0, wx.ALL, 5)

    def open_output_folder_dialog(self, event):
        with wx.DirDialog(self, "Choose a directory:", style=wx.DD_DEFAULT_STYLE) as dialog:
            if dialog.ShowModal() == wx.ID_CANCEL:
                return
            output_folder = dialog.GetPath()
            print(f"Selected output folder: {output_folder}")
            self.output_folder_text_ctrl.SetValue(output_folder)
            self.output_folder_text_ctrl.SetToolTip(output_folder)

    def on_select_voice(self, event):
        self.selected_voice = event.GetString()

    def on_select_backend(self, event):
        self.selected_backend = event.GetString()
        self.update_device_row()
        self.rebuild_languages()

    def update_device_row(self):
        """The torch device radio is meaningless when MLX will actually run."""
        uses_torch = resolve_backend(self.selected_backend) == 'torch' and torch_available()
        self.device_label.Enable(uses_torch)
        self.device_panel.Enable(uses_torch)

    def languages_for_backend(self, backend):
        """The language codes offered for a backend: Kokoro codes or Edge locales."""
        if resolve_backend(backend) == 'edge':
            return list(edge_voices.keys())
        return list(voices.keys())

    def language_label(self, backend, code):
        if resolve_backend(backend) == 'edge':
            return f'{edge_flags[code]} {code}'
        return f'{flags[code]} {code}'

    def rebuild_languages(self):
        """Repopulate the language listbox for the current backend, ticking the defaults."""
        codes = self.languages_for_backend(self.selected_backend)
        self.language_codes = codes
        self.language_listbox.SetItems([self.language_label(self.selected_backend, c) for c in codes])
        selected = default_languages(codes)
        for i, code in enumerate(codes):
            self.language_listbox.Check(i, code in selected)
        self.selected_languages = selected
        self.rebuild_voice_dropdown()

    def on_select_languages(self, event):
        self.selected_languages = {self.language_codes[i] for i in range(len(self.language_codes))
                                   if self.language_listbox.IsChecked(i)}
        self.rebuild_voice_dropdown()

    def rebuild_voice_dropdown(self):
        """Rebuild the voice dropdown from the checked languages of the current backend."""
        resolved = resolve_backend(self.selected_backend)
        choices = []
        if resolved == 'edge':
            for locale in self.language_codes:
                if locale not in self.selected_languages:
                    continue
                for v in edge_voices[locale]:
                    # Only the multilingual Edge voices are offered: a single voice
                    # that can narrate in many languages, which is what the dropdown
                    # is for. Non-multilingual voices can still be typed in by name.
                    if 'Multilingual' not in v:
                        continue
                    choices.append(f'{edge_flags[locale]} {v}')
        else:
            for code in self.language_codes:
                if code not in self.selected_languages:
                    continue
                for v in voices[code]:
                    choices.append(f'{flags[code]} {v}')
        current = self.get_selected_voice()
        self.voice_dropdown.SetItems(choices)
        # Keep the current voice if it is still offered.
        display = next((c for c in choices if c.split(' ', 1)[1] == current), '')
        if not display:
            # The dropdown is editable on purpose, and a typed voice -- a .pt path, a
            # blend, or an Edge voice outside the curated locales -- is in no list, so
            # ticking one more language must not silently swap it for choices[0] and send
            # a multi-hour run off with the wrong voice. Only a listed voice whose
            # language was just unticked falls back.
            display = current if not is_catalog_voice(current) else ''
            if not display:
                display = choices[0] if choices else ''
        self.selected_voice = display
        self.voice_dropdown.SetValue(display)

    def on_select_speed(self, event):
        speed = float(event.GetString())
        print('Selected speed', speed)
        self.selected_speed = speed

    def open_epub(self, file_path):
        # Cleanup previous layout
        if hasattr(self, 'selected_book'):
            self.splitter.DestroyChildren()

        self.selected_file_path = file_path
        print(f"Opening file: {file_path}")  # Do something with the filepath (e.g., parse the EPUB)

        from ebooklib import epub
        from epubcast.core import find_document_chapters_and_extract_texts, find_good_chapters, find_cover
        book = epub.read_epub(file_path)
        meta_title = book.get_metadata('DC', 'title')
        self.selected_book_title = meta_title[0][0] if meta_title else ''
        meta_creator = book.get_metadata('DC', 'creator')
        self.selected_book_author = meta_creator[0][0] if meta_creator else ''
        self.selected_book = book

        self.document_chapters = find_document_chapters_and_extract_texts(book)
        good_chapters = find_good_chapters(self.document_chapters)
        self.selected_chapter = good_chapters[0]
        for chapter in self.document_chapters:
            file_name = chapter.get_name().replace('.xhtml', '').replace('xhtml/', '').replace('.html', '').replace('Text/', '')
            # Chapters split out of a single-file book are named after their table-of-contents
            # entry, which reads far better than "index_split_000#anchor14".
            chapter.short_name = getattr(chapter, 'title', '') or file_name
            chapter.is_selected = chapter in good_chapters

        self.create_layout_for_ebook(self.splitter)

        # Update Cover
        cover = find_cover(book)
        if cover is not None:
            pil_image = Image.open(io.BytesIO(cover.content))
            wx_img = wx.EmptyImage(pil_image.size[0], pil_image.size[1])
            wx_img.SetData(pil_image.convert("RGB").tobytes())
            cover_h = self.scaled(170)
            cover_w = int(cover_h * pil_image.size[0] / pil_image.size[1])
            max_w = self.scaled(105)
            if cover_w > max_w:  # a landscape cover, which the old fixed max width squashed
                cover_h = int(cover_h * max_w / cover_w)
                cover_w = max_w
            wx_img.Rescale(cover_w, cover_h)
            self.cover_bitmap.SetBitmap(wx_img.ConvertToBitmap())
            self.cover_bitmap.SetMaxSize((cover_w, cover_h))

        chapters_panel = self.create_chapters_table_panel(good_chapters)

        #  chapters_panel to left_sizer, or replace if it exists already
        if self.chapters_panel:
            self.left_sizer.Replace(self.chapters_panel, chapters_panel)
            self.chapters_panel.Destroy()
            self.chapters_panel = chapters_panel
        else:
            self.left_sizer.Add(chapters_panel, 1, wx.ALL | wx.EXPAND, 5)
            self.chapters_panel = chapters_panel

        # These two are very important:
        self.splitter_left.Layout()
        self.splitter_right.Layout()
        self.splitter.Layout()
        self.fit_to_work_area()
        self.resize_table_columns()

    def on_table_checked(self, event):
        self.document_chapters[event.GetIndex()].is_selected = True

    def on_table_unchecked(self, event):
        self.document_chapters[event.GetIndex()].is_selected = False

    def on_table_selected(self, event):
        chapter = self.document_chapters[event.GetIndex()]
        print('Selected', event.GetIndex(), chapter.short_name)
        self.selected_chapter = chapter
        self.text_area.SetValue(chapter.extracted_text)
        self.set_chapter_label(chapter)

    def create_chapters_table_panel(self, good_chapters):
        panel = ScrolledPanel(self.splitter_left, -1, style=wx.TAB_TRAVERSAL | wx.SUNKEN_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        self.table = table = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        table.InsertColumn(0, "Included")
        table.InsertColumn(1, "Chapter Name")
        table.InsertColumn(2, "Chapter Length")
        table.InsertColumn(3, "Status")
        table.SetMinSize((self.scaled(240), self.scaled(160)))
        table.Bind(wx.EVT_SIZE, self.on_table_resized)
        table.EnableCheckBoxes()
        table.Bind(wx.EVT_LIST_ITEM_CHECKED, self.on_table_checked)
        table.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self.on_table_unchecked)
        table.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_table_selected)

        for i, chapter in enumerate(self.document_chapters):
            auto_selected = chapter in good_chapters
            table.Append(['', chapter.short_name, f"{len(chapter.extracted_text):,}"])
            if auto_selected: table.CheckItem(i)

        title_text = wx.StaticText(panel, label=f"Select chapters to include in the audiobook:")
        sizer.Add(title_text, 0, wx.ALL, 5)
        sizer.Add(table, 1, wx.ALL | wx.EXPAND, 5)
        return panel

    def on_table_resized(self, event):
        # Resize the table that sent the event, not self.table: opening a second book tears
        # the old one down while self.table already points at its replacement.
        self.resize_table_columns(event.GetEventObject())
        event.Skip()

    def resize_table_columns(self, table=None):
        """Share out the width the table has, instead of overflowing a narrow column."""
        table = table if table is not None else self.table
        width = table.GetClientSize().width
        if width <= 0:
            return
        included_w, length_w, status_w = self.scaled(70), self.scaled(80), self.scaled(90)
        name_w = max(self.scaled(90), width - included_w - length_w - status_w - border)
        for column, column_width in enumerate((included_w, name_w, length_w, status_w)):
            table.SetColumnWidth(column, column_width)

    def get_selected_voice(self):
        """Strip the flag emoji the dropdown prepends, tolerating typed-in custom voices."""
        voice = self.selected_voice.strip()
        first, _, rest = voice.partition(' ')
        if rest and (first in flags.values() or first in edge_flags.values()):
            return rest.strip()
        return voice

    def get_selected_speed(self):
        return float(self.selected_speed)

    def on_preview_chapter(self, event):
        lang_code = lang_code_for(self.get_selected_voice())
        button = event.GetEventObject()
        button.SetLabel("⏳")
        button.Disable()

        def restore_button():
            button.SetLabel("🔊 Preview")
            button.Enable()

        def generate_preview():
            import epubcast.core as core
            from epubcast.backends import get_pipeline
            try:
                # Same engine as the real run, or previews stop being representative.
                core.set_espeak_library()
                pipeline = get_pipeline(self.get_selected_voice(), lang_code=lang_code,
                                        backend=self.selected_backend)
                core.load_spacy()
                text = self.selected_chapter.extracted_text[:300]
                if len(text) == 0: return
                audio_segments = core.gen_audio_segments(
                    pipeline,
                    text,
                    voice=self.get_selected_voice(),
                    speed=self.get_selected_speed(),
                    lang_code=lang_code)
                final_audio = np.concatenate(audio_segments)
                tmp_preview_wav_file = NamedTemporaryFile(suffix='.wav', delete=False)
                soundfile.write(tmp_preview_wav_file, final_audio, core.sample_rate)
                cmd = ['ffplay', '-autoexit', '-nodisp', tmp_preview_wav_file.name]
                subprocess.run(cmd)
            except Exception as e:
                traceback.print_exc()
                wx.CallAfter(wx.MessageBox, f'Preview failed:\n\n{type(e).__name__}: {e}',
                             'Epubcast', wx.OK | wx.ICON_ERROR)
            finally:
                # wx widgets are only safe to touch from the main thread, and the button has
                # to come back even when the preview raised -- otherwise it stays on "⏳"
                # disabled, and previewing is dead for the rest of the session.
                wx.CallAfter(restore_button)

        if len(self.preview_threads) > 0:
            for thread in self.preview_threads:
                thread.join()
            self.preview_threads = []
        thread = threading.Thread(target=generate_preview)
        thread.start()
        self.preview_threads.append(thread)

    def on_start(self, event):
        self.synthesis_in_progress = True
        file_path = self.selected_file_path
        voice = self.get_selected_voice()
        speed = float(self.selected_speed)
        selected_chapters = [chapter for chapter in self.document_chapters if chapter.is_selected]
        self.start_button.Disable()
        self.params_panel.Disable()

        self.table.EnableCheckBoxes(False)
        for chapter_index, chapter in enumerate(self.document_chapters):
            if chapter in selected_chapters:
                self.set_table_chapter_status(chapter_index, "Planned")
                self.table.SetItem(chapter_index, 0, '✔️')

        backend = self.selected_backend
        print('Starting Audiobook Synthesis',
              dict(file_path=file_path, voice=voice, pick_manually=False, speed=speed, backend=backend))
        self.core_thread = CoreThread(params=dict(
            file_path=file_path, voice=voice, pick_manually=False, speed=speed,
            output_folder=self.output_folder_text_ctrl.GetValue(),
            selected_chapters=selected_chapters, backend=backend))
        self.core_thread.start()

    def on_open(self, event):
        with wx.FileDialog(self, "Open EPUB File", wildcard="*.epub", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_CANCEL:
                return
            file_path = dialog.GetPath()
            print(f"Selected file: {file_path}")
            if not file_path:
                print('No filepath?')
                return
            if self.synthesis_in_progress:
                wx.MessageBox("Audiobook synthesis is still in progress. Please wait for it to finish.", "Audiobook Synthesis in Progress")
            else:
                wx.CallAfter(self.open_epub, file_path)

    def on_exit(self, event):
        self.Close()

    def set_table_chapter_status(self, chapter_index, status):
        self.table.SetItem(chapter_index, 3, status)

    def open_folder_with_explorer(self, folder_path):
        try:
            if platform.system() == 'Windows':
                subprocess.Popen(['explorer', folder_path])
            elif platform.system() == 'Linux':
                subprocess.Popen(['xdg-open', folder_path])
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', folder_path])
        except Exception as e:
            print(e)


class CoreThread(threading.Thread):
    def __init__(self, params):
        super().__init__()
        self.params = params

    def run(self):
        import epubcast.core as core
        try:
            core.main(**self.params, post_event=self.post_event)
        except Exception as e:
            # A thread that dies takes its traceback to a terminal the GUI user never sees,
            # and leaves the window disabled mid-run. Hand the error back to the main thread.
            self.post_event('CORE_FAILED', message=f'{type(e).__name__}: {e}',
                            details=traceback.format_exc())

    def post_event(self, event_name, **kwargs):
        # eg. 'EVENT_CORE_PROGRESS' -> EventCoreProgress, EVENT_CORE_PROGRESS
        EventObject, EVENT_CODE = EVENTS[event_name]
        event_object = EventObject()
        for k, v in kwargs.items():
            setattr(event_object, k, v)
        wx.PostEvent(wx.GetApp().GetTopWindow(), event_object)


def main():
    print('Starting GUI...')
    app = wx.App(False)
    frame = MainWindow(None, "Epubcast - Generate Audiobooks from E-books")
    frame.Show(True)
    frame.Layout()
    app.SetTopWindow(frame)
    print('Done.')
    app.MainLoop()


if __name__ == '__main__':
    main()
