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

import copy
from PySide6 import QtCore, QtGui, QtWidgets
from lib.config import config, STARTUP_PATH_MODE_SPECIFIED_PATH, STARTUP_PATH_MODE_LAST_PATH
from lib.ui_utils import set_dark_theme
from lib.sound_player import get_available_gst_audio_sink_factories, get_available_gst_factory_supported_properties
from lib.ui_lib import prefs_dial

DEFAULT_SINK_DISPLAY_NAME = '(default)'

class PrefsDialog(prefs_dial.Ui_PrefsDialog, QtWidgets.QDialog):

    update_audio_sink_properties = QtCore.Signal()

    def __init__(self, *args, **kwds):
        super(PrefsDialog, self).__init__(*args, **kwds)
        self.setupUi(self)
        self.populate()

    def populate(self):
        self.specified_path_button.clicked.connect(self.specified_path_button_clicked)
        self.setMinimumSize(self.size())
        self.setMaximumSize(self.size())
        self.update_audio_sink_properties.connect(self.fill_audio_sink_properties, QtCore.Qt.QueuedConnection)
        audio_sink_properties_del_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.audio_output_properties)
        audio_sink_properties_del_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        audio_sink_properties_del_shortcut.activated.connect(self.audio_sink_prop_del)
        audio_sink_properties_backspace_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self.audio_output_properties)
        audio_sink_properties_backspace_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        audio_sink_properties_backspace_shortcut.activated.connect(self.audio_sink_prop_del)

    def get_actual_selected_audio_sink_name(self):
        if self.audio_output.currentText() == DEFAULT_SINK_DISPLAY_NAME:
            return ''
        else:
            return self.audio_output.currentText()

    @QtCore.Slot()
    def specified_path_button_clicked(self, checked = False):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "startup path", self.specified_path.text())
        if path:
            self.specified_path.setText(path)

    @QtCore.Slot()
    def fill_audio_sink_properties(self):
        audiosink = self.get_actual_selected_audio_sink_name()
        if audiosink == '':
            available_properties = {}
        else:
            available_properties = get_available_gst_factory_supported_properties(audiosink)
        self.audio_output_properties.blockSignals(True)
        self.audio_output_properties.clear()
        self.audio_output_properties.setHorizontalHeaderLabels([ 'property', 'value' ])
        self.audio_output_properties.setRowCount(0)
        if audiosink in self.tmpconfig['gst_audio_sink_properties']:
            self.audio_output_properties.setRowCount(len(self.tmpconfig['gst_audio_sink_properties'][audiosink]))
            for i, config_prop in enumerate(list(self.tmpconfig['gst_audio_sink_properties'][audiosink])):
                if config_prop in available_properties:
                    del available_properties[config_prop]
                    kitem = QtWidgets.QTableWidgetItem(config_prop)
                    kitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                    vitem = QtWidgets.QTableWidgetItem(self.tmpconfig['gst_audio_sink_properties'][audiosink][config_prop])
                    vitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEditable)
                    self.audio_output_properties.setItem(i, 0, kitem)
                    self.audio_output_properties.setItem(i, 1, vitem)
                else:
                    del self.tmpconfig['gst_audio_sink_properties'][audiosink][config_prop]
        prop_selection_combo = QtWidgets.QComboBox(self.audio_output_properties)
        prop_selection_combo.addItems(sorted(available_properties.keys()))
        self.audio_output_properties.setRowCount(self.audio_output_properties.rowCount() + 1)
        self.audio_output_properties.setCellWidget(self.audio_output_properties.rowCount() - 1, 0, prop_selection_combo)
        prop_value_item = QtWidgets.QTableWidgetItem('')
        prop_value_item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsEditable)
        self.audio_output_properties.setItem(self.audio_output_properties.rowCount() - 1, 1, prop_value_item)
        self.audio_output_properties.blockSignals(False)
        self.audio_output_properties.itemChanged.connect(self.audio_sink_prop_value_changed)

    @QtCore.Slot()
    def audio_sink_prop_del(self):
        item = self.audio_output_properties.currentItem()
        if item:
            row = item.row()
            if row >= 0 and row < self.audio_output_properties.rowCount() - 1:
                propkey = self.audio_output_properties.item(row, 0).text()
                del self.tmpconfig['gst_audio_sink_properties'][self.get_actual_selected_audio_sink_name()][propkey]
                self.audio_output_properties.removeRow(row)
                self.update_audio_sink_properties.emit()

    @QtCore.Slot()
    def audio_sink_prop_value_changed(self, item):
        if self.get_actual_selected_audio_sink_name() not in self.tmpconfig['gst_audio_sink_properties']:
            self.tmpconfig['gst_audio_sink_properties'][self.get_actual_selected_audio_sink_name()] = {}
        if item.row() == self.audio_output_properties.rowCount() - 1:
            propkey = self.audio_output_properties.cellWidget(item.row(), 0).currentText()
        else:
            propkey = self.audio_output_properties.item(item.row(), 0).text()
        self.tmpconfig['gst_audio_sink_properties'][self.get_actual_selected_audio_sink_name()][propkey] = \
            self.audio_output_properties.item(item.row(), 1).text()
        self.update_audio_sink_properties.emit()

    def exec_prefs(self):
        self.tmpconfig = copy.deepcopy(config)
        self.check_autoplay_mouse.setChecked(self.tmpconfig['autoplay_mouse'])
        self.check_autoplay_keyboard.setChecked(self.tmpconfig['autoplay_keyboard'])
        self.check_dark_theme.setChecked(self.tmpconfig['dark_theme'])
        self.check_dark_theme.stateChanged.connect(self.check_dark_theme_state_changed)
        self.check_hide_tune.setChecked(self.tmpconfig['hide_tune'])
        self.check_reset_tune_between_sounds.setChecked(self.tmpconfig['reset_tune_between_sounds'])
        self.file_extensions_filter.setText(' '.join(self.tmpconfig['file_extensions_filter']))
        self.specified_path.setText(self.tmpconfig['specified_path'])
        if self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_SPECIFIED_PATH:
            self.startup_path_mode_specified_path.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_LAST_PATH:
            self.startup_path_mode_last_path.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_CURRENT_DIR:
            self.startup_path_mode_current_dir.setChecked(True)
        elif self.tmpconfig['startup_path_mode'] == STARTUP_PATH_MODE_HOME_DIR:
            self.startup_path_mode_home_dir.setChecked(True)
        self.audio_output.blockSignals(True)
        self.audio_output.clear()
        self.available_gst_audio_sink_factories = get_available_gst_audio_sink_factories()
        self.audio_output.addItems( [DEFAULT_SINK_DISPLAY_NAME] + [ fname for fname in self.available_gst_audio_sink_factories ])
        self.audio_output.blockSignals(False)
        self.audio_output.currentIndexChanged.connect(self.audio_output_index_changed)
        current_config_sink_index = self.audio_output.findText(DEFAULT_SINK_DISPLAY_NAME if self.tmpconfig['gst_audio_sink'] == '' else self.tmpconfig['gst_audio_sink'])
        if current_config_sink_index == -1:
            self.audio_output.setCurrentIndex(0)
        else:
            self.audio_output.setCurrentIndex(current_config_sink_index)
        self.fill_audio_sink_properties()
        if self.exec_():
            self.tmpconfig['autoplay_mouse'] = self.check_autoplay_mouse.isChecked()
            self.tmpconfig['autoplay_keyboard'] = self.check_autoplay_keyboard.isChecked()
            self.tmpconfig['hide_tune'] = self.check_hide_tune.isChecked()
            self.tmpconfig['reset_tune_between_sounds'] = self.check_reset_tune_between_sounds.isChecked()
            if self.startup_path_mode_specified_path.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_SPECIFIED_PATH
            elif self.startup_path_mode_last_path.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_LAST_PATH
            elif self.startup_path_mode_current_dir.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_CURRENT_DIR
            elif self.startup_path_mode_home_dir.isChecked():
                self.tmpconfig['startup_path_mode'] = STARTUP_PATH_MODE_HOME_DIR
            self.tmpconfig['specified_path'] = self.specified_path.text()
            self.tmpconfig['file_extensions_filter'] = self.file_extensions_filter.text().split(' ')
            self.tmpconfig['gst_audio_sink'] = self.get_actual_selected_audio_sink_name()
            config.update(self.tmpconfig)
            self.parentWidget().refresh_config()
            self.parentWidget().configure_audio_output()
        else:
            self.parentWidget().refresh_config()

    @QtCore.Slot()
    def check_dark_theme_state_changed(self):
        self.tmpconfig['dark_theme'] = self.check_dark_theme.isChecked()
        set_dark_theme(self.app, self.check_dark_theme.isChecked())

    @QtCore.Slot()
    def audio_output_index_changed(self):
        audiosink = self.get_actual_selected_audio_sink_name()
        for o in [ self.label_gst_aa_details, self.label_aa_long_name,
                   self.audio_output_long_name, self.label_aa_description,
                   self.audio_output_description, self.audio_output_description,
                   self.label_aa_plugin, self.audio_output_plugin,
                   self.label_aa_plugin_description, self.audio_output_plugin_description,
                   self.label_aa_plugin_package, self.audio_output_plugin_package,
                   self.label_aa_properties, self.audio_output_properties ]:
            o.setEnabled(audiosink != '')
        if audiosink == '':
            factory = None
            for o in [ self.audio_output_long_name, self.audio_output_description,
                       self.audio_output_plugin, self.audio_output_plugin_description,
                       self.audio_output_plugin_package ]:
                o.setText('')
            self.audio_output_properties.clear()
        else:
            factory = self.available_gst_audio_sink_factories[audiosink]
            self.audio_output_long_name.setText(factory.get_metadata('long-name'))
            self.audio_output_description.setText(factory.get_metadata('description'))
            self.audio_output_plugin.setText(factory.get_plugin().get_name())
            self.audio_output_plugin_description.setText(factory.get_plugin().get_description())
            self.audio_output_plugin_package.setText(factory.get_plugin().get_package())
            self.fill_audio_sink_properties()
