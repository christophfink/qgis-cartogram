#!/usr/bin/python
# -*- coding: utf-8 -*-

from PyQt4.QtCore import pyqtSignal, QObject, QPyNullVariant
from qgis.core import QgsDistanceArea, QgsGeometry, QgsPoint, QgsVectorFileWriter

from cartogram_feature import CartogramFeature

import math
import traceback

import multiprocessing
import Queue


class CartogramWorker(QObject):
    """Background worker which actually creates the cartogram."""

    finished = pyqtSignal(object, int)
    error = pyqtSignal(Exception, basestring)
    progress = pyqtSignal(float)
    feedback = pyqtSignal(unicode)

    forces=[]

    def __init__(self, layer, field_name, iterations):
        """Constructor."""
        QObject.__init__(self)

        self.layer = layer
        self.field_name = field_name
        self.iterations = iterations

        self.intermediateLayers = []

        # used to store the computed minimum value when the input data contains
        # zero or null values in the column used to create the cartogram
        self.min_value = None

        # set default exit code - if this doesn't change everything went well
        self.exit_code = -1

    def run(self):
        ret = None

        try:
            feature_count = self.layer.featureCount()

            step = self.get_step()
            steps = 0

            for i in range(self.iterations):
                self.feedback.emit("starting iteration {} of {}".format(i+1,self.iterations))
                (meta_features,
                    force_reduction_factor) = self.get_reduction_factor(
                    self.layer, self.field_name)

                inQueue=multiprocessing.Queue()
                outQueue=multiprocessing.Queue()

                for feature in self.layer.getFeatures():
                    if self.exit_code > 0:
                        break

                    old_geometry = feature.geometry()
                    #new_geometry = self.transform(meta_features, force_reduction_factor, old_geometry)

                    inQueue.put((feature.id(),old_geometry.exportToWkt()))

                threads=[]
                for i in range(multiprocessing.cpu_count()+1):
                    p=multiprocessing.Process(target=self.transform,args=(meta_features,force_reduction_factor,inQueue,outQueue))
                    p.start()
                    threads.append(p)

                while True:
                    try:
                        (featureId,new_geometry)=outQueue.get(True,60)
                    except Queue.Empty:
                        break

                    new_geometry=QgsGeometry().fromWkt(new_geometry)

                    self.layer.dataProvider().changeGeometryValues({
                        featureId : new_geometry})

                    steps += 1
                    if step == 0 or steps % step == 0:
                        self.progress.emit(steps / float(feature_count) * 100)

#                intermediateLayer = QgsVectorLayer(
#                    "{geomType}?crs={crsId}".format(geomType=QGis.vectorGeometryType(self.layer.geometryType()),crsId=layer.crs().authid()),
#                    "intermediate layer #{}".format(step),
#                    "memory"
#                )
#                intermediateLayer.startEditing()
#                intermediateLayer.dataProvider().addAttributes(
#                    self.layer.dataprovider().fields().toList()
#                )
#                intermediateLayer.commitChanges()
#
#                for feature in self.layer.dataProvider().getFeatures():
#                    intermediateLayer.dataProvider().addFeatures([feature])
#                intermediateLayer.commitChanges()
#
#                self.intermediateLayers.append(intermediateLayer)

