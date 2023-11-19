# Shared list / set / dict across processes / environments

## pip install sharedbuiltinmutables

### Tested against Windows 10 / Python 3.11 / Anaconda 


## FILE 1 

```python
from sharedbuiltinmutables import MemSharedDict, MemSharedList, MemSharedSet, cfg

# dill/pickle protocol
cfg.protocol = 5

d = MemSharedDict({3: 323}, name="d1", size=1024)
l = MemSharedList([3, 323], name="l1", size=1024)
s = MemSharedSet({3, 6, 5}, name="s1", size=1024)

d[111] = 444
d.pop(3)
d[9] = lambda h: h * 3
# to clean up: d.cleanup()

```

## FILE 2 

```python
from sharedbuiltinmutables import MemSharedDict, MemSharedList, MemSharedSet, cfg

# dill/pickle protocol
cfg.protocol = 5

d = MemSharedDict(name="d1", size=1024)
# passing a value ( d = MemSharedDict({33:11,3:3} name="d1", size=1024) )
# won't do anything if
# the dict has already been created by another proc
#
# use instead:
# d = MemSharedDict(name="d1", size=1024)
# d.clear()
# d.update({33:11,3:3})
l = MemSharedList(name="l1", size=1024)
s = MemSharedSet(name="s1", size=1024)
# to clean up: d.cleanup()

```