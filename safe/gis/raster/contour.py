# coding=utf-8
"""Create contour from shakemap raster layer."""

import os
import shutil
import numpy as np
from osgeo import gdal, ogr
from osgeo.gdalconst import GA_ReadOnly
import logging

from safe.common.exceptions import (
    GridXmlFileNotFoundError,
    GridXmlParseError,
    ContourCreationError,
    InvalidLayerError,
    CallGDALError)

from safe.definitions.constants import (
    NONE_SMOOTHING, NUMPY_SMOOTHING, SCIPY_SMOOTHING
)
from safe.utilities.resources import resources_path

__copyright__ = "Copyright 2017, The InaSAFE Project"
__license__ = "GPL version 3"
__email__ = "info@inasafe.org"
__revision__ = '$Format:%H$'

LOGGER = logging.getLogger('InaSAFE')


def gaussian_kernel(sigma, truncate=4.0):
    """Return Gaussian that truncates at the given number of std deviations.

    Adapted from https://github.com/nicjhan/gaussian-filter
    """

    sigma = float(sigma)
    radius = int(truncate * sigma + 0.5)

    x, y = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    sigma = sigma ** 2

    k = 2 * np.exp(-0.5 * (x ** 2 + y ** 2) / sigma)
    k = k / np.sum(k)

    return k


def tile_and_reflect(input):
    """Make 3x3 tiled array.

    Central area is 'input', surrounding areas are reflected.

    Adapted from https://github.com/nicjhan/gaussian-filter
    """

    tiled_input = np.tile(input, (3, 3))

    rows = input.shape[0]
    cols = input.shape[1]

    # Now we have a 3x3 tiles - do the reflections.
    # All those on the sides need to be flipped left-to-right.
    for i in range(3):
        # Left hand side tiles
        tiled_input[i * rows:(i + 1) * rows, 0:cols] = \
            np.fliplr(tiled_input[i * rows:(i + 1) * rows, 0:cols])
        # Right hand side tiles
        tiled_input[i * rows:(i + 1) * rows, -cols:] = \
            np.fliplr(tiled_input[i * rows:(i + 1) * rows, -cols:])

    # All those on the top and bottom need to be flipped up-to-down
    for i in range(3):
        # Top row
        tiled_input[0:rows, i * cols:(i + 1) * cols] = \
            np.flipud(tiled_input[0:rows, i * cols:(i + 1) * cols])
        # Bottom row
        tiled_input[-rows:, i * cols:(i + 1) * cols] = \
            np.flipud(tiled_input[-rows:, i * cols:(i + 1) * cols])

    # The central array should be unchanged.
    assert (np.array_equal(input, tiled_input[rows:2 * rows, cols:2 * cols]))

    # All sides of the middle array should be the same as those bordering them.
    # Check this starting at the top and going around clockwise. This can be
    # visually checked by plotting the 'tiled_input' array.
    assert (np.array_equal(input[0, :], tiled_input[rows - 1, cols:2 * cols]))
    assert (np.array_equal(input[:, -1], tiled_input[rows:2 * rows, 2 * cols]))
    assert (np.array_equal(input[-1, :], tiled_input[2 * rows, cols:2 * cols]))
    assert (np.array_equal(input[:, 0], tiled_input[rows:2 * rows, cols - 1]))

    return tiled_input


