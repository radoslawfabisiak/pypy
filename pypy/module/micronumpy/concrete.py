from pypy.interpreter.error import OperationError, oefmt
from rpython.rlib import jit, rgc
from rpython.rlib.buffer import Buffer
from rpython.rlib.debug import make_sure_not_resized, debug_print
from rpython.rlib.rawstorage import alloc_raw_storage, free_raw_storage, \
    raw_storage_getitem, raw_storage_setitem, RAW_STORAGE
from rpython.rtyper.lltypesystem import rffi, lltype, llmemory
from pypy.module.micronumpy import support, loop, constants as NPY
from pypy.module.micronumpy.base import convert_to_array, W_NDimArray, \
    ArrayArgumentException, W_NumpyObject
from pypy.module.micronumpy.iterators import ArrayIter
from pypy.module.micronumpy.strides import (Chunk, Chunks, NewAxisChunk,
    RecordChunk, calc_strides, calc_new_strides, shape_agreement,
    calculate_broadcast_strides, calc_backstrides, calc_start, is_c_contiguous,
    is_f_contiguous)
from rpython.rlib.objectmodel import keepalive_until_here
from rpython.rtyper.annlowlevel import cast_gcref_to_instance
from pypy.interpreter.baseobjspace import W_Root


class BaseConcreteArray(object):
    _immutable_fields_ = ['dtype?', 'storage', 'start', 'size', 'shape[*]',
                          'strides[*]', 'backstrides[*]', 'order', 'gcstruct',
                          'flags']
    start = 0
    parent = None
    flags = 0

    # JIT hints that length of all those arrays is a constant

    def get_shape(self):
        shape = self.shape
        jit.hint(len(shape), promote=True)
        return shape

    def get_strides(self):
        strides = self.strides
        jit.hint(len(strides), promote=True)
        return strides

    def get_backstrides(self):
        backstrides = self.backstrides
        jit.hint(len(backstrides), promote=True)
        return backstrides

    def getitem(self, index):
        return self.dtype.itemtype.read(self, index, 0)

    def getitem_bool(self, index):
        return self.dtype.itemtype.read_bool(self, index, 0)

    def setitem(self, index, value):
        self.dtype.itemtype.store(self, index, 0, value)

    @jit.unroll_safe
    def setslice(self, space, arr):
        if len(arr.get_shape()) > 0 and len(self.get_shape()) == 0:
            raise oefmt(space.w_ValueError,
                "could not broadcast input array from shape "
                "(%s) into shape ()",
                ','.join([str(x) for x in arr.get_shape()]))
        shape = shape_agreement(space, self.get_shape(), arr)
        impl = arr.implementation
        if impl.storage == self.storage:
            impl = impl.copy(space)
        loop.setslice(space, shape, self, impl)

    def get_size(self):
        return self.size // self.dtype.elsize

    def get_storage_size(self):
        return self.size

    def reshape(self, orig_array, new_shape):
        # Since we got to here, prod(new_shape) == self.size
        new_strides = None
        if self.size == 0:
            new_strides, _ = calc_strides(new_shape, self.dtype, self.order)
        else:
            if len(self.get_shape()) == 0:
                new_strides = [self.dtype.elsize] * len(new_shape)
            else:
                new_strides = calc_new_strides(new_shape, self.get_shape(),
                                               self.get_strides(), self.order)
                if new_strides is None or len(new_strides) != len(new_shape):
                    return None
        if new_strides is not None:
            # We can create a view, strides somehow match up.
            new_backstrides = calc_backstrides(new_strides, new_shape)
            assert isinstance(orig_array, W_NDimArray) or orig_array is None
            return SliceArray(self.start, new_strides, new_backstrides,
                              new_shape, self, orig_array)
        return None

    def get_view(self, space, orig_array, dtype, new_shape, strides=None, backstrides=None):
        if not strides:
            strides, backstrides = calc_strides(new_shape, dtype,
                                                    self.order)
        return SliceArray(self.start, strides, backstrides, new_shape,
                          self, orig_array, dtype=dtype)

    def get_real(self, space, orig_array):
        strides = self.get_strides()
        backstrides = self.get_backstrides()
        if self.dtype.is_complex():
            dtype = self.dtype.get_float_dtype(space)
            return SliceArray(self.start, strides, backstrides,
                              self.get_shape(), self, orig_array, dtype=dtype)
        return SliceArray(self.start, strides, backstrides,
                          self.get_shape(), self, orig_array)

    def set_real(self, space, orig_array, w_value):
        tmp = self.get_real(space, orig_array)
        tmp.setslice(space, convert_to_array(space, w_value))

    def get_imag(self, space, orig_array):
        strides = self.get_strides()
        backstrides = self.get_backstrides()
        if self.dtype.is_complex():
            dtype = self.dtype.get_float_dtype(space)
            return SliceArray(self.start + dtype.elsize, strides, backstrides,
                              self.get_shape(), self, orig_array, dtype=dtype)
        impl = NonWritableArray(self.get_shape(), self.dtype, self.order,
                                strides, backstrides)
        if not self.dtype.is_flexible():
            impl.fill(space, self.dtype.box(0))
        return impl

    def set_imag(self, space, orig_array, w_value):
        tmp = self.get_imag(space, orig_array)
        tmp.setslice(space, convert_to_array(space, w_value))

    # -------------------- applevel get/setitem -----------------------

    @jit.unroll_safe
    def _lookup_by_index(self, space, view_w):
        item = self.start
        strides = self.get_strides()
        for i, w_index in enumerate(view_w):
            if space.isinstance_w(w_index, space.w_slice):
                raise IndexError
            idx = support.index_w(space, w_index)
            if idx < 0:
                idx = self.get_shape()[i] + idx
            if idx < 0 or idx >= self.get_shape()[i]:
                raise oefmt(space.w_IndexError,
                            "index %d is out of bounds for axis %d with size "
                            "%d", idx, i, self.get_shape()[i])
            item += idx * strides[i]
        return item

    @jit.unroll_safe
    def _lookup_by_unwrapped_index(self, space, lst):
        item = self.start
        shape = self.get_shape()
        strides = self.get_strides()
        assert len(lst) == len(shape)
        for i, idx in enumerate(lst):
            if idx < 0:
                idx = shape[i] + idx
            if idx < 0 or idx >= shape[i]:
                raise oefmt(space.w_IndexError,
                            "index %d is out of bounds for axis %d with size "
                            "%d", idx, i, self.get_shape()[i])
            item += idx * strides[i]
        return item

    def getitem_index(self, space, index):
        return self.getitem(self._lookup_by_unwrapped_index(space, index))

    def setitem_index(self, space, index, value):
        self.setitem(self._lookup_by_unwrapped_index(space, index), value)

    @jit.unroll_safe
    def _single_item_index(self, space, w_idx):
        """ Return an index of single item if possible, otherwise raises
        IndexError
        """
        if (space.isinstance_w(w_idx, space.w_str) or
            space.isinstance_w(w_idx, space.w_slice) or
            space.is_w(w_idx, space.w_None)):
            raise IndexError
        if isinstance(w_idx, W_NDimArray) and not w_idx.is_scalar():
            raise ArrayArgumentException
        shape = self.get_shape()
        shape_len = len(shape)
        view_w = None
        if space.isinstance_w(w_idx, space.w_list):
            raise ArrayArgumentException
        if space.isinstance_w(w_idx, space.w_tuple):
            view_w = space.fixedview(w_idx)
            if len(view_w) < shape_len:
                raise IndexError
            if len(view_w) > shape_len:
                # we can allow for one extra None
                count = len(view_w)
                for w_item in view_w:
                    if space.is_w(w_item, space.w_None):
                        count -= 1
                if count == shape_len:
                    raise IndexError # but it's still not a single item
                raise oefmt(space.w_IndexError, "invalid index")
            # check for arrays
            for w_item in view_w:
                if (isinstance(w_item, W_NDimArray) or
                    space.isinstance_w(w_item, space.w_list)):
                    raise ArrayArgumentException
            return self._lookup_by_index(space, view_w)
        if shape_len == 0:
            raise oefmt(space.w_IndexError, "0-d arrays can't be indexed")
        elif shape_len > 1:
            raise IndexError
        idx = support.index_w(space, w_idx)
        return self._lookup_by_index(space, [space.wrap(idx)])

    @jit.unroll_safe
    def _prepare_slice_args(self, space, w_idx):
        if space.isinstance_w(w_idx, space.w_str):
            idx = space.str_w(w_idx)
            dtype = self.dtype
            if not dtype.is_record() or idx not in dtype.fields:
                raise oefmt(space.w_ValueError, "field named %s not found", idx)
            return RecordChunk(idx)
        elif (space.isinstance_w(w_idx, space.w_int) or
                space.isinstance_w(w_idx, space.w_slice)):
            if len(self.get_shape()) == 0:
                raise oefmt(space.w_ValueError, "cannot slice a 0-d array")
            return Chunks([Chunk(*space.decode_index4(w_idx, self.get_shape()[0]))])
        elif isinstance(w_idx, W_NDimArray) and w_idx.is_scalar():
            w_idx = w_idx.get_scalar_value().item(space)
            if not space.isinstance_w(w_idx, space.w_int) and \
                    not space.isinstance_w(w_idx, space.w_bool):
                raise OperationError(space.w_IndexError, space.wrap(
                    "arrays used as indices must be of integer (or boolean) type"))
            return Chunks([Chunk(*space.decode_index4(w_idx, self.get_shape()[0]))])
        elif space.is_w(w_idx, space.w_None):
            return Chunks([NewAxisChunk()])
        result = []
        i = 0
        for w_item in space.fixedview(w_idx):
            if space.is_w(w_item, space.w_None):
                result.append(NewAxisChunk())
            else:
                result.append(Chunk(*space.decode_index4(w_item,
                                                         self.get_shape()[i])))
                i += 1
        return Chunks(result)

    def descr_getitem(self, space, orig_arr, w_index):
        try:
            item = self._single_item_index(space, w_index)
            return self.getitem(item)
        except IndexError:
            # not a single result
            chunks = self._prepare_slice_args(space, w_index)
            return chunks.apply(space, orig_arr)

    def descr_setitem(self, space, orig_arr, w_index, w_value):
        try:
            item = self._single_item_index(space, w_index)
            self.setitem(item, self.dtype.coerce(space, w_value))
        except IndexError:
            w_value = convert_to_array(space, w_value)
            chunks = self._prepare_slice_args(space, w_index)
            view = chunks.apply(space, orig_arr)
            view.implementation.setslice(space, w_value)

    def transpose(self, orig_array):
        if len(self.get_shape()) < 2:
            return self
        strides = []
        backstrides = []
        shape = []
        for i in range(len(self.get_shape()) - 1, -1, -1):
            strides.append(self.get_strides()[i])
            backstrides.append(self.get_backstrides()[i])
            shape.append(self.get_shape()[i])
        return SliceArray(self.start, strides,
                          backstrides, shape, self, orig_array)

    def copy(self, space):
        strides, backstrides = calc_strides(self.get_shape(), self.dtype,
                                                    self.order)
        impl = ConcreteArray(self.get_shape(), self.dtype, self.order, strides,
                             backstrides)
        return loop.setslice(space, self.get_shape(), impl, self)

    def create_iter(self, shape=None, backward_broadcast=False):
        if shape is not None and \
                support.product(shape) > support.product(self.get_shape()):
            r = calculate_broadcast_strides(self.get_strides(),
                                            self.get_backstrides(),
                                            self.get_shape(), shape,
                                            backward_broadcast)
            i = ArrayIter(self, support.product(shape), shape, r[0], r[1])
        else:
            i = ArrayIter(self, self.get_size(), self.shape,
                          self.strides, self.backstrides)
        return i, i.reset()

    def swapaxes(self, space, orig_arr, axis1, axis2):
        shape = self.get_shape()[:]
        strides = self.get_strides()[:]
        backstrides = self.get_backstrides()[:]
        shape[axis1], shape[axis2] = shape[axis2], shape[axis1]
        strides[axis1], strides[axis2] = strides[axis2], strides[axis1]
        backstrides[axis1], backstrides[axis2] = backstrides[axis2], backstrides[axis1]
        return W_NDimArray.new_slice(space, self.start, strides,
                                     backstrides, shape, self, orig_arr)

    def nonzero(self, space, index_type):
        s = loop.count_all_true_concrete(self)
        box = index_type.itemtype.box
        nd = len(self.get_shape()) or 1
        w_res = W_NDimArray.from_shape(space, [s, nd], index_type)
        loop.nonzero(w_res, self, box)
        w_res = w_res.implementation.swapaxes(space, w_res, 0, 1)
        l_w = [w_res.descr_getitem(space, space.wrap(d)) for d in range(nd)]
        return space.newtuple(l_w)

    ##def get_storage(self):
    ##    return self.storage
    ## use a safer context manager
    def __enter__(self):
        return self.storage

    def __exit__(self, typ, value, traceback):
        keepalive_until_here(self)

    def get_buffer(self, space, readonly):
        return ArrayBuffer(self, readonly)

    def astype(self, space, dtype):
        # copy the general pattern of the strides
        # but make the array storage contiguous in memory
        shape = self.get_shape()
        strides = self.get_strides()
        if len(strides) > 0:
            mins = strides[0]
            t_elsize = dtype.elsize
            for s in strides:
                if s < mins:
                    mins = s
            t_strides = [s * t_elsize / mins for s in strides]
            backstrides = calc_backstrides(t_strides, shape)
        else:
            t_strides = []
            backstrides = []
        impl = ConcreteArray(shape, dtype, self.order, t_strides, backstrides)
        loop.setslice(space, impl.get_shape(), impl, self)
        return impl

