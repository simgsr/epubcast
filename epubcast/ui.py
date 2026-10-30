#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# A simple wxWidgets UI for epubcast

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

from epubcast import DEFAULT_OUTPUT_FOLDER
from epubcast import preview
from epubcast.backends import edge_available, mlx_available, resolve_backend, torch_available
from epubcast.voices import (voices, flags, edge_voices, edge_flags, language_name,
                             lang_code_for, default_languages, is_catalog_voice)

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

# One spacing scale for the whole window. Before this, gaps of 5 and 8 were picked per
# control, so unrelated rows sat at slightly different distances and the eye read the
# misalignment as sloppiness rather than as grouping.
GAP = border
SECTION_GAP = border * 2

# The speed slider's range. Kokoro stays intelligible across this band and becomes uncanny
# outside it, so the control does not offer what it cannot do well.
SPEED_MIN = 0.5
SPEED_MAX = 2.0

# The pixel sizes in this file were picked on a 14" laptop; ui_scale stretches or shrinks
# them so the same layout fits a small screen and still uses the room on a large one.
REFERENCE_SCREEN_WIDTH = 1512


def muted_colour():
    """The system's own grey text, so helper lines stay legible in light and dark themes.

    A hardcoded (110, 110, 110) read fine on the light theme it was chosen on and sat at
    the edge of legibility the moment macOS switched to dark.
    """
    return wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)


def compact_number(value):
    """A short form of a large count, for places too narrow for the exact figure.

    The Book card sits in a side column where "1,384,632 characters" is wider than the space
    it has; the footer states the exact number for the run being started.
    """
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    if value >= 10_000:
        return f'{value / 1_000:.0f}k'
    return f'{value:,}'


def make_card(parent, title):
    """A titled section with exactly one border.

    The child panel is parented to the StaticBox itself. Hanging it off the surrounding
    panel instead is what makes wx warn that it "should be created as child of its
    wxStaticBox", and is why these sections rendered with a doubled frame inside the box.
    """
    sizer = wx.StaticBoxSizer(wx.VERTICAL, parent, title)
    panel = wx.Panel(sizer.GetStaticBox())
    sizer.Add(panel, 1, wx.EXPAND | wx.ALL, GAP)
    return sizer, panel