#                writer = QgsVectorFileWriter(
#                    "/tmp/intermediateLayer{}".format(step),
#                    "utf-8",
#                    self.layer.dataProvider().fields(),
#                    self.layer.wkbType(),
#                    self.layer.crs(),
#                    "GeoJSON",
#                    layerOptions=["COORDINATE_PRECISION=1"]
#                )
#                for f in self.layer.getFeatures():
#                    writer.addFeature(f)
#                del writer
        
        
            if self.exit_code == -1:
                self.progress.emit(100)
                ret = self.layer
        except Exception, e:
            self.error.emit(e, traceback.format_exc())

        self.finished.emit(ret, self.exit_code)

    def kill(self):
        self.exit_code = 1

    def get_reduction_factor(self, layer, field):
        """Calculate the reduction factor."""
        data_provider = layer.dataProvider()
        meta_features = []

        total_area = 0.0
        total_value = 0.0

        if self.min_value is None:
            self.min_value = self.get_min_value(data_provider, field)

        for feature in data_provider.getFeatures():
            meta_feature = CartogramFeature()

            geometry = QgsGeometry(feature.geometry())

            area = QgsDistanceArea().measure(geometry)
            total_area += area

            feature_value = feature.attribute(field)
            if type(feature_value) is QPyNullVariant or feature_value == 0:
                feature_value = self.min_value / 100

            total_value += feature_value

            meta_feature.area = area
            meta_feature.value = feature_value

            centroid = geometry.centroid()
            (cx, cy) = centroid.asPoint().x(), centroid.asPoint().y()
            meta_feature.center_x = cx
            meta_feature.center_y = cy

            meta_features.append(meta_feature)

        fraction = total_area / total_value

        total_size_error = 0

        for meta_feature in meta_features:
            polygon_value = meta_feature.value
            polygon_area = meta_feature.area

            if polygon_area < 0:
                polygon_area = 0

            # this is our 'desired' area...
            desired_area = polygon_value * fraction

            # calculate radius, a zero area is zero radius
            radius = math.sqrt(polygon_area / math.pi)
            meta_feature.radius = radius

            if desired_area / math.pi > 0:
                mass = math.sqrt(desired_area / math.pi) - radius
                meta_feature.mass = mass
            else:
                meta_feature.mass = 0

            size_error = max(polygon_area, desired_area) / \
                min(polygon_area, desired_area)

            total_size_error += size_error

        average_error = total_size_error / len(meta_features)
        force_reduction_factor = 1 / (average_error + 1)

        return (meta_features, force_reduction_factor)

    def transform(self, meta_features, force_reduction_factor, inQueue, outQueue):
        """Transform the geometry based on the force reduction factor."""

        while True:
            try:
                (featureId,geometry)=inQueue.get(False)
            except Queue.Empty:
                break

            geometry=QgsGeometry().fromWkt(geometry)

            if geometry.isMultipart():
                geometries = []
                for polygon in geometry.asMultiPolygon():
                    new_polygon = self.transform_polygon(polygon, meta_features,
                        force_reduction_factor)
                    geometries.append(new_polygon)
                returnValue = QgsGeometry.fromMultiPolygon(geometries)
            else:
                polygon = geometry.asPolygon()
                new_polygon = self.transform_polygon(polygon, meta_features,
                    force_reduction_factor)
                returnValue = QgsGeometry.fromPolygon(new_polygon)

            outQueue.put((featureId,returnValue.exportToWkt()))

    def transform_polygon(self, polygon, meta_features,
        force_reduction_factor):
        """Transform the geometry of a single polygon."""

        new_line = []
        new_polygon = []

        whitelist=[] # list of centroids we got actual correction factors from for the first point of the polygon. let’s assume if we get <0.0 change, it is not going to influence ANY of the polygons points

        for line in polygon:
            for point in line:
                x = x0 = point.x()
                y = y0 = point.y()

                if len(whitelist)==0:
                    featureList=meta_features
                else:
                    featureList=whitelist
                # compute the influence of all shapes on this point
                for feature in featureList:
                    if feature.mass == 0:
                        continue 
                    cx = feature.center_x
                    cy = feature.center_y
                    dX=x0-cx
                    dY=y0-cy
                    distance = math.sqrt(dX ** 2 + dY ** 2)

                    if (distance > feature.radius):
                        # calculate the force exerted on points far away from
                        # the centroid of this polygon
                        force = feature.mass * feature.radius / distance
                    else:
                        # calculate the force exerted on points close to the
                        # centroid of this polygon
                        xF = distance / feature.radius
                        # distance ** 2 / feature.radius ** 2 instead of xF
                        force = feature.mass * (xF ** 2) * (4 - (3 * xF))
                    force = force * force_reduction_factor / distance
                    corrX=dX*force
                    corrY=dY*force
                    if sqrt(corrX**2 + corrY**2) > 0.1: # HUOM! that is assuming we’re dealing with meters here! does NOT work with geographic crs!
                        x += corrX
                        y += corrY
                        whitelist.append(feature)
                new_line.append(QgsPoint(x, y))
            new_polygon.append(new_line)
            new_line = []

        return new_polygon

    def get_step(self):
        """Determine how often the progress bar should be updated."""

        feature_count = self.layer.featureCount()

        # update the progress bar at each .1% increment
        step = feature_count // 1000

        # because we use modulo to determine if we should emit the progress
        # signal, the step needs to be greater than 1
        if step < 2:
            step = 2

        return step

    def get_min_value(self, data_provider, field):
        features = []
        for feature in data_provider.getFeatures():
            feature_value = feature.attribute(field)
            if not type(feature_value) is QPyNullVariant \
                and feature_value != 0:
                features.append(feature.attribute(field))

        return min(features)