OBJECTSTORE = lltype.GcStruct('ObjectStore',
                              ('length', lltype.Signed),
                              ('step', lltype.Signed),
                              ('storage', llmemory.Address),
                              rtti=True)
offset_of_storage = llmemory.offsetof(OBJECTSTORE, 'storage')
offset_of_length = llmemory.offsetof(OBJECTSTORE, 'length')
offset_of_step = llmemory.offsetof(OBJECTSTORE, 'step')

V_OBJECTSTORE = lltype.nullptr(OBJECTSTORE)

def customtrace(gc, obj, callback, arg):
    #debug_print('in customtrace w/obj', obj)
    length = (obj + offset_of_length).signed[0]
    step = (obj + offset_of_step).signed[0]
    storage = (obj + offset_of_storage).address[0]
    #debug_print('tracing', length, 'objects in ndarray.storage')
    i = 0
    while i < length:
        gc._trace_callback(callback, arg, storage)
        storage += step
        i += 1
    
lambda_customtrace = lambda: customtrace

def _setup():
    rgc.register_custom_trace_hook(OBJECTSTORE, lambda_customtrace)

@jit.dont_look_inside
def _create_objectstore(storage, length, elsize):
    gcstruct = lltype.malloc(OBJECTSTORE)
    # JIT does not support cast_ptr_to_adr
    gcstruct.storage = llmemory.cast_ptr_to_adr(storage)
    #print 'create gcstruct',gcstruct,'with storage',storage,'as',gcstruct.storage
    gcstruct.length = length
    gcstruct.step = elsize
    return gcstruct


