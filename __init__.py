import gc
import sys
import time
from functools import wraps
from multiprocessing.shared_memory import SharedMemory
from multiprocessing import RLock
import pickle
import dill

_lock = RLock()
cfg = sys.modules[__name__]
cfg.protocol = pickle.HIGHEST_PROTOCOL
cfg.with_lock = True


def lock(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if cfg.with_lock:
            try:
                _lock.acquire()
            except Exception:
                pass
        try:
            return func(*args, **kwargs)
        finally:
            if cfg.with_lock:
                try:
                    _lock.release()
                except Exception:
                    pass

    return wrapper


def get_or_create_memory_block(name: str, size: int, newval=None) -> SharedMemory:
    # Based on https://github.com/luizalabs/shared-memory-dict
    # The MIT License (MIT)
    #
    # Copyright (c) 2020 LuizaLabs
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
    try:
        return SharedMemory(name=name), True
    except FileNotFoundError:
        dm = SharedMemory(name=name, create=True, size=size)
        asdill = dill.dumps(newval, protocol=cfg.protocol)
        dm.buf[: len(asdill)] = asdill
        return dm, False


def loader_nonlock(memblock, oldhash):
    hashval = hash(bytes(memblock.buf))
    if hashval == oldhash:
        return None, hashval
    return dill.loads(memblock.buf), hashval


@lock
def loader(memblock, oldhash):
    hashval = hash(bytes(memblock.buf))
    if hashval == oldhash:
        return None, hashval
    return dill.loads(memblock.buf), hashval


def update_nonlock(memblock, it):
    try:
        if str(it.__class__) == "<class 'dict_items'>":
            asdill = dill.dumps(it, recurse=True, protocol=cfg.protocol)
        else:
            asdill = pickle.dumps(it, protocol=cfg.protocol)
    except Exception as e:
        asdill = dill.dumps(it, recurse=True, protocol=cfg.protocol)
    memblock.buf[: len(asdill)] = asdill
    return hash(asdill)


@lock
def update(memblock, it):
    try:
        if str(it.__class__) == "<class 'dict_items'>":
            asdill = dill.dumps(it, recurse=True, protocol=cfg.protocol)
        else:
            asdill = pickle.dumps(it, protocol=cfg.protocol)
    except Exception as e:
        asdill = dill.dumps(it, recurse=True, protocol=cfg.protocol)
    memblock.buf[: len(asdill)] = asdill
    return hash(asdill)


class MemSharedDict(dict):
    r"""
    MemSharedDict: A shared memory dictionary with locking support.

    This class extends the functionality of the built-in Python dictionary
    by allowing shared access to its data across multiple processes.
    It is designed to be used in scenarios where multiple processes need to read and update a
    common dictionary, and a locking mechanism is provided to ensure data consistency.

    Initialization:
        MemSharedDict(initialdata=None, /, name=None, size=1024 * 1000, **kwargs)

    Parameters:
        - initialdata (optional): Initial data to populate the shared dictionary.
        - name (optional): A unique name for identifying the shared memory block.
        - size (optional): Size of the shared memory block in bytes.
        - **kwargs: Additional keyword arguments supported by the underlying Python dictionary.

    Attributes:
        - _memsize: Size of the shared memory block.
        - _memname: Name of the shared memory block.
        - _memshared: Shared memory block instance.
        - _mem_exists: Boolean indicating whether the shared memory block already exists.
        - _memhashold: Hash value representing the state of the shared memory block.

    Methods:
        - _load_mem(func): Decorator for loading data from the shared memory block before executing a method.
        - _update_mem(func): Decorator for updating data in the shared memory block after executing a method.
        - _memloader(): Loads data from the shared memory block into the dictionary.
        - _memupdater(): Updates data in the shared memory block based on the current state of the dictionary.
        - cleanup(): close the shared memory

    Inherited Methods from dict:
        - clear, copy, get, items, keys, pop, popitem, setdefault, update, values, etc.

    Note: This class utilizes multiprocessing.shared_memory.SharedMemory
    for shared memory handling and pickle/dill for serialization.
    """

    def __init__(self, initialdata=None, /, name=None, size=1024 * 1000, **kwargs):
        self._memsize = size
        self._memname = name if name is not None else str(time.time())
        self._memshared, self._mem_exists = get_or_create_memory_block(
            self._memname, self._memsize, newval={}.items()
        )
        self._memhashold = 0
        if initialdata:
            super().__init__(initialdata, **kwargs)
        if self._mem_exists:
            self._memloader()
        else:
            self._memhashold = update_nonlock(
                memblock=self._memshared, it=initialdata.items() if initialdata else {}
            )

    def _load_mem(func):
        def wrapper(self, *arg, **kw):
            self._memloader()
            res = func(self, *arg, **kw)
            return res

        return wrapper

    def to_dict(self):
        return {k: v for k, v in self.items()}

    def _update_mem(func):
        def wrapper(self, *arg, **kw):
            res = func(self, *arg, **kw)
            self._memupdater()
            return res

        return wrapper

    def _memloader(self):
        tmp, self._memhashold = loader(self._memshared, self._memhashold)
        if tmp is not None:
            super().clear()
            super().update(tmp)

    def _memupdater(self):
        try:
            self._memhashold = update(memblock=self._memshared, it=self.items())
        except Exception as e:
            sys.stderr.write(f"Failed to activate lock - trying without it\n")
            sys.stderr.flush()
            self._memhashold = update_nonlock(memblock=self._memshared, it=self.items())

    @_load_mem
    def __class_getitem__(self, *args, **kwargs):
        return super().__class_getitem__(*args, **kwargs)

    @_load_mem
    def __contains__(self, *args, **kwargs):
        return super().__contains__(*args, **kwargs)

    def cleanup(self):
        self._memshared.close()
        if not self._mem_exists:
            self._memshared.unlink()
            gc.collect()
        # return super().__del__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __delitem__(self, *args, **kwargs):
        return super().__delitem__(*args, **kwargs)

    @_load_mem
    def __eq__(self, *args, **kwargs):
        return super().__eq__(*args, **kwargs)

    @_load_mem
    def __format__(self, *args, **kwargs):
        return super().__format__(*args, **kwargs)

    @_load_mem
    def __ge__(self, *args, **kwargs):
        return super().__ge__(*args, **kwargs)

    @_load_mem
    def __getitem__(self, *args, **kwargs):
        return super().__getitem__(*args, **kwargs)

    @_load_mem
    def __getstate__(self, *args, **kwargs):
        return super().__getstate__(*args, **kwargs)

    @_load_mem
    def __gt__(self, *args, **kwargs):
        return super().__gt__(*args, **kwargs)

    @_load_mem
    def __hash__(self, *args, **kwargs):
        return super().__hash__(*args, **kwargs)

    @_load_mem
    def __init_subclass__(self, *args, **kwargs):
        return super().__init_subclass__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __ior__(self, *args, **kwargs):
        return super().__ior__(*args, **kwargs)

    @_load_mem
    def __iter__(self, *args, **kwargs):
        return super().__iter__(*args, **kwargs)

    @_load_mem
    def __le__(self, *args, **kwargs):
        return super().__le__(*args, **kwargs)

    @_load_mem
    def __len__(self, *args, **kwargs):
        return super().__len__(*args, **kwargs)

    @_load_mem
    def __lt__(self, *args, **kwargs):
        return super().__lt__(*args, **kwargs)

    @_load_mem
    def __ne__(self, *args, **kwargs):
        return super().__ne__(*args, **kwargs)

    @_load_mem
    def __or__(self, *args, **kwargs):
        return super().__or__(*args, **kwargs)

    @_load_mem
    def __reduce__(self, *args, **kwargs):
        return super().__reduce__(*args, **kwargs)

    @_load_mem
    def __reduce_ex__(self, *args, **kwargs):
        return super().__reduce_ex__(*args, **kwargs)

    @_load_mem
    def __repr__(self, *args, **kwargs):
        return super().__repr__(*args, **kwargs)

    @_load_mem
    def __reversed__(self, *args, **kwargs):
        return super().__reversed__(*args, **kwargs)

    @_load_mem
    def __ror__(self, *args, **kwargs):
        return super().__ror__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __setitem__(self, *args, **kwargs):
        return super().__setitem__(*args, **kwargs)

    @_load_mem
    def __sizeof__(self, *args, **kwargs):
        return super().__sizeof__(*args, **kwargs)

    @_load_mem
    def __str__(self, *args, **kwargs):
        return super().__str__(*args, **kwargs)

    @_load_mem
    def __subclasshook__(self, *args, **kwargs):
        return super().__subclasshook__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def clear(self, *args, **kwargs):
        return super().clear(*args, **kwargs)

    @_load_mem
    def copy(self, *args, **kwargs):
        return super().copy(*args, **kwargs)

    @_load_mem
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)

    @_load_mem
    def items(self, *args, **kwargs):
        return super().items(*args, **kwargs)

    @_load_mem
    def keys(self, *args, **kwargs):
        return super().keys(*args, **kwargs)

    @_load_mem
    @_update_mem
    def pop(self, *args, **kwargs):
        return super().pop(*args, **kwargs)

    @_load_mem
    @_update_mem
    def popitem(self, *args, **kwargs):
        return super().popitem(*args, **kwargs)

    @_load_mem
    @_update_mem
    def setdefault(self, *args, **kwargs):
        return super().setdefault(*args, **kwargs)

    @_load_mem
    @_update_mem
    def update(self, *args, **kwargs):
        return super().update(*args, **kwargs)

    @_load_mem
    def values(self, *args, **kwargs):
        return super().values(*args, **kwargs)


