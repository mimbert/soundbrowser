import copy
from PySide2 import QtCore, QtGui, QtWidgets
from lib.config import config, STARTUP_PATH_MODE_SPECIFIED_PATH, STARTUP_PATH_MODE_LAST_PATH
from lib.ui_utils import set_dark_theme
from lib.sound import get_available_gst_audio_sink_factories, get_available_gst_factory_supported_properties
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
        audio_sink_properties_del_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.audio_output_properties)
        audio_sink_properties_del_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        audio_sink_properties_del_shortcut.activated.connect(self.audio_sink_prop_del)
        audio_sink_properties_backspace_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self.audio_output_properties)
        audio_sink_properties_backspace_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        audio_sink_properties_backspace_shortcut.activated.connect(self.audio_sink_prop_del)

    @QtCore.Slot()
    def specified_path_button_clicked(self, checked = False):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "startup path", self.specified_path.text())
        if path:
            self.specified_path.setText(path)

    @QtCore.Slot()
    def fill_audio_sink_properties(self):
        audiosink = self.audio_output.currentText()
        available_properties = get_available_gst_factory_supported_properties(audiosink)
        self.audio_output_properties.blockSignals(True)
        self.audio_output_properties.clear()
        self.audio_output_properties.setHorizontalHeaderLabels([ 'property', 'value' ])
        self.audio_output_properties.setRowCount(0)
        if audiosink in self.tmpconfig['gst_audio_sink_properties']:
            self.audio_output_properties.setRowCount(len(self.tmpconfig['gst_audio_sink_properties'][audiosink]))
            for i, config_prop in enumerate(self.tmpconfig['gst_audio_sink_properties'][audiosink]):
                del available_properties[config_prop]
                kitem = QtWidgets.QTableWidgetItem(config_prop)
                kitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                vitem = QtWidgets.QTableWidgetItem(self.tmpconfig['gst_audio_sink_properties'][audiosink][config_prop])
                vitem.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEditable)
                self.audio_output_properties.setItem(i, 0, kitem)
                self.audio_output_properties.setItem(i, 1, vitem)
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
                del self.tmpconfig['gst_audio_sink_properties'][self.audio_output.currentText()][propkey]
                self.audio_output_properties.removeRow(row)
                self.update_audio_sink_properties.emit()

    @QtCore.Slot()
    def audio_sink_prop_value_changed(self, item):
        if self.audio_output.currentText() not in self.tmpconfig['gst_audio_sink_properties']:
            self.tmpconfig['gst_audio_sink_properties'] \
                [self.audio_output.currentText()] = {}
        if item.row() == self.audio_output_properties.rowCount() - 1:
            propkey = self.audio_output_properties.cellWidget(item.row(), 0).currentText()
        else:
            propkey = self.audio_output_properties.item(item.row(), 0).text()
        self.tmpconfig['gst_audio_sink_properties'] \
            [self.audio_output.currentText()][propkey] \
            = self.audio_output_properties.item(item.row(), 1).text()
        self.update_audio_sink_properties.emit()

    def exec_prefs(self):
        self.tmpconfig = copy.deepcopy(config)
        self.check_autoplay_mouse.setChecked(self.tmpconfig['autoplay_mouse'])
        self.check_autoplay_keyboard.setChecked(self.tmpconfig['autoplay_keyboard'])
        self.check_dark_theme.setChecked(self.tmpconfig['dark_theme'])
        self.check_dark_theme.stateChanged.connect(self.check_dark_theme_state_changed)
        self.check_hide_reverse.setChecked(self.tmpconfig['hide_reverse'])
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
        self.audio_output.setCurrentIndex(self.audio_output.findText(self.tmpconfig['gst_audio_sink']))
        self.fill_audio_sink_properties()
        if self.exec_():
            self.tmpconfig['autoplay_mouse'] = self.check_autoplay_mouse.isChecked()
            self.tmpconfig['autoplay_keyboard'] = self.check_autoplay_keyboard.isChecked()
            self.tmpconfig['hide_reverse'] = self.check_hide_reverse.isChecked()
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
            self.tmpconfig['gst_audio_sink'] = self.audio_output.currentText()
            config.update(self.tmpconfig)
            self.parentWidget().refresh_config()
            self.configure_audio_output()
        else:
            self.parentWidget().refresh_config()

    @QtCore.Slot()
    def check_dark_theme_state_changed(self):
        self.tmpconfig['dark_theme'] = self.check_dark_theme.isChecked()
        set_dark_theme(self.app, self.check_dark_theme.isChecked())

    @QtCore.Slot()
    def audio_output_index_changed(self):
        audiosink = self.audio_output.currentText()
        for o in [ self.label_gst_aa_details, self.label_aa_long_name,
                   self.audio_output_long_name, self.label_aa_description,
                   self.audio_output_description, self.audio_output_description,
                   self.label_aa_plugin, self.audio_output_plugin,
                   self.label_aa_plugin_description, self.audio_output_plugin_description,
                   self.label_aa_plugin_package, self.audio_output_plugin_package,
                   self.label_aa_properties, self.audio_output_properties ]:
            o.setEnabled(audiosink not in [ DEFAULT_SINK_DISPLAY_NAME, '' ])
        if audiosink == '':
            audiosink == DEFAULT_SINK_DISPLAY_NAME
            #self.audio_output.itemText(DEFAULT_SINK_DISPLAY_NAME)
        if audiosink == DEFAULT_SINK_DISPLAY_NAME:
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