class ConcreteArrayNotOwning(BaseConcreteArray):
    def __init__(self, shape, dtype, order, strides, backstrides, storage, start=0):
        make_sure_not_resized(shape)
        make_sure_not_resized(strides)
        make_sure_not_resized(backstrides)
        self.shape = shape
        self.size = support.product(shape) * dtype.elsize
        self.order = order
        self.dtype = dtype
        self.strides = strides
        self.backstrides = backstrides
        self.storage = storage
        self.start = start
        self.gcstruct = V_OBJECTSTORE

    def fill(self, space, box):
        self.dtype.itemtype.fill(self.storage, self.dtype.elsize,
                                 box, 0, self.size, 0, self.gcstruct)

    def set_shape(self, space, orig_array, new_shape):
        strides, backstrides = calc_strides(new_shape, self.dtype,
                                                    self.order)
        return SliceArray(self.start, strides, backstrides, new_shape, self,
                          orig_array)

    def set_dtype(self, space, dtype):
        # size/shape/strides shouldn't change
        assert dtype.elsize == self.dtype.elsize
        self.dtype = dtype

    def argsort(self, space, w_axis):
        from .selection import argsort_array
        return argsort_array(self, space, w_axis)

    def sort(self, space, w_axis, w_order):
        from .selection import sort_array
        return sort_array(self, space, w_axis, w_order)

    def base(self):
        return None

