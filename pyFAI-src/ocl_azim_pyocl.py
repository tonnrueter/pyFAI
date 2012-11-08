# -*- coding: utf-8 -*-
#
#    Project: Azimuthal integration
#             https://forge.epn-campus.eu/projects/azimuthal
#
#    File: "$Id$"
#
#    Copyright (C) European Synchrotron Radiation Facility, Grenoble, France
#
#    Principal author:       Jérôme Kieffer (Jerome.Kieffer@ESRF.eu)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

"""
Attempt to implements ocl_azim using pyopencl
"""
__author__ = "Jerome Kieffer"
__license__ = "GPLv3"
__date__ = "07/11/2012"
__copyright__ = "2012, ESRF, Grenoble"
__contact__ = "jerome.kieffer@esrf.fr"

import os, logging
import threading
import hashlib
import numpy
from opencl import ocl, pyopencl
mf = pyopencl.mem_flags
logger = logging.getLogger("pyFAI.ocl_azim_pyocl")

class Integrator1d(object):
    """
    Attempt to implements ocl_azim using pyopencl
    """

    def __cinit__(self, filename=None):
        """
        Cython only contructor
        """
        self._nBins = -1
        self._nData = -1
        self._platformid = -1
        self._deviceid = -1
        self._useFp64 = False
        self._devicetype = "gpu"
        self.filename = filename
        self.lock = threading.Semaphore()
        #Those are pointer to memory on the GPU (or None if uninitialized
        self._cl_mem = {"tth":None,
                        "image":None,
                        "solidangle":None,
                        "histogram":None,
                        "uhistogram":None,
                        "uweights":None,
                        "span_ranges":None,
                        "tth_min_max":None,
                        "tth_delta":None,
                        "mask":None,
                        "dummyval":None,
                        "dummyval_delta":None,
                        "dark":None
                        }
        self._cl_kernels = {"integrate":None,
                            "uimemset2":None,
                            "imemset":None,
                            "ui2f2":None,
                            "get_spans":None,
                            "group_spans":None,
                            "solidangle_correction":None,
                            "dummyval_correction":None}
        self._ctx = None
        self._queue = None
        self.do_solidangle = None

    def __dealloc__(self):
        self.tth_out = None
        self._free_buffers()
        self._queue.finish()
        self._queue = None
        self._ctx = None

    def __repr__(self):
        return os.linesep.join(["Cython wrapper for ocl_xrpd1d.ocl_xrpd1D_fullsplit C++ class. Logging in %s" % self.filename,
                                "device: %s, platform %s device %s 64bits:%s image size: %s histogram size: %s" % (self._devicetype, self._platformid, self._deviceid, self._useFp64, self._nData, self._nBins),
                                ",\t ".join(["%s: %s" % (k, v) for k, v in self.get_status().items()])])

    def _free_buffers(self):
        """
        free all memory allocated on the device
        """
        for buffer_name in self._cl_mem:
            if self._cl_mem[buffer] is not None:
                try:
                    self._cl_mem[buffer].release()
                    self._cl_mem[buffer] = None
                except LogicError:
                    logger.error("Error while freeing buffer %s" % buffer_name)


    def _calc_tth_out(self, lower, upper):
        """
        Calculate the bin-center position in 2theta
        """
        self.tth_min = float(lower)
        self.tth_max = float(upper)
        delta = (upper - lower) / float(self._nBins)
        self.tth_out = numpy.arange(lower, upper, delta, dtype=numpy.float32)


    def getConfiguration(self, Nimage, Nbins, useFp64=None):
        """getConfiguration gets the description of the integrations to be performed and keeps an internal copy
        @param Nimage: number of pixel in image
        @param Nbins: number of bins in regrouped histogram
        @param useFp64: use double precision. By default the same as init!
        """
        if useFp64 is not None:
            self._useFp64 = bool(useFp64)
        self._nBins = Nbins
        self._nData = Nimage

    def configure(self, kernel=None):
        """configure is possibly the most crucial method of the class.
        It is responsible of allocating the required memory and compile the OpenCL kernels
        based on the configuration of the integration.
        It also "ties" the OpenCL memory to the kernel arguments.
        If ANY of the arguments of getConfiguration needs to be changed, configure must
        be called again for them to take effect

        @param kernel: name or path to the file containing the kernel
        """
        kernel_name = "ocl_azim_kernel_2.cl"
        if kernel is None:
            if os.path.isfile(kernel_name):
                kernel = kernel_name
            else:
                kernel = os.path.join(os.path.dirname(os.path.abspath(__file__)), kernel_name)
        else:
            kernel = str(kernel)

        try:
            self._ctx = pyopencl.Context(devices=[pyopencl.get_platforms()[platformid].get_devices()[deviceid]])
            self._queue = pyopencl.CommandQueue(self._ctx)
            self._program = pyopencl.Program(self._ctx, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocl_azim_LUT.cl")).read()).build()
            if self.device_type == "CPU":
                self._lut_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=lut)
            else:
                self._lut_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=lut.T.copy())
            self.outData_buffer = pyopencl.Buffer(self._ctx, mf.WRITE_ONLY, numpy.dtype(numpy.float32).itemsize * self.bins)
            self.outCount_buffer = pyopencl.Buffer(self._ctx, mf.WRITE_ONLY, numpy.dtype(numpy.float32).itemsize * self.bins)
            self.outMerge_buffer = pyopencl.Buffer(self._ctx, mf.WRITE_ONLY, numpy.dtype(numpy.float32).itemsize * self.bins)
            self.dark_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY, size=1)
            self.flat_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY, size=1)
            self.solidAngle_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY, size=1)
            self.polarization_buffer = pyopencl.Buffer(self._ctx, mf.READ_ONLY, size=1)
        except pyopencl.MemoryError as error:
            raise MemoryError(error)



    def loadTth(self, tth, dtth , tth_min=None, tth_max=None):
        """
        Load the 2th arrays along with the min and max value.

        loadTth maybe be recalled at any time of the execution in order to update
        the 2th arrays.

        loadTth is required and must be called at least once after a configure()
        """

        self._tth_max = (tth + dtth).max() * (1.0 + numpy.finfo(numpy.float32).eps)
        self._tth_min = max(0.0, (tthc - dtthc).min())
        if tth_min is None:
            tth_min = self._tth_min

        if tth_max is None:
            tth_max = self._tth_max
        self._calc_tth_out(tth_min, tth_max)
        #TODO: setup buffer, upload tth and dtth

    def setSolidAngle(self, solidAngle):
        """
        Enables SolidAngle correction and uploads the suitable array to the OpenCL device.

        By default the program will assume no solidangle correction unless setSolidAngle() is called.
        From then on, all integrations will be corrected via the SolidAngle array.

        If the SolidAngle array needs to be changes, one may just call setSolidAngle() again
        with that array

        @param solidAngle: numpy array representing the solid angle of the given pixel
        """
        cSolidANgle = numpy.ascontiguousarray(solidAngle.ravel(), dtype=numpy.float32)
        with self.lock:
            self.do_solidangle = True
            if self._cl_mem["solidangle"] is not None:
               self._cl_mem["solidangle"].release()
            self._cl_mem["solidangle"] = pyopencl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=cSolidANgle)

    def unsetSolidAngle(self):
        """
        Instructs the program to not perform solidangle correction from now on.

        SolidAngle correction may be turned back on at any point
        """
        with self.lock:
            self.do_solidangle = False
            if self._cl_mem["solidangle"] is not None:
               self._cl_mem["solidangle"].release()
               self._cl_mem["solidangle"] = None

    def setMask(self, mask):
        """
        Enables the use of a Mask during integration. The Mask can be updated by
        recalling setMask at any point.

        The Mask must be a PyFAI Mask. Pixels with 0 are masked out. TODO: check and invert!
        @param mask: numpy.ndarray of integer.
        """
        cMask = numpy.ascontiguousarray(mask.ravel(), dtype=numpy.int32)
        with self.lock:
            self.do_mask = True
            if self._cl_mem["mask"] is not None:
               self._cl_mem["mask"].release()
            self._cl_mem["mask"] = pyopencl.Buffer(self._ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=cMask)

    def unsetMask(self):
        """
        Disables the use of a Mask from that point.
        It may be re-enabled at any point via setMask
        """
        return self.cpp_integrator.unsetMask()

    def setDummyValue(self, float dummy, float delta_dummy):
        """
        Enables dummy value functionality and uploads the value to the OpenCL device.
        Image values that are similar to the dummy value are set to 0.
        @param dummy
        @param delta_dummy
        """
        cdef int rc
        with nogil:
            rc = self.cpp_integrator.setDummyValue(dummy, delta_dummy)
        return rc

    def unsetDummyValue(self):
        """Disable a dummy value.
        May be re-enabled at any time by setDummyValue
        """
        return self.cpp_integrator.unsetDummyValue()

    def setRange(self, float lowerBound, float upperBound):
        """Sets the active range to integrate on. By default the range is set to tth_min and tth_max
        By calling this functions, one may change to different bounds

        @param lowerBound: usually tth_min
        @param upperBound: usually tth_max
        @return: integer
        """
        self._calc_tth_out(lowerBound, upperBound)
        return self.cpp_integrator.setRange(lowerBound, upperBound)

    def unsetRange(self):
        "Resets the 2th integration range back to tth_min, tth_max"
        self._calc_tth_out(self._tth_min, self._tth_max)
        return self.cpp_integrator.unsetRange()

    def execute(self, numpy.ndarray image not None):
        """Take an image, integrate and return the histogram and weights
        set / unset and loadTth methods have a direct impact on the execute() method.
        All the rest of the methods will require at least a new configuration via configure()

        @param image: image to be processed as a numpy array
        @return: tth_out, histogram, bins

        TODO: to improve performances, the image should be casted to float32 in an optimal way:
        currently using numpy machinery but would be better if done in OpenCL
        """
        cdef int rc
        cdef numpy.ndarray[numpy.float32_t, ndim = 1] cimage, histogram, bins, tth_out
        cimage = numpy.ascontiguousarray(image.ravel(), dtype=numpy.float32)
        histogram = numpy.empty(self._nBins, dtype=numpy.float32)
        bins = numpy.empty(self._nBins, dtype=numpy.float32)
        tth_out = numpy.empty(self._nBins, dtype=numpy.float32)
        assert cimage.size == self._nData
        with nogil:
            rc = self.cpp_integrator.execute(< float *> cimage.data, < float *> histogram.data, < float *> bins.data)
        if rc != 0:
            raise RuntimeError("OpenCL integrator failed with RC=%s" % rc)

        memcpy(tth_out.data, self.ctth_out, self._nBins * sizeof(float))
        return tth_out, histogram, bins

    def clean(self, int preserve_context=0):
        """Free OpenCL related resources.
        It may be asked to preserve the context created by init or completely clean up OpenCL.

        Guard / Status flags that are set will be reset. All the Operation flags are also reset"""
        return  self.cpp_integrator.clean(preserve_context)