class MemSharedList(list):
    r"""
    MemSharedList: A shared memory list with locking support.

    This class extends the functionality of the built-in Python list by allowing
    shared access to its data across multiple processes.
    It is designed to be used in scenarios where multiple processes need to read and update a common list,
    and a locking mechanism is provided to ensure data consistency.

    Initialization:
        MemSharedList(initialdata=None, name=None, size=1024 * 1000, **kwargs)

    Parameters:
        - initialdata (optional): Initial data to populate the shared list.
        - name (optional): A unique name for identifying the shared memory block.
        - size (optional): Size of the shared memory block in bytes.
        - **kwargs: Additional keyword arguments supported by the underlying Python list.

    Attributes:
        - _memsize: Size of the shared memory block.
        - _memname: Name of the shared memory block.
        - _memshared: Shared memory block instance.
        - _mem_exists: Boolean indicating whether the shared memory block already exists.
        - _memhashold: Hash value representing the state of the shared memory block.

    Methods:
        - _load_mem(func): Decorator for loading data from the shared memory block before executing a method.
        - _update_mem(func): Decorator for updating data in the shared memory block after executing a method.
        - _memloader(): Loads data from the shared memory block into the list.
        - _memupdater(): Updates data in the shared memory block based on the current state of the list.
        - cleanup(): close the shared memory

    Inherited Methods from list:
        - append, clear, copy, count, extend, index, insert, pop, remove, reverse, sort, etc.

    Note: This class utilizes multiprocessing.shared_memory.SharedMemory for shared memory
    handling and pickle/dill for serialization."""

    def __init__(self, initialdata=None, /, name=None, size=1024 * 1000, **kwargs):
        self._memsize = size
        self._memname = name if name is not None else str(time.time())
        self._memshared, self._mem_exists = get_or_create_memory_block(
            self._memname, self._memsize, newval=[]
        )
        self._memhashold = 0
        if initialdata:
            super().__init__(initialdata, **kwargs)
        if self._mem_exists:
            self._memloader()
        else:
            self._memhashold = update_nonlock(
                memblock=self._memshared,
                it=[x for x in super().__iter__()] if initialdata else [],
            )

    def to_list(self):
        return [k for k in self.__iter__()]

    def _load_mem(func):
        def wrapper(self, *arg, **kw):
            self._memloader()
            res = func(self, *arg, **kw)
            return res

        return wrapper

    def _update_mem(func):
        def wrapper(self, *arg, **kw):
            res = func(self, *arg, **kw)
            self._memupdater()
            return res

        return wrapper

    def _memloader(self):
        tmp, self._memhashold = loader(self._memshared, self._memhashold)
        if tmp is not None:
            super().clear()
            _ = [self.append(x) for x in tmp]

    def _memupdater(self):
        try:
            self._memhashold = update(
                memblock=self._memshared, it=[x for x in super().__iter__()]
            )
        except Exception as e:
            sys.stderr.write(f"Failed to activate lock - trying without it\n")
            sys.stderr.flush()
            self._memhashold = update_nonlock(
                memblock=self._memshared, it=[x for x in super().__iter__()]
            )

    @_load_mem
    def __add__(self, *args, **kwargs):
        return super().__add__(*args, **kwargs)

    @_load_mem
    def __class_getitem__(self, *args, **kwargs):
        return super().__class_getitem__(*args, **kwargs)

    @_load_mem
    def __contains__(self, *args, **kwargs):
        return super().__contains__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __delitem__(self, *args, **kwargs):
        return super().__delitem__(*args, **kwargs)

    def cleanup(
        self,
    ):
        self._memshared.close()
        if not self._mem_exists:
            self._memshared.unlink()
            gc.collect()

    @_load_mem
    def __eq__(self, *args, **kwargs):
        return super().__eq__(*args, **kwargs)

    @_load_mem
    def __format__(self, *args, **kwargs):
        return super().__format__(*args, **kwargs)

    @_load_mem
    def __ge__(self, *args, **kwargs):
        return super().__ge__(*args, **kwargs)

    @_load_mem
    def __getitem__(self, *args, **kwargs):
        return super().__getitem__(*args, **kwargs)

    @_load_mem
    def __getstate__(self, *args, **kwargs):
        return super().__getstate__(*args, **kwargs)

    @_load_mem
    def __gt__(self, *args, **kwargs):
        return super().__gt__(*args, **kwargs)

    @_load_mem
    def __hash__(self, *args, **kwargs):
        return super().__hash__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __iadd__(self, *args, **kwargs):
        return super().__iadd__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __imul__(self, *args, **kwargs):
        return super().__imul__(*args, **kwargs)

    @_load_mem
    def __init_subclass__(self, *args, **kwargs):
        return super().__init_subclass__(*args, **kwargs)

    @_load_mem
    def __iter__(self, *args, **kwargs):
        return super().__iter__(*args, **kwargs)

    @_load_mem
    def __le__(self, *args, **kwargs):
        return super().__le__(*args, **kwargs)

    @_load_mem
    def __len__(self, *args, **kwargs):
        return super().__len__(*args, **kwargs)

    @_load_mem
    def __lt__(self, *args, **kwargs):
        return super().__lt__(*args, **kwargs)

    @_load_mem
    def __mul__(self, *args, **kwargs):
        return super().__mul__(*args, **kwargs)

    @_load_mem
    def __ne__(self, *args, **kwargs):
        return super().__ne__(*args, **kwargs)

    @_load_mem
    def __reduce__(self, *args, **kwargs):
        return super().__reduce__(*args, **kwargs)

    @_load_mem
    def __reduce_ex__(self, *args, **kwargs):
        return super().__reduce_ex__(*args, **kwargs)

    @_load_mem
    def __repr__(self, *args, **kwargs):
        return super().__repr__(*args, **kwargs)

    @_load_mem
    def __reversed__(self, *args, **kwargs):
        return super().__reversed__(*args, **kwargs)

    @_load_mem
    def __rmul__(self, *args, **kwargs):
        return super().__rmul__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __setitem__(self, *args, **kwargs):
        return super().__setitem__(*args, **kwargs)

    @_load_mem
    def __sizeof__(self, *args, **kwargs):
        return super().__sizeof__(*args, **kwargs)

    @_load_mem
    def __str__(self, *args, **kwargs):
        return super().__str__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def append(self, *args, **kwargs):
        return super().append(*args, **kwargs)

    @_load_mem
    @_update_mem
    def clear(self, *args, **kwargs):
        return super().clear(*args, **kwargs)

    @_load_mem
    def copy(self, *args, **kwargs):
        return super().copy(*args, **kwargs)

    @_load_mem
    def count(self, *args, **kwargs):
        return super().count(*args, **kwargs)

    @_load_mem
    @_update_mem
    def extend(self, *args, **kwargs):
        return super().extend(*args, **kwargs)

    @_load_mem
    def index(self, *args, **kwargs):
        return super().index(*args, **kwargs)

    @_load_mem
    @_update_mem
    def insert(self, *args, **kwargs):
        return super().insert(*args, **kwargs)

    @_load_mem
    @_update_mem
    def pop(self, *args, **kwargs):
        return super().pop(*args, **kwargs)

    @_load_mem
    @_update_mem
    def remove(self, *args, **kwargs):
        return super().remove(*args, **kwargs)

    @_load_mem
    @_update_mem
    def reverse(self, *args, **kwargs):
        return super().reverse(*args, **kwargs)

    @_load_mem
    @_update_mem
    def sort(self, *args, **kwargs):
        return super().sort(*args, **kwargs)


