from PyQt4.QtCore import (Qt, QCoreApplication, QPyNullVariant, QSettings,
    QThread, QTranslator, qVersion)
from PyQt4.QtGui import (QAction, QPushButton, QDialog, QIcon, QLabel,
    QMessageBox, QProgressBar)
from qgis.core import (QGis, QgsDistanceArea, QgsGeometry, QgsMapLayer,
    QgsMapLayerRegistry, QgsMessageLog, QgsPoint, QgsVectorFileWriter,
    QgsVectorLayer, QgsProject)
from qgis.gui import QgsFieldProxyModel, QgsMapLayerProxyModel, QgsMessageBar

from cartogram_dialog import CartogramDialog
from cartogram_worker import CartogramWorker

import math
import os.path
import resources_rc


class Cartogram:
    """QGIS Plugin Implementation."""

    workers=[]

    def __init__(self, iface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """

        # save reference to the QGIS interface
        self.iface = iface

        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)

        # initialize locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            '{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)

            if qVersion() > '4.3.3':
                QCoreApplication.installTranslator(self.translator)

        # declare instance attributes
        self.action = None
        self.menu = self.tr('&Cartogram')

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""

        # create action to display the settings dialog
        self.run_action = QAction(
            QIcon(':/plugins/cartogram/assets/icon.png'),
            self.tr('Create cartogram...'),
            self.iface.mainWindow())

        self.demo_action = QAction(
            self.tr('Add demo layer'),
            self.iface.mainWindow())

        # connect the actions to their respective methods
        self.run_action.triggered.connect(self.run)
        self.demo_action.triggered.connect(self.demo)

        # add toolbar button and menu items
        self.iface.addToolBarIcon(self.run_action)
        self.iface.addPluginToVectorMenu(self.menu, self.run_action)
        self.iface.addPluginToVectorMenu(self.menu, self.demo_action)

    def unload(self):
        """Removes the plugin menu item and icon from the QGIS GUI."""
        self.iface.removePluginVectorMenu('&Cartogram', self.run_action)
        self.iface.removePluginVectorMenu('&Cartogram', self.demo_action)
        self.iface.removeToolBarIcon(self.run_action)

    def run(self):
        """Makes a few sanity checks and prepares the worker thread."""

        # create the dialog (after translation) and keep reference
        self.dialog = CartogramDialog()

        # make sure we have at least one vector layer to work on
        count = self.count_vector_layers()
        if count == 0:
            message = self.tr('You need at least one vector layer to create a '
                'cartogram.')
            self.iface.messageBar().pushMessage('Error', message,
                level=QgsMessageBar.CRITICAL, duration=5)
            return False

        # we are only interested in polygon layers and numeric fields
        self.dialog.sourceLayerCombo.setFilters(
            QgsMapLayerProxyModel.PolygonLayer)
        self.dialog.sourceFieldCombo.setFilters(QgsFieldProxyModel.Numeric)

        # select the first layer in the list and notify the field combobox
        self.dialog.sourceLayerCombo.setCurrentIndex(0)
        currentLayer = self.dialog.sourceLayerCombo.currentLayer()
        self.dialog.sourceLayerCombo.layerChanged.emit(currentLayer)

        # connect some odds and ends
        self.dialog.buttonBox.accepted.connect(self.validate)

        # show the dialog
        self.dialog.show()
        result = self.dialog.exec_()
        if result == QDialog.Rejected:
            return False

        input_layer_name = self.dialog.sourceLayerCombo.currentText()
        input_layer = self.get_vector_layer_by_name(input_layer_name)
        input_field = self.dialog.sourceFieldCombo.currentText()
        iterations = self.dialog.iterationsSpinBox.value()

        anim_fields = [f.name() for f in input_layer.fields().toList() if f.name()[:4]=="anim"]
        if len(anim_fields)>1 and input_field[:4]=="anim":
            for anim_field in anim_fields:
                memory_layer = self.create_memory_layer(input_layer,anim_field)
                self.workers.append({
                    "layer":memory_layer,
                    "fieldName":anim_field,
                    "iterations":iterations
                })
            firstWorker=self.workers.pop()
            self.worker_start(
                firstWorker["layer"],
                firstWorker["fieldName"],
                firstWorker["iterations"]
            )
            
        else:
            memory_layer = self.create_memory_layer(input_layer,input_field)
            self.worker_start(memory_layer, input_field, iterations)


    def demo(self):
        path = os.path.join(self.plugin_dir, 'demo', 'demo.shp')

        layer = QgsVectorLayer(path, 'Cartogram demo layer', 'ogr')
        QgsMapLayerRegistry.instance().addMapLayer(layer)

    def worker_start(self, layer, field_name, iterations):
        """Start a worker instance on a background thread."""

        worker = CartogramWorker(layer, field_name, iterations)

        message_bar = self.iface.messageBar().createMessage('')

        label = QLabel('Creating cartogram...')
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        progress_bar = QProgressBar()
        progress_bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        progress_bar.setMaximum(iterations * layer.featureCount())

        cancel_button = QPushButton()
        cancel_button.setText(self.tr('Cancel'))
        cancel_button.clicked.connect(worker.kill)

        message_bar.layout().addWidget(label)
        message_bar.layout().addWidget(progress_bar)
        message_bar.layout().addWidget(cancel_button)

        self.iface.messageBar().pushWidget(message_bar,
            self.iface.messageBar().INFO)
        self.message_bar = message_bar

        # start the worker in a new thread
        thread = QThread()
        worker.moveToThread(thread)

        # connect some odds and ends
        worker.finished.connect(self.worker_finished)
        worker.error.connect(self.worker_error)
        worker.progress.connect(progress_bar.setValue)
        worker.feedback.connect(self.worker_feedback)
        thread.started.connect(worker.run)

        thread.start()

        self.thread = thread
        self.worker = worker

    def worker_finished(self, layer, exit_code):
        """Clean up after the worker and the thread."""

        self.worker.deleteLater()
        self.thread.quit()
        self.thread.wait()
        self.thread.terminate()
        self.thread.deleteLater()

        self.iface.messageBar().popWidget(self.message_bar)

        #for intermediateLayer in intermediateLayers:
        #    QgsMapLayerRegistry.instance().addMapLayer(intermediateLayers)

        if layer is not None:
            QgsMapLayerRegistry.instance().addMapLayer(layer)
            QgsVectorFileWriter.writeAsVectorFormat(
                layer,
                os.path.join(QgsProject.instance().homePath(),layer.name()+".shp"),
                "utf-8", 
                None, 
                "ESRI Shapefile"
            )
        else:
            if (exit_code == 1):
                message = self.tr('Cartogram creation cancelled by user.')
                self.iface.messageBar().pushMessage(message,
                    level=QgsMessageBar.INFO, duration=3)

        if len(self.workers) > 0:
            worker=self.workers.pop()
            self.worker_start(
                worker["layer"],
                worker["fieldName"],
                worker["iterations"]
            )

    def worker_error(self, e, exception_string):
        message = self.tr('An error ocurred during cartogram creation. '
            'Please see the "Plugins" log for details.')
        self.iface.messageBar().pushMessage('Error', message,
            level=QgsMessageBar.CRITICAL, duration=5)

        log = 'Worker thread exception: {}'.format(exception_string)
        QgsMessageLog.logMessage(log, level=QgsMessageLog.CRITICAL,
            tag='Plugins')

    def worker_feedback(self,msg):
        QgsMessageLog.logMessage(msg)

    def validate(self):
        """Make sure that all fields have valid values."""
        message = ''
        if not self.dialog.sourceLayerCombo.currentText():
            message += self.tr('Please select an input layer.')
        if not self.dialog.sourceFieldCombo.currentText():
            message += self.tr('Please select an area field.')

        if message:
            QMessageBox.warning(self.dialog, 'Cartogram', message)
        else:
            # get the select layer and field
            layer_name = self.dialog.sourceLayerCombo.currentText()
            layer = self.get_vector_layer_by_name(layer_name)
            field = self.dialog.sourceFieldCombo.currentText()

            # loop through the input data to make sure no rows contain zero or
            # null values
            zero_null = None
            for feature in layer.getFeatures():
                feature_value = feature.attribute(field)
                if type(feature_value) is QPyNullVariant or feature_value == 0:
                    zero_null = 1

            # ask the user if she wants to continue if one or more zero or null
            # rows are found in the input data
            if zero_null is not None:
                message = self.tr('One or more rows in your "area" column '
                    'contain zero or NULL values. Do you want to continue '
                    'anyway with modified (non-zero) values for those fields?')
                reply = QMessageBox.question(self.dialog, 'Cartogram',
                    message, QMessageBox.Cancel, QMessageBox.Ok)

                if reply == QMessageBox.Cancel:
                    self.dialog.reject()
                    return False

            self.dialog.accept()

    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API."""
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('Cartogram', message)

    def get_vector_layer_by_name(self, layer_name):
        """Retrieve a layer from the registry by name."""
        layerMap = QgsMapLayerRegistry.instance().mapLayers()
        for name, layer in layerMap.iteritems():
            if layer.type() == QgsMapLayer.VectorLayer \
                    and layer.name() == layer_name:
                if layer.isValid():
                    return layer
                else:
                    return None

    def count_vector_layers(self):
        """Count the number of vector layers on the canvas."""
        layermap = QgsMapLayerRegistry.instance().mapLayers()

        count = 0
        for name, layer in layermap.iteritems():
            if layer.type() == QgsMapLayer.VectorLayer:
                if layer.geometryType() == QGis.Polygon:
                    count += 1

        return count

    def create_memory_layer(self, layer, inputField):
        """Create an in-memory copy of an existing vector layer."""

        data_provider = layer.dataProvider()

        # create the layer path defining geometry type and reference system
        geometry_type = QGis.vectorGeometryType(layer.geometryType())
        crs_id = layer.crs().authid()
        path = geometry_type + '?crs=' + crs_id + '&index=yes'

        # create the memory layer and get a reference to the data provider
        memory_layer = QgsVectorLayer(path, 'cartogram_{}'.format(inputField), 'memory')
        memory_layer_data_provider = memory_layer.dataProvider()

        # copy all attributes from the source layer to the memory layer
        memory_layer.startEditing()
        memory_layer_data_provider.addAttributes(
            layer.fields().toList())
        memory_layer.commitChanges()

        # copy all features from the source layer to the memory layer
        for feature in layer.getFeatures():
            memory_layer_data_provider.addFeatures([feature])

        return memory_layer
