# Copyright (c) 2022-2025 Matthieu Imbert
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
from lib.config import config
from PySide2 import QtCore, QtGui, QtWidgets

class SbQFileSystemModel(QtWidgets.QFileSystemModel):

    def __init__(self, show_hidden_files, *args, **kwds):
        super().__init__(*args, **kwds)
        self.show_hidden_files = show_hidden_files

    def hasChildren(self, parent):
        if self.flags(parent) & QtCore.Qt.ItemNeverHasChildren:
            return False
        try:
            with os.scandir(self.filePath(parent)) as it:
                for entry in it:
                    if (not entry.name.startswith('.') or self.show_hidden_files) and entry.is_dir():
                        return True
        except PermissionError:
            return False
        return False

class SbQSortFilterProxyModel(QtCore.QSortFilterProxyModel):

    def lessThan(self, left, right):
        if left.column() not in [ 0, 2 ] or right.column() not in [ 0, 2 ]:
            return super().lessThan(left, right)
        info_left = self.sourceModel().fileInfo(left)
        info_right =  self.sourceModel().fileInfo(right)
        if info_left.isDir() and info_right.isFile():
            return True
        elif info_left.isFile() and info_right.isDir():
            return False
        return super().lessThan(left, right)

    def filterAcceptsRow(self, source_row, source_parent):
        first_col_index = self.sourceModel().index(source_row, 0, source_parent);
        file_info = self.sourceModel().fileInfo(first_col_index)
        if file_info.isDir():
            return super().filterAcceptsRow(source_row, source_parent)
        filename = self.sourceModel().fileName(first_col_index)
        remaining, sep, ext = filename.rpartition('.')
        if not config['filter_files']: return True
        if not sep: return False
        if ext.lower() in [ e.lower() for e in config['file_extensions_filter'] ]: return True
        return False

def set_pixmap(qlabel, qpixmap):
    w = qlabel.width()
    h = qlabel.height()
    qlabel.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
    qlabel.setPixmap(qpixmap.scaled(w, h, QtCore.Qt.KeepAspectRatio))

def set_dark_theme(app, dark):
    if dark:
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(53, 53, 53))
        palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor(25, 25, 25))
        palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(53, 53, 53))
        palette.setColor(QtGui.QPalette.ToolTipBase, QtCore.Qt.black)
        palette.setColor(QtGui.QPalette.ToolTipText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor(53, 53, 53))
        palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.BrightText, QtCore.Qt.red)
        palette.setColor(QtGui.QPalette.Link, QtGui.QColor(42, 130, 218))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(42, 130, 218))
        palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        app.setPalette(palette)
    else:
        app.setPalette(app.default_palette)