class ConcreteArray(ConcreteArrayNotOwning):
    def __init__(self, shape, dtype, order, strides, backstrides,
                 storage=lltype.nullptr(RAW_STORAGE), zero=True):
        gcstruct = V_OBJECTSTORE
        if storage == lltype.nullptr(RAW_STORAGE):
            length = support.product(shape) 
            if dtype.num == NPY.OBJECT:
                storage = dtype.itemtype.malloc(length * dtype.elsize, zero=True)
                gcstruct = _create_objectstore(storage, length, dtype.elsize)
            else:
                storage = dtype.itemtype.malloc(length * dtype.elsize, zero=zero)
        start = calc_start(shape, strides)
        ConcreteArrayNotOwning.__init__(self, shape, dtype, order, strides, backstrides,
                                        storage, start=start)
        self.gcstruct = gcstruct
        self.flags = NPY.ARRAY_ALIGNED | NPY.ARRAY_WRITEABLE
        if is_c_contiguous(self):
            self.flags |= NPY.ARRAY_C_CONTIGUOUS
        if is_f_contiguous(self):
            self.flags |= NPY.ARRAY_F_CONTIGUOUS

    def __del__(self):
        if self.gcstruct:
            self.gcstruct.length = 0
        free_raw_storage(self.storage, track_allocation=False)