################################################################################
# Methods inherited from ocl_base class
################################################################################
    def init(self, devicetype="gpu", useFp64=True, platformid=None, deviceid=None):
        """Initial configuration: Choose a device and initiate a context. Devicetypes can be GPU,gpu,CPU,cpu,DEF,ACC,ALL.
        Suggested are GPU,CPU. For each setting to work there must be such an OpenCL device and properly installed.
        E.g.: If Nvidia driver is installed, GPU will succeed but CPU will fail. The AMD SDK kit is required for CPU via OpenCL.
        @param devicetype: string in ["cpu","gpu", "all", "acc"]
        @param useFp64: boolean specifying if double precision will be used
        @param platformid: integer
        @param devid: integer
        """
        cdef int forceIDs, rc
        self._useFp64 = < cpp_bool > useFp64
        self._devicetype = < char *> devicetype
        if (platformid is not None) and (deviceid is not None):
            self._platformid = < int > int(platformid)
            self._deviceid = < int > int(deviceid)
        else:
            if useFp64:
                ids = ocl.select_device(type=devicetype, extensions=["cl_khr_int64_base_atomics"])
            else:
                ids = ocl.select_device(type=devicetype)
            self._platformid = < int > ids[0]
            self._deviceid = < int > ids[1]

        with nogil:
            rc = self.cpp_integrator.init(< char *> self._devicetype, < int > self._platformid, < int > self._deviceid, < cpp_bool > self._useFp64)
        return rc

    def show_devices(self, to_log=True):
        """
        Prints a list of OpenCL capable devices, their platforms and their ids"
        @param to_log: Set to false if you want to have info printed on screen
        """
        self.cpp_integrator.show_devices(< int > to_log)

    def show_device_details(self, to_log=True):
        """
        Print details of a selected device
        @param to_log: Set to false if you want to have info printed on screen
        """
        self.cpp_integrator.show_device_details(< int > to_log)

    def reset_time(self):
        'Resets the internal profiling timers to 0'
        self.cpp_integrator.reset_time()

    def get_exec_time(self):
        "Returns the internal profiling timer for the kernel executions"
        return self.cpp_integrator.get_exec_time()

    def get_exec_count(self):
        "Returns how many integrations have been performed"
        return self.cpp_integrator.get_exec_count()

    def get_memCpy_time(self):
        "Returns the time spent on memory copies"
        return self.cpp_integrator.get_memCpy_time()

    def get_status(self):
        "return a dictionnary with the status of the integrator"
        retbin = numpy.binary_repr(self.cpp_integrator.get_status(), 9)
        out = {}
        for i, v in enumerate(['dummy', 'mask', 'dark', 'solid_angle', 'pos1', 'pos0', 'compiled', 'size', 'context']):
            out[v] = bool(int(retbin[i]))
        return out

    def get_contexed_Ids(self):
        """
        @return: 2-tuple of integers corresponding to (platform_id, device_id)
        """
        cdef int platform = -1, device = -1
        self.cpp_integrator.get_contexed_Ids(platform, device)
        return (platform, device)

    def get_platform_info(self):
        """
        @return: dict with platform info
        """
        out = {}
        out["name"] = self.cpp_integrator.platform_info.name
        out["vendor"] = self.cpp_integrator.platform_info.vendor
        out["extensions"] = self.cpp_integrator.platform_info.extensions
        out["version"] = self.cpp_integrator.platform_info.version
        return out

    def get_device_info(self):
        """
        @return: dict with device info
        """
        out = {}
        out["name"] = self.cpp_integrator.device_info.name
        out["type"] = self.cpp_integrator.device_info.type
        out["version"] = self.cpp_integrator.device_info.version
        out["driver_version"] = self.cpp_integrator.device_info.driver_version
        out["extensions"] = self.cpp_integrator.device_info.extensions
        out["global_mem"] = self.cpp_integrator.device_info.global_mem
        return out