class MemSharedSet(set):
    r"""
    MemSharedSet: A shared memory set with locking support.

    This class extends the functionality of the built-in Python set by allowing shared access to
    its data across multiple processes. It is designed to be used in scenarios where multiple
    processes need to read and update a common set, and a locking mechanism is provided to ensure data consistency.

    Initialization:
        MemSharedSet(initialdata=None, name=None, size=1024 * 1000, **kwargs)

    Parameters:
        - initialdata (optional): Initial data to populate the shared set.
        - name (optional): A unique name for identifying the shared memory block.
        - size (optional): Size of the shared memory block in bytes.
        - **kwargs: Additional keyword arguments supported by the underlying Python set.

    Attributes:
        - _memsize: Size of the shared memory block.
        - _memname: Name of the shared memory block.
        - _memshared: Shared memory block instance.
        - _mem_exists: Boolean indicating whether the shared memory block already exists.
        - _memhashold: Hash value representing the state of the shared memory block.

    Methods:
        - _load_mem(func): Decorator for loading data from the shared memory block before executing a method.
        - _update_mem(func): Decorator for updating data in the shared memory block after executing a method.
        - _memloader(): Loads data from the shared memory block into the set.
        - _memupdater(): Updates data in the shared memory block based on the current state of the set.
        - cleanup(): close the shared memory
    Inherited Methods from set:
        - add, clear, copy, difference, difference_update, discard, intersection, intersection_update, isdisjoint, issubset, issuperset, pop, remove, symmetric_difference, symmetric_difference_update, union, update, etc.

    Note: This class utilizes multiprocessing.shared_memory.SharedMemory for shared memory handling
    and pickle/dill for serialization."""

    def __init__(self, initialdata=None, /, name=None, size=1024 * 1000, **kwargs):
        self._memsize = size
        self._memname = name if name is not None else str(time.time())
        self._memshared, self._mem_exists = get_or_create_memory_block(
            self._memname, self._memsize, newval=set()
        )
        self._memhashold = 0
        if initialdata:
            super().__init__(initialdata, **kwargs)
        if self._mem_exists:
            self._memloader()
        else:
            self._memhashold = update_nonlock(
                memblock=self._memshared,
                it={x for x in super().__iter__()} if initialdata else set(),
            )

    def to_set(self):
        return {k for k in self.__iter__()}

    def _load_mem(func):
        def wrapper(self, *arg, **kw):
            self._memloader()
            res = func(self, *arg, **kw)
            return res

        return wrapper

    def _update_mem(func):
        def wrapper(self, *arg, **kw):
            res = func(self, *arg, **kw)
            self._memupdater()
            return res

        return wrapper

    def _memloader(self):
        tmp, self._memhashold = loader(self._memshared, self._memhashold)
        if tmp is not None:
            super().clear()
            self.update(tmp)

    def _memupdater(self):
        try:
            self._memhashold = update(
                memblock=self._memshared, it={x for x in super().__iter__()}
            )
        except Exception as e:
            sys.stderr.write(f"Failed to activate lock - trying without it\n")
            sys.stderr.flush()
            self._memhashold = update_nonlock(
                memblock=self._memshared, it={x for x in super().__iter__()}
            )

    @_load_mem
    def __and__(self, *args, **kwargs):
        return super().__and__(*args, **kwargs)

    @_load_mem
    def __class_getitem__(self, *args, **kwargs):
        return super().__class_getitem__(*args, **kwargs)

    @_load_mem
    def __contains__(self, *args, **kwargs):
        return super().__contains__(*args, **kwargs)

    def cleanup(self):
        self._memshared.close()
        if not self._mem_exists:
            self._memshared.unlink()
            gc.collect()

    @_load_mem
    def __eq__(self, *args, **kwargs):
        return super().__eq__(*args, **kwargs)

    @_load_mem
    def __format__(self, *args, **kwargs):
        return super().__format__(*args, **kwargs)

    @_load_mem
    def __ge__(self, *args, **kwargs):
        return super().__ge__(*args, **kwargs)

    @_load_mem
    def __gt__(self, *args, **kwargs):
        return super().__gt__(*args, **kwargs)

    @_load_mem
    def __hash__(self, *args, **kwargs):
        return super().__hash__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __iand__(self, *args, **kwargs):
        return super().__iand__(*args, **kwargs)

    @_load_mem
    def __init_subclass__(self, *args, **kwargs):
        return super().__init_subclass__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __ior__(self, *args, **kwargs):
        return super().__ior__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __isub__(self, *args, **kwargs):
        return super().__isub__(*args, **kwargs)

    @_load_mem
    def __iter__(self, *args, **kwargs):
        return super().__iter__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def __ixor__(self, *args, **kwargs):
        return super().__ixor__(*args, **kwargs)

    @_load_mem
    def __le__(self, *args, **kwargs):
        return super().__le__(*args, **kwargs)

    @_load_mem
    def __len__(self, *args, **kwargs):
        return super().__len__(*args, **kwargs)

    @_load_mem
    def __lt__(self, *args, **kwargs):
        return super().__lt__(*args, **kwargs)

    @_load_mem
    def __ne__(self, *args, **kwargs):
        return super().__ne__(*args, **kwargs)

    @_load_mem
    def __or__(self, *args, **kwargs):
        return super().__or__(*args, **kwargs)

    @_load_mem
    def __rand__(self, *args, **kwargs):
        return super().__rand__(*args, **kwargs)

    @_load_mem
    def __reduce__(self, *args, **kwargs):
        return super().__reduce__(*args, **kwargs)

    @_load_mem
    def __reduce_ex__(self, *args, **kwargs):
        return super().__reduce_ex__(*args, **kwargs)

    @_load_mem
    def __repr__(self, *args, **kwargs):
        return super().__repr__(*args, **kwargs)

    @_load_mem
    def __ror__(self, *args, **kwargs):
        return super().__ror__(*args, **kwargs)

    @_load_mem
    def __rsub__(self, *args, **kwargs):
        return super().__rsub__(*args, **kwargs)

    @_load_mem
    def __rxor__(self, *args, **kwargs):
        return super().__rxor__(*args, **kwargs)

    @_load_mem
    def __sizeof__(self, *args, **kwargs):
        return super().__sizeof__(*args, **kwargs)

    @_load_mem
    def __str__(self, *args, **kwargs):
        return super().__str__(*args, **kwargs)

    @_load_mem
    def __sub__(self, *args, **kwargs):
        return super().__sub__(*args, **kwargs)

    @_load_mem
    def __xor__(self, *args, **kwargs):
        return super().__xor__(*args, **kwargs)

    @_load_mem
    @_update_mem
    def add(self, *args, **kwargs):
        return super().add(*args, **kwargs)

    @_load_mem
    @_update_mem
    def clear(self, *args, **kwargs):
        return super().clear(*args, **kwargs)

    @_load_mem
    def copy(self, *args, **kwargs):
        return super().copy(*args, **kwargs)

    @_load_mem
    def difference(self, *args, **kwargs):
        return super().difference(*args, **kwargs)

    @_load_mem
    @_update_mem
    def difference_update(self, *args, **kwargs):
        return super().difference_update(*args, **kwargs)

    @_load_mem
    @_update_mem
    def discard(self, *args, **kwargs):
        return super().discard(*args, **kwargs)

    @_load_mem
    def intersection(self, *args, **kwargs):
        return super().intersection(*args, **kwargs)

    @_load_mem
    @_update_mem
    def intersection_update(self, *args, **kwargs):
        return super().intersection_update(*args, **kwargs)

    @_load_mem
    def isdisjoint(self, *args, **kwargs):
        return super().isdisjoint(*args, **kwargs)

    @_load_mem
    def issubset(self, *args, **kwargs):
        return super().issubset(*args, **kwargs)

    @_load_mem
    def issuperset(self, *args, **kwargs):
        return super().issuperset(*args, **kwargs)

    @_load_mem
    @_update_mem
    def pop(self, *args, **kwargs):
        return super().pop(*args, **kwargs)

    @_load_mem
    @_update_mem
    def remove(self, *args, **kwargs):
        return super().remove(*args, **kwargs)

    @_load_mem
    def symmetric_difference(self, *args, **kwargs):
        return super().symmetric_difference(*args, **kwargs)

    @_load_mem
    @_update_mem
    def symmetric_difference_update(self, *args, **kwargs):
        return super().symmetric_difference_update(*args, **kwargs)

    @_load_mem
    def union(self, *args, **kwargs):
        return super().union(*args, **kwargs)

    @_load_mem
    @_update_mem
    def update(self, *args, **kwargs):
        return super().update(*args, **kwargs)