class ConcreteArrayWithBase(ConcreteArrayNotOwning):
    def __init__(self, shape, dtype, order, strides, backstrides, storage,
                 orig_base, start=0):
        ConcreteArrayNotOwning.__init__(self, shape, dtype, order,
                                        strides, backstrides, storage, start)
        self.orig_base = orig_base
        if isinstance(orig_base, W_NumpyObject):
            self.flags = orig_base.get_flags() & NPY.ARRAY_ALIGNED
            self.flags |=  orig_base.get_flags() & NPY.ARRAY_WRITEABLE
        else:
            self.flags = 0
        if is_c_contiguous(self):
            self.flags |= NPY.ARRAY_C_CONTIGUOUS
        if is_f_contiguous(self):
            self.flags |= NPY.ARRAY_F_CONTIGUOUS

    def base(self):
        return self.orig_base


class ConcreteNonWritableArrayWithBase(ConcreteArrayWithBase):
    def __init__(self, shape, dtype, order, strides, backstrides, storage,
                 orig_base, start=0):
        ConcreteArrayWithBase.__init__(self, shape, dtype, order, strides,
                backstrides, storage, orig_base, start)
        self.flags &= ~ NPY.ARRAY_WRITEABLE

    def descr_setitem(self, space, orig_array, w_index, w_value):
        raise OperationError(space.w_ValueError, space.wrap(
            "assignment destination is read-only"))


class NonWritableArray(ConcreteArray):
    def __init__(self, shape, dtype, order, strides, backstrides,
                 storage=lltype.nullptr(RAW_STORAGE), zero=True):
        ConcreteArray.__init__(self, shape, dtype, order, strides, backstrides,
                    storage, zero)
        self.flags &= ~ NPY.ARRAY_WRITEABLE
        
    def descr_setitem(self, space, orig_array, w_index, w_value):
        raise OperationError(space.w_ValueError, space.wrap(
            "assignment destination is read-only"))


