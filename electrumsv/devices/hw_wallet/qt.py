#!/usr/bin/env python3
# -*- mode: python -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2016  The Electrum developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from functools import partial
from queue import Queue
import threading

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QLabel, QLineEdit, QHBoxLayout, QAction

from electrumsv.app_state import app_state
from electrumsv.exceptions import UserCancelled
from electrumsv.i18n import _

from electrumsv.gui.qt.password_dialog import PasswordDialog, PW_PASSPHRASE, PasswordLineEdit
from electrumsv.gui.qt.util import (
    WindowModalDialog, Buttons, OkButton, CancelButton, WWLabel, read_QIcon,
)


# The trickiest thing about this handler was getting windows properly
# parented on MacOSX.
class QtHandlerBase(QObject):
    '''An interface between the GUI (here, QT) and the device handling
    logic for handling I/O.'''

    passphrase_signal = pyqtSignal(object, object)
    message_signal = pyqtSignal(object, object)
    error_signal = pyqtSignal(object, object)
    warning_signal = pyqtSignal(object)
    word_signal = pyqtSignal(object)
    clear_signal = pyqtSignal()
    query_signal = pyqtSignal(object, object)
    yes_no_signal = pyqtSignal(object)
    status_signal = pyqtSignal(object)

    def __init__(self, win, device):
        super(QtHandlerBase, self).__init__()
        self.clear_signal.connect(self.clear_dialog)
        self.error_signal.connect(self.error_dialog)
        self.warning_signal.connect(self.warning_dialog)
        self.message_signal.connect(self.message_dialog)
        self.passphrase_signal.connect(self.passphrase_dialog)
        self.word_signal.connect(self.word_dialog)
        self.query_signal.connect(self.win_query_choice)
        self.yes_no_signal.connect(self.win_yes_no_question)
        self.status_signal.connect(self._update_status)
        self.win = win
        self.device = device
        self.dialog = None
        self.done = threading.Event()
        self.passphrase_queue = Queue()

    def top_level_window(self):
        return self.win.top_level_window()

    def update_status(self, paired):
        self.status_signal.emit(paired)

    def _update_status(self, paired):
        icon = self.icon_paired if paired else self.icon_unpaired
        self.action.setIcon(read_QIcon(icon))

    def query_choice(self, msg, labels):
        self.done.clear()
        self.query_signal.emit(msg, labels)
        self.done.wait()
        return self.choice

    def yes_no_question(self, msg):
        self.done.clear()
        self.yes_no_signal.emit(msg)
        self.done.wait()
        return self.ok

    def show_message(self, msg, on_cancel=None):
        self.message_signal.emit(msg, on_cancel)

    def show_error(self, msg, blocking=False):
        self.done.clear()
        self.error_signal.emit(msg, blocking)
        if blocking:
            self.done.wait()

    def show_warning(self, msg):
        self.done.clear()
        self.warning_signal.emit(msg)
        self.done.wait()

    def finished(self):
        self.clear_signal.emit()

    def get_word(self, msg):
        self.done.clear()
        self.word_signal.emit(msg)
        self.done.wait()
        return self.word

    def get_passphrase(self, msg, confirm):
        self.passphrase_signal.emit(msg, confirm)
        return self.passphrase_queue.get()

    def passphrase_dialog(self, msg, confirm):
        # If confirm is true, require the user to enter the passphrase twice
        parent = self.top_level_window()
        if confirm:
            d = PasswordDialog(parent, None, msg, PW_PASSPHRASE)
            confirmed, p, passphrase = d.run()
        else:
            d = WindowModalDialog(parent, _("Enter Passphrase"))
            pw = PasswordLineEdit()
            pw.setMinimumWidth(200)
            vbox = QVBoxLayout()
            vbox.addWidget(WWLabel(msg))
            vbox.addWidget(pw)
            vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
            d.setLayout(vbox)
            passphrase = pw.text() if d.exec_() else None
            pw.setText('')
        self.passphrase_queue.put(passphrase)

    def word_dialog(self, msg):
        dialog = WindowModalDialog(self.top_level_window(), "")
        hbox = QHBoxLayout(dialog)
        hbox.addWidget(QLabel(msg))
        text = QLineEdit()
        text.setMaximumWidth(100)
        text.returnPressed.connect(dialog.accept)
        hbox.addWidget(text)
        hbox.addStretch(1)
        dialog.exec_()  # Firmware cannot handle cancellation
        self.word = text.text()
        self.done.set()

    def message_dialog(self, msg, on_cancel):
        # Called more than once during signing, to confirm output and fee
        self.clear_dialog()
        title = _('Please check your {} device').format(self.device)
        self.dialog = dialog = WindowModalDialog(self.top_level_window(), title)
        l = QLabel(msg)
        vbox = QVBoxLayout(dialog)
        vbox.addWidget(l)
        if on_cancel:
            dialog.rejected.connect(on_cancel)
            vbox.addLayout(Buttons(CancelButton(dialog)))
        dialog.show()

    def error_dialog(self, msg):
        self.win.show_error(msg, parent=self.top_level_window())
        self.done.set()

    def warning_dialog(self, msg):
        self.win.show_warning(msg, parent=self.top_level_window())
        self.done.set()

    def clear_dialog(self):
        if self.dialog:
            self.dialog.accept()
            self.dialog = None

    def win_query_choice(self, msg, labels):
        self.choice = self.win.query_choice(msg, labels)
        self.done.set()

    def win_yes_no_question(self, msg):
        self.ok = self.win.question(msg)
        self.done.set()


class QtPluginBase(object):

    def create_handler(self, window):
        raise NotImplementedError

    def replace_gui_handler(self, window, keystore):
        handler = self.create_handler(window)
        keystore.handler = handler
        keystore.plugin = self

        action_label = keystore.label.strip() or _('Unnamed')
        action = QAction(read_QIcon(self.icon_unpaired), action_label, window)
        action.triggered.connect(partial(self.show_settings_wrapped, window, keystore))
        action.setToolTip(_("Hardware Wallet"))
        window.add_toolbar_action(action)
        handler.action = action
        handler.icon_unpaired = self.icon_unpaired
        handler.icon_paired = self.icon_paired

    def missing_message(self):
        if hasattr(self, 'libraries_available_message'):
            message = self.libraries_available_message + '\n'
        else:
            message = _("Cannot find python library for") + " '{}'.\n".format(self.name)
        message += _("Make sure you install it with python3")
        return message

    def choose_device(self, window, keystore):
        '''This dialog box should be usable even if the user has
        forgotten their PIN or it is in bootloader mode.'''
        device_id = app_state.device_manager.xpub_id(keystore.xpub)
        if not device_id:
            try:
                info = app_state.device_manager.select_device(self, keystore.handler, keystore)
            except UserCancelled:
                return
            device_id = info.device.id_
        return device_id

    def show_settings_wrapped(self, window, keystore):
        try:
            self.show_settings_dialog(window, keystore)
        except Exception as e:
            keystore.handler.show_error(str(e))