def helper_text(parent, text, width):
    """A wrapped, muted line of secondary explanation."""
    label = wx.StaticText(parent, label=text)
    label.SetForegroundColour(muted_colour())
    label.Wrap(width)
    return label


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
        self.preview_process = None
        self.preview_files = []
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

        # A single line of state along the bottom. The window is dense, and "what is selected
        # right now" -- voice, chapters, total length -- was previously spread over three
        # panels and never stated in one place.
        self.CreateStatusBar(1)
        self.set_status('Open an EPUB to begin.')

        self.create_menu()
        self.create_layout()
        self.centre_on_work_area()
        self.Show(True)

    def set_status(self, text):
        """Update the bottom status line."""
        if self.GetStatusBar() is not None:
            self.SetStatusText(text)

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
        self.set_table_chapter_status(event.chapter_index, "In progress…")

    def on_core_chapter_finished(self, event):
        self.set_table_chapter_status(event.chapter_index, "Done")
        self.start_button.Show()

    def on_core_progress(self, event):
        self.progress_bar.SetValue(event.stats.progress)
        self.progress_bar_label.SetLabel(f"Synthesis Progress: {event.stats.progress}%")
        self.eta_label.SetLabel(f"Estimated Time Remaining: {event.stats.eta}")
        self.synth_panel.Layout()

    def on_core_finished(self, event):
        self.synthesis_in_progress = False
        self.set_status('Finished — the .m4b is in the output folder.')
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
        self.set_status('Synthesis failed.')
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
        # Layout, top to bottom:
        #   top bar        [Open EPUB] [About] ................ backend in use
        #   content host   the welcome panel, until a book is opened, then:
        #       splitter
        #           splitter_left   chapters table
        #           splitter_right  text editor | settings column
        #                               settings column = scrolling cards + fixed footer
        #   status bar     what is selected right now
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.main_sizer)
        self.main_sizer.Add(self.create_top_bar(), 0, wx.EXPAND | wx.ALL, GAP)

        # The host swaps the welcome panel for the working layout in place, so opening a
        # book does not mean building a second window.
        self.content_host = wx.Panel(self)
        self.content_sizer = wx.BoxSizer(wx.VERTICAL)
        self.content_host.SetSizer(self.content_sizer)
        self.main_sizer.Add(self.content_host, 1, wx.EXPAND)

        self.splitter = wx.Panel(self.content_host)
        self.splitter_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.splitter.SetSizer(self.splitter_sizer)

        self.welcome_panel = self.create_welcome_panel(self.content_host)
        self.content_sizer.Add(self.welcome_panel, 1, wx.EXPAND)

    def create_top_bar(self):
        """The always-present strip of app-level actions.

        Emoji were doing duty as icons here, which reads as decoration rather than as
        affordance; the labels carry the meaning, so they stand on their own.
        """
        top_panel = wx.Panel(self)
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        top_panel.SetSizer(top_sizer)

        open_epub_button = wx.Button(top_panel, label="Open EPUB…")
        open_epub_button.Bind(wx.EVT_BUTTON, self.on_open)
        top_sizer.Add(open_epub_button, 0, wx.RIGHT, GAP)

        help_button = wx.Button(top_panel, label="About")
        help_button.Bind(wx.EVT_BUTTON, lambda event: self.about_dialog())
        top_sizer.Add(help_button, 0, 0)

        top_sizer.AddStretchSpacer()

        # Which engine will actually run, stated once instead of only inside a dropdown.
        self.engine_badge = wx.StaticText(top_panel, label='')
        self.engine_badge.SetForegroundColour(muted_colour())
        top_sizer.Add(self.engine_badge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, GAP)
        self.set_engine_badge('auto')
        return top_panel

    def set_engine_badge(self, backend):
        """Name the engine 'auto' resolves to, so the badge never says 'auto'."""
        resolved = resolve_backend(backend)
        detail = {'torch': 'PyTorch', 'mlx': 'MLX (Apple GPU)', 'edge': 'Microsoft Edge (online)'}
        self.engine_badge.SetLabel(f'Engine: {detail.get(resolved, resolved)}')
        self.engine_badge.GetParent().Layout()

    def create_welcome_panel(self, parent):
        """What the window says before a book is open.

        It used to say nothing: a blank grey frame with two buttons in the corner, which
        read as a broken app rather than as an invitation.
        """
        panel = wx.Panel(parent)
        outer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(outer)
        outer.AddStretchSpacer()

        title = wx.StaticText(panel, label='Turn an EPUB into a chaptered audiobook')
        title.SetFont(wx.Font(self.scaled(20), wx.DEFAULT, wx.NORMAL, wx.BOLD))
        outer.Add(title, 0, wx.ALIGN_CENTER_HORIZONTAL)
        outer.Add(helper_text(
            panel,
            'Open an EPUB, choose the chapters and a voice, then start. Audio is written '
            'to the output folder as one chaptered .m4b file.',
            self.scaled(520)), 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, GAP)

        open_button = wx.Button(panel, label='Open EPUB…', size=(self.scaled(180), self.scaled(36)))
        open_button.Bind(wx.EVT_BUTTON, self.on_open)
        open_button.SetDefault()
        outer.Add(open_button, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, SECTION_GAP)

        hint = helper_text(panel, 'You can also drop a file on the window, or press ⌘O.',
                           self.scaled(520))
        outer.Add(hint, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, GAP)
        outer.AddStretchSpacer()
        return panel

    def show_working_layout(self):
        """Swap the welcome panel for the chapter/editor/settings layout."""
        if self.welcome_panel is None:
            return
        self.welcome_panel.Hide()
        self.content_sizer.Detach(self.welcome_panel)
        self.welcome_panel.Destroy()
        self.welcome_panel = None
        self.content_sizer.Add(self.splitter, 1, wx.EXPAND)

    def create_layout_for_ebook(self, splitter):
        self.show_working_layout()
        splitter_left = wx.Panel(splitter, -1)
        splitter_right = wx.Panel(self.splitter)
        self.splitter_left, self.splitter_right = splitter_left, splitter_right
        # 3:5 to the working area, then 5:4 inside it. The settings cards are the density
        # limit here -- at a narrower column their controls overflowed the card edge -- while
        # the chapter names only lose a few characters.
        self.splitter_sizer.Add(splitter_left, 3, wx.EXPAND | wx.ALL, GAP)
        self.splitter_sizer.Add(splitter_right, 5, wx.EXPAND | wx.ALL, GAP)

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
        preview_button = wx.Button(self.center_panel, label="Preview chapter")
        preview_button.SetToolTip('Hear the text in this box read aloud with the selected voice')
        preview_button.Bind(wx.EVT_BUTTON, self.on_preview_chapter)

        self.center_sizer.Add(self.chapter_label, 0, wx.BOTTOM, GAP)
        self.center_sizer.Add(preview_button, 0, wx.BOTTOM, GAP)
        self.center_sizer.Add(self.text_area, 1, wx.EXPAND)

        splitter_right_sizer = wx.BoxSizer(wx.HORIZONTAL)
        splitter_right.SetSizer(splitter_right_sizer)

        self.create_right_panel(splitter_right)
        splitter_right_sizer.Add(self.center_panel, 5, wx.EXPAND | wx.RIGHT, GAP)
        splitter_right_sizer.Add(self.right_panel, 4, wx.EXPAND)

    def set_chapter_label(self, chapter):
        """Long section names wrap: as a single line they widen the whole centre column."""
        self.chapter_label.SetLabel(f'Section "{chapter.short_name}"')
        self.chapter_label.SetFont(wx.Font(self.scaled(13), wx.DEFAULT, wx.NORMAL, wx.BOLD))
        self.chapter_label.Wrap(max(self.scaled(260), self.center_panel.GetSize().width - 4 * border))
        self.center_panel.Layout()

    def about_dialog(self):
        msg = ("A simple tool to generate audiobooks from EPUB files using Kokoro-82M models\n" +
               "Distributed under the MIT License.\n\n" +
               "by Claudio Santini 2025\nand many contributors.\n\n" +
               "https://claudio.uk\n\n")
        wx.MessageBox(msg, "Epubcast")

    def create_right_panel(self, splitter_right):
        """The settings column: scrolling cards above, a footer that never scrolls below.

        The Start button used to live inside the scrolling area, so in any window shorter
        than the full layout -- which is most of them -- the primary action of the whole app
        sat below the fold, with nothing on screen to suggest scrolling would reveal it.
        """
        self.right_panel = wx.Panel(splitter_right)
        column = wx.BoxSizer(wx.VERTICAL)
        self.right_panel.SetSizer(column)

        self.right_cards = ScrolledPanel(self.right_panel, style=wx.TAB_TRAVERSAL)
        self.right_sizer = wx.BoxSizer(wx.VERTICAL)
        self.right_cards.SetSizer(self.right_sizer)
        column.Add(self.right_cards, 1, wx.EXPAND)
        # One widget to disable for the length of a run, as before: every settings card.
        self.params_panel = self.right_cards

        self.create_book_details_panel()
        self.create_engine_card()
        self.create_voice_card()
        self.create_output_card()
        self.right_cards.SetupScrolling(scroll_x=False, scroll_y=True, scrollToTop=False)

        self.create_synthesis_panel()
        column.Add(self.synth_panel_box, 0, wx.EXPAND | wx.TOP, GAP)

    def create_book_details_panel(self):
        card, panel = make_card(self.right_cards, "Book")
        self.right_sizer.Add(card, 0, wx.EXPAND | wx.ALL, GAP)
        self.book_info_panel = panel
        row = wx.BoxSizer(wx.HORIZONTAL)
        panel.SetSizer(row)

        self.cover_bitmap = wx.StaticBitmap(panel, -1)
        row.Add(self.cover_bitmap, 0, wx.RIGHT, GAP)

        details = wx.Panel(panel)
        grid = wx.GridBagSizer(GAP, GAP)
        details.SetSizer(grid)
        row.Add(details, 1, wx.EXPAND)

        def add_row(index, label, value):
            """A label and a value that ellipsizes.

            Laid out at its natural width a long title made this box the widest thing in the
            window, which stretched the whole right column and squeezed the chapter list.
            """
            label_text = wx.StaticText(details, label=label)
            label_text.SetForegroundColour(muted_colour())
            grid.Add(label_text, pos=(index, 0), flag=wx.ALIGN_TOP)
            value_text = wx.StaticText(details, label=value, style=wx.ST_ELLIPSIZE_END)
            value_text.SetMinSize((self.scaled(95), -1))
            value_text.SetToolTip(value)
            grid.Add(value_text, pos=(index, 1), flag=wx.EXPAND)

        total_len = sum(len(c.extracted_text) for c in getattr(self, 'document_chapters', []))
        add_row(0, "Title", self.selected_book_title)
        add_row(1, "Author", self.selected_book_author)
        add_row(2, "Length", f'{compact_number(total_len)} characters')
        grid.AddGrowableCol(1)

    def field_label(self, parent, text):
        """A small muted label above a control.

        Labels sit above their controls rather than beside them: in a column this narrow a
        label column stole ~70px from every row, which is what pushed the Preview button and
        the sample dropdown off the right edge of their cards.
        """
        label = wx.StaticText(parent, label=text)
        label.SetForegroundColour(muted_colour())
        return label

    def create_engine_card(self):
        card, panel = make_card(self.right_cards, "Engine")
        self.right_sizer.Add(card, 0, wx.EXPAND | wx.ALL, GAP)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        # Backend: which TTS engine runs. MLX is Apple Silicon only and much faster.
        self.selected_backend = 'auto'
        backend_choices = ['auto'] + (['mlx'] if mlx_available() else []) + \
                          (['torch'] if torch_available() else []) + \
                          (['edge'] if edge_available() else [])
        sizer.Add(self.field_label(panel, "Backend"), 0, wx.BOTTOM, 2)
        self.backend_dropdown = wx.ComboBox(panel, choices=backend_choices, value='auto',
                                            style=wx.CB_READONLY)
        self.backend_dropdown.Bind(wx.EVT_COMBOBOX, self.on_select_backend)
        sizer.Add(self.backend_dropdown, 0, wx.EXPAND)

        resolved = resolve_backend('auto')
        if torch_available():
            backend_note = '"auto" uses PyTorch.'
        elif mlx_available():
            backend_note = 'torch missing — "auto" uses MLX.'
        else:
            backend_note = f'"auto" will use {resolved}.'
        self.backend_note = helper_text(panel, backend_note, self.scaled(240))
        sizer.Add(self.backend_note, 0, wx.EXPAND | wx.TOP, 2)

        # Device only affects the torch backend; MLX always runs on the Apple GPU.
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
        radio_sizer = wx.BoxSizer(wx.HORIZONTAL)
        engine_radio_panel.SetSizer(radio_sizer)
        radio_sizer.Add(cpu_radio, 0, wx.RIGHT, GAP)
        radio_sizer.Add(cuda_radio, 0)
        self.device_label = self.field_label(panel, "Device")
        sizer.Add(self.device_label, 0, wx.TOP | wx.BOTTOM, GAP)
        sizer.Add(engine_radio_panel, 0, wx.EXPAND)

        self.update_device_row()

    def create_voice_card(self):
        card, panel = make_card(self.right_cards, "Voice")
        self.right_sizer.Add(card, 0, wx.EXPAND | wx.ALL, GAP)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        # Languages: multi-select, so the voice dropdown only shows the languages you care
        # about. The list depends on the backend (Kokoro codes vs Edge locales).
        sizer.Add(self.field_label(panel, "Languages"), 0, wx.BOTTOM, 2)
        self.language_listbox = wx.CheckListBox(panel, size=(-1, self.scaled(74)))
        self.language_listbox.Bind(wx.EVT_CHECKLISTBOX, self.on_select_languages)
        sizer.Add(self.language_listbox, 0, wx.EXPAND)

        sizer.Add(self.field_label(panel, "Voice"), 0, wx.TOP | wx.BOTTOM, GAP)
        self.selected_voice = ''
        # Editable: a voice can also be a blend ("af_heart,af_bella"), a path to a .pt
        # pack, or an Edge voice name typed in full.
        self.voice_dropdown = wx.ComboBox(panel, choices=[], value='')
        self.voice_dropdown.SetToolTip('Blend voices with commas, or type a path to a .pt pack')
        self.voice_dropdown.Bind(wx.EVT_COMBOBOX, self.on_select_voice)
        self.voice_dropdown.Bind(wx.EVT_TEXT, self.on_select_voice)
        sizer.Add(self.voice_dropdown, 0, wx.EXPAND)

        # Auditioning a voice next to the control that chooses it: picking by name alone
        # means committing a multi-hour run to a voice nobody has heard.
        self.preview_button = wx.Button(panel, label="Preview")
        self.preview_button.SetToolTip('Hear this voice read a short sample')
        self.preview_button.Bind(wx.EVT_BUTTON, self.on_preview_voice)
        self.sample_dropdown = wx.ComboBox(panel, choices=list(preview.SAMPLE_MODES),
                                           value=preview.MODE_AUTO, style=wx.CB_READONLY)
        self.sample_dropdown.SetToolTip('What the preview reads. Auto follows the selected voice.')
        preview_row = wx.BoxSizer(wx.HORIZONTAL)
        preview_row.Add(self.preview_button, 0, wx.RIGHT, GAP)
        preview_row.Add(self.sample_dropdown, 1, wx.EXPAND)
        sizer.Add(preview_row, 0, wx.EXPAND | wx.TOP, GAP)

        self.rebuild_languages()

    def create_output_card(self):
        card, panel = make_card(self.right_cards, "Output")
        self.right_sizer.Add(card, 0, wx.EXPAND | wx.ALL, GAP)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        # Speed is a slider, not a text box. Free text meant a stray keystroke raised
        # ValueError inside the change handler, and the app then refused to start a run for
        # reasons it never showed anywhere.
        sizer.Add(self.field_label(panel, "Speed"), 0, wx.BOTTOM, 2)
        self.speed_slider = wx.Slider(panel, value=100, minValue=int(SPEED_MIN * 100),
                                      maxValue=int(SPEED_MAX * 100),
                                      style=wx.SL_HORIZONTAL | wx.SL_SELRANGE)
        self.speed_slider.Bind(wx.EVT_SLIDER, self.on_select_speed)
        self.speed_value_label = wx.StaticText(panel, label='1.00×',
                                               size=(self.scaled(46), -1))
        speed_row = wx.BoxSizer(wx.HORIZONTAL)
        speed_row.Add(self.speed_slider, 1, wx.EXPAND | wx.RIGHT, GAP)
        speed_row.Add(self.speed_value_label, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(speed_row, 0, wx.EXPAND)

        # Add file dialog selector to select output folder
        sizer.Add(self.field_label(panel, "Folder"), 0, wx.TOP | wx.BOTTOM, GAP)
        default_output = os.path.abspath(DEFAULT_OUTPUT_FOLDER)
        os.makedirs(default_output, exist_ok=True)
        self.output_folder_text_ctrl = wx.TextCtrl(panel, value=default_output)
        self.output_folder_text_ctrl.SetEditable(False)
        # Without a minimum the control asks to be as wide as the whole path, which is what
        # used to push the parameters column past the edge of the screen.
        self.output_folder_text_ctrl.SetMinSize((self.scaled(120), -1))
        self.output_folder_text_ctrl.SetToolTip(default_output)
        output_folder_button = wx.Button(panel, label="Choose…")
        output_folder_button.Bind(wx.EVT_BUTTON, self.open_output_folder_dialog)
        folder_row = wx.BoxSizer(wx.HORIZONTAL)
        folder_row.Add(self.output_folder_text_ctrl, 1, wx.EXPAND | wx.RIGHT, GAP)
        folder_row.Add(output_folder_button, 0)
        sizer.Add(folder_row, 0, wx.EXPAND)

    def create_synthesis_panel(self):
        """The footer: the primary action, always on screen, plus run progress."""
        self.synth_panel_box = wx.Panel(self.right_panel)
        box_sizer = wx.BoxSizer(wx.VERTICAL)
        self.synth_panel_box.SetSizer(box_sizer)

        panel = self.synth_panel = wx.Panel(self.synth_panel_box)
        box_sizer.Add(panel, 1, wx.EXPAND)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        self.start_button = wx.Button(panel, label="Start synthesis",
                                      size=(-1, self.scaled(36)))
        self.start_button.SetFont(wx.Font(self.scaled(14), wx.DEFAULT, wx.NORMAL, wx.BOLD))
        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)
        sizer.Add(self.start_button, 0, wx.EXPAND | wx.BOTTOM, GAP)

        # How much is about to be synthesized, stated before the run rather than discovered
        # from a progress bar afterwards.
        self.selection_summary = wx.StaticText(panel, label='')
        self.selection_summary.SetForegroundColour(muted_colour())
        sizer.Add(self.selection_summary, 0, wx.BOTTOM, GAP)

        self.progress_bar_label = wx.StaticText(panel, label="Synthesis Progress:")
        self.progress_bar = wx.Gauge(panel, range=100, style=wx.GA_PROGRESS)
        self.progress_bar.SetMinSize((-1, self.scaled(22)))
        self.eta_label = wx.StaticText(panel, label="Estimated Time Remaining: ")
        sizer.Add(self.progress_bar_label, 0, wx.BOTTOM, 2)
        sizer.Add(self.progress_bar, 0, wx.EXPAND | wx.BOTTOM, GAP)
        sizer.Add(self.eta_label, 0)
        for hidden in (self.progress_bar_label, self.progress_bar, self.eta_label):
            hidden.Hide()
        self.update_selection_summary()

    def update_selection_summary(self):
        """Refresh the footer's one-line statement of what will be synthesized."""
        if not hasattr(self, 'selection_summary'):
            return
        chapters = getattr(self, 'document_chapters', None)
        if not chapters:
            self.selection_summary.SetLabel('')
            return
        chosen = [c for c in chapters if c.is_selected]
        characters = sum(len(c.extracted_text) for c in chosen)
        self.selection_summary.SetLabel(
            f'{len(chosen)} of {len(chapters)} chapters · {characters:,} characters')
        self.selection_summary.GetParent().Layout()

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
        self.set_engine_badge(self.selected_backend)

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
        """A language row that says what it is: flag, name, and the code the CLI uses."""
        if resolve_backend(backend) == 'edge':
            return f'{edge_flags[code]}  {language_name(code)}  ({code})'
        return f'{flags[code]}  {language_name(code)}  ({code})'

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
        self.selected_speed = self.speed_slider.GetValue() / 100
        self.speed_value_label.SetLabel(f'{self.selected_speed:.2f}×')
        self.set_status(f'Speed {self.selected_speed:.2f}× for the next run.')

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
        # Show the first section straight away. The editor used to come up empty next to a
        # label naming a section, which read as a rendering failure rather than as "nothing
        # is selected yet".
        self.text_area.SetValue(self.selected_chapter.extracted_text)

        # Update Cover
        cover = find_cover(book)
        if cover is not None:
            pil_image = Image.open(io.BytesIO(cover.content))
            wx_img = wx.Image(pil_image.size[0], pil_image.size[1])
            wx_img.SetData(pil_image.convert("RGB").tobytes())
            cover_h = self.scaled(104)
            cover_w = int(cover_h * pil_image.size[0] / pil_image.size[1])
            max_w = self.scaled(105)
            if cover_w > max_w:  # a landscape cover, which the old fixed max width squashed
                cover_h = int(cover_h * max_w / cover_w)
                cover_w = max_w
            wx_img.Rescale(cover_w, cover_h)
            self.cover_bitmap.SetBitmap(wx_img.ConvertToBitmap())
            self.cover_bitmap.SetMaxSize((cover_w, cover_h))

        chapters_panel = self.create_chapters_table_panel(good_chapters)
        # Always add: opening a second book rebuilds splitter_left and its sizer from
        # scratch, so the previous panel was already destroyed with the old children and
        # there is nothing left for Replace to find.
        self.left_sizer.Add(chapters_panel, 1, wx.EXPAND)
        self.chapters_panel = chapters_panel

        # These two are very important:
        self.splitter_left.Layout()
        self.splitter_right.Layout()
        self.splitter.Layout()
        self.fit_to_work_area()
        self.resize_table_columns()
        self.update_selection_summary()
        self.set_status(f'{self.selected_book_title or "Book"} · {len(self.document_chapters)} sections '
                        f'· {len(good_chapters)} selected')

    def on_table_checked(self, event):
        self.document_chapters[event.GetIndex()].is_selected = True
        self.update_selection_summary()

    def on_table_unchecked(self, event):
        self.document_chapters[event.GetIndex()].is_selected = False
        self.update_selection_summary()

    def on_table_selected(self, event):
        chapter = self.document_chapters[event.GetIndex()]
        self.selected_chapter = chapter
        self.text_area.SetValue(chapter.extracted_text)
        self.set_chapter_label(chapter)
        self.set_status(f'Editing "{chapter.short_name}" · {len(chapter.extracted_text):,} characters')

    def create_chapters_table_panel(self, good_chapters):
        panel = ScrolledPanel(self.splitter_left, -1, style=wx.TAB_TRAVERSAL)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)

        title_text = wx.StaticText(panel, label="Chapters")
        title_text.SetFont(wx.Font(self.scaled(13), wx.DEFAULT, wx.NORMAL, wx.BOLD))
        sizer.Add(title_text, 0, wx.BOTTOM, 2)
        sizer.Add(helper_text(panel, 'Untick anything that should not be narrated.',
                              self.scaled(260)), 0, wx.BOTTOM, GAP)

        self.table = table = wx.ListCtrl(panel, style=wx.LC_REPORT)
        table.InsertColumn(0, "Included")
        table.InsertColumn(1, "Chapter")
        table.InsertColumn(2, "Characters")
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

        sizer.Add(table, 1, wx.EXPAND)
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
        included_w, length_w, status_w = self.scaled(74), self.scaled(88), self.scaled(96)
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
        return self.speed_slider.GetValue() / 100

    def on_preview_chapter(self, event):
        """Read the text currently in the editor, so an edit can be heard before a run."""
        text = preview.clip_text(self.text_area.GetValue(), limit=600)
        if not text:
            wx.MessageBox('This section has no text to read.', 'Epubcast',
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        self.start_preview([('Chapter', text)], event.GetEventObject(), 'the chapter')

    def on_preview_voice(self, event):
        """Audition the selected voice with a short sample, next to the voice selector."""
        voice = self.get_selected_voice()
        if not voice:
            wx.MessageBox('Choose a voice first.', 'Epubcast',
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        segments = preview.sample_segments(voice, lang_code_for(voice), self.get_sample_mode(),
                                           self.get_selected_chapter_text())
        self.start_preview(segments, self.preview_button, f'a {voice} sample')

    def get_sample_mode(self):
        return self.sample_dropdown.GetValue() or preview.MODE_AUTO

    def get_selected_chapter_text(self):
        chapter = getattr(self, 'selected_chapter', None)
        return getattr(chapter, 'extracted_text', '') if chapter is not None else ''

    def start_preview(self, segments, button, description):
        """Synthesize `segments` off the UI thread, then play them.

        The preview threads are deliberately not joined from here. The previous version
        joined every outstanding preview thread on the UI thread before starting the next,
        so a second click froze the whole window until the first preview had finished
        synthesizing -- which for a 300-character sample is several seconds.
        """
        if self.synthesis_in_progress:
            self.set_status('Wait for the current synthesis to finish before previewing.')
            return
        voice = self.get_selected_voice()
        lang_code = lang_code_for(voice)
        backend = self.selected_backend
        speed = self.get_selected_speed()
        label = button.GetLabel()
        button.SetLabel('Working…')
        button.Disable()
        self.set_status(f'Preparing {description}…')

        def work():
            try:
                audio = preview.render_preview(voice, lang_code, backend, speed, segments)
                path = preview.write_preview_wav(audio)
            except Exception as error:
                traceback.print_exc()
                wx.CallAfter(self.preview_failed, error)
                return
            finally:
                # wx widgets are only safe to touch from the main thread, and the button has
                # to come back even when the preview raised -- otherwise it stays disabled
                # and previewing is dead for the rest of the session.
                wx.CallAfter(self.restore_preview_button, button, label)
            wx.CallAfter(self.play_preview, path, description)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.preview_threads = [t for t in self.preview_threads if t.is_alive()] + [thread]

    def restore_preview_button(self, button, label):
        button.SetLabel(label)
        button.Enable()

    def preview_failed(self, error):
        self.set_status('Preview failed.')
        wx.MessageBox(f'Preview failed:\n\n{type(error).__name__}: {error}',
                      'Epubcast', wx.OK | wx.ICON_ERROR, self)

    def stop_preview_audio(self):
        """Stop whatever preview is playing, so overlapping previews do not talk over each other."""
        process, self.preview_process = self.preview_process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def play_preview(self, path, description):
        self.stop_preview_audio()
        for stale in self.preview_files:
            try:
                os.unlink(stale)
            except OSError:
                pass
        self.preview_files = [path]
        process = preview.play(path)
        if process is None:
            self.set_status('No audio player available for previews.')
            wx.MessageBox('Could not find anything to play the preview with.\n\n'
                          'Install ffmpeg (which provides ffplay) to hear previews.',
                          'Epubcast', wx.OK | wx.ICON_WARNING, self)
            return
        self.preview_process = process
        self.set_status(f'Playing {description}…')

    def on_start(self, event):
        if not any(chapter.is_selected for chapter in self.document_chapters):
            # Previously this started a run that produced nothing, after a long model load.
            wx.MessageBox('Tick at least one chapter to include in the audiobook.', 'Epubcast',
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        self.synthesis_in_progress = True
        file_path = self.selected_file_path
        voice = self.get_selected_voice()
        speed = self.get_selected_speed()
        selected_chapters = [chapter for chapter in self.document_chapters if chapter.is_selected]
        self.start_button.Disable()
        self.params_panel.Disable()
        self.stop_preview_audio()
        self.set_status(f'Synthesizing {len(selected_chapters)} chapters with {voice}…')

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