def convolve(input, weights, mask=None, slow=False):
    """2 dimensional convolution.

    This is a Python implementation of what will be written in Fortran.

    Borders are handled with reflection.

    Masking is supported in the following way:
        * Masked points are skipped.
        * Parts of the input which are masked have weight 0 in the kernel.
        * Since the kernel as a whole needs to have value 1, the weights of the
          masked parts of the kernel are evenly distributed over the non-masked
          parts.

    Adapted from https://github.com/nicjhan/gaussian-filter
    """

    assert (len(input.shape) == 2)
    assert (len(weights.shape) == 2)

    # Only one reflection is done on each side so the weights array cannot be
    # bigger than width/height of input +1.
    assert (weights.shape[0] < input.shape[0] + 1)
    assert (weights.shape[1] < input.shape[1] + 1)

    if mask is not None:
        # The slow convolve does not support masking.
        assert (not slow)
        assert (input.shape == mask.shape)
        tiled_mask = tile_and_reflect(mask)

    output = np.copy(input)
    tiled_input = tile_and_reflect(input)

    rows = input.shape[0]
    cols = input.shape[1]
    # Stands for half weights row.
    hw_row = weights.shape[0] / 2
    hw_col = weights.shape[1] / 2

    # Now do convolution on central array.
    # Iterate over tiled_input.
    for i, io in zip(range(rows, rows * 2), range(rows)):
        for j, jo in zip(range(cols, cols * 2), range(cols)):
            # The current central pixel is at (i, j)

            # Skip masked points.
            if mask is not None and tiled_mask[i, j]:
                continue

            average = 0.0
            if slow:
                # Iterate over weights/kernel.
                for k in range(weights.shape[0]):
                    for l in range(weights.shape[1]):
                        # Get coordinates of tiled_input array that match given
                        # weights
                        m = i + k - hw_row
                        n = j + l - hw_col

                        average += tiled_input[m, n] * weights[k, l]
            else:
                # Find the part of the tiled_input array that overlaps with the
                # weights array.
                overlapping = tiled_input[
                    i - hw_row:i + hw_row + 1,
                    j - hw_col:j + hw_col + 1]
                assert (overlapping.shape == weights.shape)

                # If any of 'overlapping' is masked then set the corrosponding
                # points in the weights matrix to 0 and redistribute these to
                # non-masked points.
                if mask is not None:
                    overlapping_mask = tiled_mask[
                        i - hw_row:i + hw_row + 1,
                        j - hw_col:j + hw_col + 1]
                    assert (overlapping_mask.shape == weights.shape)

                    # Total value and number of weights clobbered by the mask.
                    clobber_total = np.sum(weights[overlapping_mask])
                    remaining_num = np.sum(np.logical_not(overlapping_mask))
                    # This is impossible since at least i, j is not masked.
                    assert (remaining_num > 0)
                    correction = clobber_total / remaining_num

                    # It is OK if nothing is masked - the weights will not be
                    #  changed.
                    if correction == 0:
                        assert (not overlapping_mask.any())

                    # Redistribute to non-masked points.
                    tmp_weights = np.copy(weights)
                    tmp_weights[overlapping_mask] = 0.0
                    tmp_weights[np.where(tmp_weights != 0)] += correction

                    # Should be very close to 1. May not be exact due to
                    # rounding.
                    assert (abs(np.sum(tmp_weights) - 1) < 1e-15)

                else:
                    tmp_weights = weights

                merged = tmp_weights[:] * overlapping
                average = np.sum(merged)

            # Set new output value.
            output[io, jo] = average

    return output


