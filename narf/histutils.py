import ROOT
import boost_histogram as bh
import hist
from hashlib import sha256
import time
import math
import numpy as np
import functools

ROOT.gInterpreter.Declare('#include "histutils.h"')
ROOT.gInterpreter.Declare('#include "FillBoostHelperAtomic.h"')

def bool_to_string(b):
    if b:
        return "true"
    else:
        return "false"

def convert_axis(axis):
    default = ROOT.boost.histogram.use_default

    if (axis.traits.growth):
        raise ValueError("growable axes are not supported")

    optionsval = ROOT.narf.get_option[bool_to_string(axis.traits.underflow),
                                   bool_to_string(axis.traits.overflow),
                                   bool_to_string(axis.traits.circular),
                                   bool_to_string(axis.traits.growth)]()

    options = type(optionsval)

    if isinstance(axis, bh.axis.Regular):
        #TODO add transform support
        if axis.transform is not None:
            raise ValueError("transforms are not currently supported")

        nbins = axis.size
        xlow = axis.edges[0]
        xhigh = axis.edges[-1]

        return ROOT.boost.histogram.axis.regular["double", default, default, options](nbins, xlow, xhigh)
    elif isinstance(axis, bh.axis.Variable):
        return ROOT.boost.histogram.axis.variable["double", default, options](axis.edges)
    elif isinstance(axis, bh.axis.Integer):
        ilow = axis.bin(0)
        ihigh = axis.bin(axis.size - 1) + 1
        return ROOT.boost.histogram.axis.integer["int", default, options](ilow, ihigh)
    elif isinstance(axis, bh.axis.IntCategory):
        ncats = axis.size
        cats = [axis.bin(icat) for icat in range(ncats)]
        return ROOT.boost.histogram.axis.category["int", default, options](cats)
    elif isinstance(axis, bh.axis.StrCategory):
        ncats = axis.size
        cats = [axis.bin(icat) for icat in range(ncats)]
        return ROOT.boost.histogram.axis.category[ROOT.std.string, default, options](cats)
    elif isinstance(axis, bh.axis.Boolean):
        return ROOT.boost.histogram.axis.boolean[""]()
    else:
        raise TypeError("axis must be a boost_histogram or compatible axis")

def convert_storage_type(storage, force_atomic):
    if isinstance(storage, bh.storage.Double):
        if force_atomic:
            raise TypeError("atomic storage not supported for storage type Double")
        else:
            return "double"
    elif isinstance(storage, bh.storage.Unlimited):
        raise TypeError("Unlimited storage not supported")
    elif isinstance(storage, bh.storage.Int64):
        if force_atomic:
            return "boost::histogram::accumulators::count<std::int64_t, true>"
        else:
            return "std::int64_t"
    elif isinstance(storage, bh.storage.AtomicInt64):
        return "boost::histogram::accumulators::count<std::int64_t, true>"
    elif isinstance(storage, bh.storage.Weight):
        if force_atomic:
            return "narf::weighted_sum<double, true>"
            #return "narf::weighted_sum<double, false>"
        else:
            return "boost::histogram::accumulators::weighted_sum<double>"
    elif isinstance(storage, bh.storage.Mean):
        if force_atomic:
            raise TypeError("atomic storage not supported for storage type Mean")
        else:
            return "boost::histogram::accumulators::mean<double>"
    elif isinstance(storage, bh.storage.WeightedMean):
        if force_atomic:
            raise TypeError("atomic storage not supported for storage type WeightedMean")
        else:
            return "boost::histogram::accumulators::weighted_mean<double>"
    else:
        raise TypeError("storage must be a boost_histogram or compatible storage type")

def _histo_boost(df, name, axes, cols, storage = bh.storage.Weight(), force_atomic = ROOT.ROOT.IsImplicitMTEnabled()):
    # first construct a histogram from the hist python interface, then construct a boost histogram
    # using PyROOT with compatible axes and storage types, adopting the underlying storage
    # of the python hist histogram

    _hist = hist.Hist(*axes, storage = storage)

    arr = _hist.view(flow = True).__array_interface__

    addr = arr["data"][0]
    shape = arr["shape"]
    elem_size = int(arr["typestr"][2:])
    strides = arr["strides"]

    size = math.prod(shape)
    size_bytes = size*elem_size

    # compute strides for a fortran-style contiguous array with the given shape
    stridesf = []
    current_stride = elem_size
    for axis_size in shape:
        stridesf.append(current_stride)
        current_stride *= axis_size
    stridesf = tuple(stridesf)

    if strides is None:
        #default stride for C-style contiguous array
        strides = tuple(reversed(stridesf))

    # check that memory is a fortran-style contiguous array
    if strides != stridesf:
        raise ValueError("memory is not a contiguous fortran-style array as required by the C++ class")

    cppaxes = [convert_axis(axis) for axis in axes]
    cppstoragetype = convert_storage_type(storage, force_atomic)

    h = ROOT.narf.make_histogram_adopted[cppstoragetype](addr, size_bytes, *cppaxes)

    # confirm storage order empirically
    origin = (0,)*len(shape)
    origin_addr = ROOT.addressof(h.at(*origin))

    for iaxis, stride in enumerate(strides):
        coords = [0,]*len(shape)
        coords[iaxis] = 1
        addr = ROOT.addressof(h.at(*coords))
        addr_diff = addr - origin_addr
        if addr_diff != stride:
            raise ValueError("mismatched storage ordering")

    helper = ROOT.narf.FillBoostHelperAtomic[type(h)](ROOT.std.move(h))
    coltypes = [df.GetColumnType(col) for col in cols]
    targs = tuple([type(df), type(helper)] + coltypes)
    res = ROOT.narf.book_helper[targs](df, ROOT.std.move(helper), cols)

    res.name = name
    res._hist = _hist

    # hide underlying C++ class and return the python version instead

    res._GetValue = res.GetValue

    def get_hist():
        res._GetValue()
        return res._hist

    def hist_getitem(*args, **kwargs):
        res._GetValue()
        return res._hist.__getitem__(*args, **kwargs)

    ret_null = lambda : None

    res.__deref__ = get_hist
    res.__follow__ = get_hist
    res.begin = ret_null
    res.end = ret_null
    res.GetPtr = get_hist
    res.GetValue = get_hist
    res.__getitem__ = hist_getitem

    return res

def _sum_and_count(df, col):
    sumres = df.Sum(col)
    countres = df.Count()
    return (sumres, countres)

@ROOT.pythonization("RInterface<", ns="ROOT::RDF", is_prefix=True)
def pythonize_rdataframe(klass):
    # add function for boost histograms
    klass.HistoBoost = _histo_boost
    klass.SumAndCount = _sum_and_count