class SliceArray(BaseConcreteArray):
    def __init__(self, start, strides, backstrides, shape, parent, orig_arr,
                 dtype=None):
        self.strides = strides
        self.backstrides = backstrides
        self.shape = shape
        if dtype is None:
            dtype = parent.dtype
        if isinstance(parent, SliceArray):
            parent = parent.parent # one level only
        self.parent = parent
        self.storage = parent.storage
        self.gcstruct = parent.gcstruct
        self.order = parent.order
        self.dtype = dtype
        self.size = support.product(shape) * self.dtype.elsize
        self.start = start
        self.orig_arr = orig_arr
        self.flags = parent.flags & NPY.ARRAY_ALIGNED
        self.flags |= parent.flags & NPY.ARRAY_WRITEABLE
        if is_c_contiguous(self):
            self.flags |= NPY.ARRAY_C_CONTIGUOUS
        if is_f_contiguous(self):
            self.flags |= NPY.ARRAY_F_CONTIGUOUS

    def base(self):
        return self.orig_arr

    def fill(self, space, box):
        loop.fill(self, box.convert_to(space, self.dtype))

    def set_shape(self, space, orig_array, new_shape):
        if len(self.get_shape()) < 2 or self.size == 0:
            # TODO: this code could be refactored into calc_strides
            # but then calc_strides would have to accept a stepping factor
            strides = []
            backstrides = []
            dtype = self.dtype
            try:
                s = self.get_strides()[0] // dtype.elsize
            except IndexError:
                s = 1
            if self.order == 'C':
                new_shape.reverse()
            for sh in new_shape:
                strides.append(s * dtype.elsize)
                backstrides.append(s * (sh - 1) * dtype.elsize)
                s *= max(1, sh)
            if self.order == 'C':
                strides.reverse()
                backstrides.reverse()
                new_shape.reverse()
            return self.__class__(self.start, strides, backstrides, new_shape,
                              self, orig_array)
        new_strides = calc_new_strides(new_shape, self.get_shape(),
                                       self.get_strides(),
                                       self.order)
        if new_strides is None or len(new_strides) != len(new_shape):
            raise oefmt(space.w_AttributeError,
                "incompatible shape for a non-contiguous array")
        new_backstrides = [0] * len(new_shape)
        for nd in range(len(new_shape)):
            new_backstrides[nd] = (new_shape[nd] - 1) * new_strides[nd]
        return self.__class__(self.start, new_strides, new_backstrides, new_shape,
                          self, orig_array)

    def sort(self, space, w_axis, w_order):
        from .selection import sort_array
        return sort_array(self, space, w_axis, w_order)

class NonWritableSliceArray(SliceArray):
    def __init__(self, start, strides, backstrides, shape, parent, orig_arr,
                 dtype=None):
        SliceArray.__init__(self, start, strides, backstrides, shape, parent,
                        orig_arr, dtype)
        self.flags &= ~NPY.ARRAY_WRITEABLE

    def descr_setitem(self, space, orig_array, w_index, w_value):
        raise OperationError(space.w_ValueError, space.wrap(
            "assignment destination is read-only"))


class VoidBoxStorage(BaseConcreteArray):
    def __init__(self, size, dtype):
        self.storage = alloc_raw_storage(size)
        self.gcstruct = V_OBJECTSTORE
        self.dtype = dtype
        self.size = size
        self.flags = (NPY.ARRAY_C_CONTIGUOUS | NPY.ARRAY_F_CONTIGUOUS |
                     NPY.ARRAY_WRITEABLE | NPY.ARRAY_ALIGNED)

    def __del__(self):
        free_raw_storage(self.storage)


class ArrayBuffer(Buffer):
    _immutable_ = True

    def __init__(self, impl, readonly):
        self.impl = impl
        self.readonly = readonly

    def getitem(self, item):
        return raw_storage_getitem(lltype.Char, self.impl.storage, item)

    def setitem(self, item, v):
        raw_storage_setitem(self.impl.storage, item,
                            rffi.cast(lltype.Char, v))

    def getlength(self):
        return self.impl.size

    def get_raw_address(self):
        return self.impl.storage