def create_contour(
        shakemap_layer,
        output_file_path='',
        active_band=1,
        smoothing_method=NUMPY_SMOOTHING,
        smoothing_sigma=0.9):
    """Create contour from a shake map layer by using smoothing method.

    :param shakemap_layer: The shake map raster layer.
    :type shakemap_layer: QgsRasterLayer

    :param active_band: The band which the data located, default to 1.
    :type active_band: int

    :param smoothing_method: The smoothing method that wanted to be used.
    :type smoothing_method: NONE_SMOOTHING, NUMPY_SMOOTHING, SCIPY_SMOOTHING

    :param smooth_sigma: parameter for gaussian filter used in smoothing
        function.
    :type smooth_sigma: float

    :returns: The contour of the shake map layer.
    :rtype: QgsRasterLayer
    """
    # Set output path
    if not output_file_path:
        input_layer_path = shakemap_layer.source()
        input_directory = os.path.dirname(input_layer_path)
        input_file_name = os.path.basename(input_layer_path)
        input_base_name = os.path.splitext(input_file_name)[0]
        output_file_path = os.path.join(
            input_directory, input_base_name + '-contour' + '.geojson'
        )
    # convert to numpy
    raster_file = gdal.Open(shakemap_layer.source())
    shakemap_array = np.array(
        raster_file.GetRasterBand(active_band).ReadAsArray())

    # do smoothing
    if smoothing_method == NUMPY_SMOOTHING:
        smoothed_array = convolve(shakemap_array, gaussian_kernel(
            smoothing_sigma))
    else:
        smoothed_array = shakemap_array

    # create contour
    # Based largely on
    # http://svn.osgeo.org/gdal/trunk/autotest/alg/contour.py
    driver = ogr.GetDriverByName('ESRI Shapefile')
    ogr_dataset = driver.CreateDataSource(output_file_path)
    if ogr_dataset is None:
        # Probably the file existed and could not be overriden
        raise ContourCreationError(
            'Could not create datasource for:\n%s. Check that the file '
            'does not already exist and that you do not have file system '
            'permissions issues' % output_file_path)
    layer = ogr_dataset.CreateLayer('contour')
    field_definition = ogr.FieldDefn('ID', ogr.OFTInteger)
    layer.CreateField(field_definition)
    field_definition = ogr.FieldDefn('MMI', ogr.OFTReal)
    layer.CreateField(field_definition)
    # So we can fix the x pos to the same x coord as centroid of the
    # feature so labels line up nicely vertically
    field_definition = ogr.FieldDefn('X', ogr.OFTReal)
    layer.CreateField(field_definition)
    # So we can fix the y pos to the min y coord of the whole contour so
    # labels line up nicely vertically
    field_definition = ogr.FieldDefn('Y', ogr.OFTReal)
    layer.CreateField(field_definition)
    # So that we can set the html hex colour based on its MMI class
    field_definition = ogr.FieldDefn('RGB', ogr.OFTString)
    layer.CreateField(field_definition)
    # So that we can set the label in it roman numeral form
    field_definition = ogr.FieldDefn('ROMAN', ogr.OFTString)
    layer.CreateField(field_definition)
    # So that we can set the label horizontal alignment
    field_definition = ogr.FieldDefn('ALIGN', ogr.OFTString)
    layer.CreateField(field_definition)
    # So that we can set the label vertical alignment
    field_definition = ogr.FieldDefn('VALIGN', ogr.OFTString)
    layer.CreateField(field_definition)
    # So that we can set feature length to filter out small features
    field_definition = ogr.FieldDefn('LEN', ogr.OFTReal)
    layer.CreateField(field_definition)

    # see http://gdal.org/java/org/gdal/gdal/gdal.html for these options
    contour_interval = 0.5
    contour_base = 0
    fixed_level_list = []
    use_no_data_flag = 0
    no_data_value = -9999
    id_field = 0  # first field defined above
    elevation_field = 1  # second (MMI) field defined above
    try:
        gdal.ContourGenerate(
            smoothed_array,
            contour_interval,
            contour_base,
            fixed_level_list,
            use_no_data_flag,
            no_data_value,
            layer,
            id_field,
            elevation_field)
    except Exception, e:
        LOGGER.exception('Contour creation failed')
        raise ContourCreationError(str(e))
    finally:
        ogr_dataset.Release()

    output_directory = os.path.dirname(output_file_path)
    output_file_name = os.path.basename(output_file_path)
    output_base_name = os.path.splitext(output_file_name)[0]

    # Copy over the standard .prj file since ContourGenerate does not
    # create a projection definition
    projection_path = os.path.join(
        output_directory, output_base_name + '.prj')
    source_projection_path = resources_path(
        'converter_data', 'mmi-contours.prj')
    shutil.copyfile(source_projection_path, projection_path)

    # Lastly copy over the standard qml (QGIS Style file)
    qml_path = os.path.join(
        output_directory, output_base_name + '.qml')
    source_qml_path = resources_path('converter_data', 'mmi-contours.qml')
    shutil.copyfile(source_qml_path, qml_path)

    return output_file_path
