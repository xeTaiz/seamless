from copy import deepcopy
import json
import hashlib
from io import BytesIO
import numpy as np
import pickle
import ast
from ast import PyCF_ONLY_AST, FunctionDef
import inspect

from .macro_mode import with_macro_mode
from .protocol import modes, submodes, celltypes
from .. import Wrapper
from . import SeamlessBase
from ..mixed import io as mixed_io
from .cached_compile import cached_compile
from . import macro_register, get_macro_mode
from .mount import MountItem
from .utils import strip_source

cell_counter = 0

class CellLikeBase(SeamlessBase):
    def __init__(self):
        global cell_counter
        super().__init__()
        cell_counter += 1
        self._counter = cell_counter
        if get_macro_mode():
            macro_register.add(self)

    def __hash__(self):
        return self._counter


class CellBase(CellLikeBase):
    _has_text_checksum = False
    _exception = None
    _val = None
    _last_checksum = None
    _last_text_checksum = None
    _naming_pattern = "cell"
    _prelim_val = None
    _authoritative = True
    _overruled = False #a non-authoritative cell that has previously received a value
    _mount = None
    _mount_kwargs = None
    _mount_setter = None
    _slave = False   #Slave cells. Cannot be written to by API, do not accept connections,
                     #  and mounting is write-only unless there is a mount_setter.
                     # Slave cells are controlled by StructuredCell.
                     # TODO: make StructuredCell a bit less tyrannical, and allow slave
                     #  cells to be controlled by workers/macros external to the StructuredCell
                     # This will require a listener in the vein of mount_setter

    def status(self):
        """The cell's current status."""
        return self._status.name

    @property
    def authoritative(self):
        return self._authoritative

    @property
    def _value(self):
        return self._val

    @property
    def _seal(self):
        ctx = self._context()
        assert ctx is not None
        return ctx._seal

    @_value.setter
    def _value(self, value):
        """Should only ever be set by the manager, since it bypasses validation, last checksum, status flags, authority, etc."""
        self._value = value

    @property
    def value(self):
        return self._val

    def _check_mode(self, mode, submode=None):
        #TODO: obsolete!
        assert mode in modes, mode
        if submode is not None:
            assert submode in submodes, (submode, submodes)

    def touch(self):
        """Forces a cell update, even though the value stays the same
        This triggers all workers that are connected to the cell"""
        manager = self._get_manager()
        manager.touch_cell(self)
        return self

    def set(self, value):
        """Update cell data from Python code in the main thread."""
        assert not self._slave #slave cells are read-only
        if isinstance(value, Wrapper):
            value = value._unwrap()
        if self._context is None:
            self._prelim_val = value, False #non-default-value prelim
        else:
            manager = self._get_manager()
            manager.set_cell(self, value)
        return self

    def set_default(self, value):
        """Provides a default value for the cell
        This value can be overwritten by workers"""
        if self._context is None:
            self._prelim_val = value, True #default-value prelim
        else:
            manager = self._get_manager()
            manager.set_cell(self, value, default=True)
        return self

    def from_buffer(self, value, checksum=None):
        """Sets a cell from a buffer value"""
        if self._context is None:
            self._prelim_val = value, False #non-default-value prelim
        else:
            manager = self._get_manager()
            manager.set_cell(self, value, from_buffer=True, force=True)
        return self

    def from_file(self, filepath):
        ok = False
        if self._mount_kwargs is not None:
            if "binary" in self._mount_kwargs:
                binary = self._mount_kwargs["binary"]
                if not binary:
                    if "encoding" in self._mount_kwargs:
                        encoding = self._mount_kwargs["encoding"]
                        ok = True
                else:
                    ok = True
        if not ok:
            raise TypeError("Cell %s cannot be loaded from file" % self)
        filemode = "rb" if binary else "r"
        with open(filepath, filemode, encoding=encoding) as f:
            filevalue = f.read()
        self.from_buffer(filevalue)

    def serialize(self, mode, submode=None):
        self._check_mode(mode, submode)
        #assert self.status() == "OK", self.status() #why?
        checksum = self.checksum() #TODO: determine which checksum we need
        return self._serialize(mode, submode), checksum

    def _reset_checksums(self):
        self._last_checksum = None
        self._alternative_checksums = None

    def _assign(self, value):
        assert value is not None
        v = self._val
        if not issubclass(type(value), type(v)):
            self._val = value
            return value
        if isinstance(v, dict):
            v.clear()
            v.update(value)
        elif isinstance(v, list):
            v[:] = value #not for ndarray, since they must have the same shape
        else:
            self._val = value
        return value


    def deserialize(self, value, mode, submode=None, *, from_pin, default, force=False):
        """Should normally be invoked by the manager, since it does not notify the manager
        from_pin: can be True (normal pin that has authority), False (from code) or "edit" (edit pin)
        default: indicates a default value (pins may overwrite it)
        force: force deserialization, even if slave (normally, force is invoked only by structured_cell)
        """
        assert from_pin in (True, False, "edit")
        if not force:
            assert not self._slave
        self._check_mode(mode, submode)
        old_status = self._status
        if value is None:
            different = (self._last_checksum is not None)
            text_different = (self._last_text_checksum is not None)
            self._val = None
            self._reset_checksums()
            self._status = self.StatusFlags.UNDEFINED
            return different, text_different
        old_checksum = None
        old_text_checksum = None
        if value is not None:
            if old_status == self.StatusFlags.OK:
                if self.value is not None:
                    old_checksum = self.checksum()
                    old_text_checksum = self.text_checksum()
        self._reset_checksums()
        curr_val = self._val
        try:
            parsed_value = self._deserialize(value, mode, submode)
            self._validate(parsed_value)
        except:
            self._val = curr_val
            raise
        self._status = self.StatusFlags.OK
        if old_checksum is None: #old checksum failed
            different = True
            text_different =True
        elif value is not None:
            different = (self.checksum(may_fail=True) != old_checksum)
            text_different = (self.text_checksum(may_fail=True) != old_text_checksum)
        else:
            pass #"different" has already been set

        if from_pin == True:
            assert not self._authoritative, self
            self._un_overrule(different)
        elif from_pin == "edit":
            if not self._authoritative:
                if different:
                    self._overrule()
            else:
                self._un_overrule(different)
        elif from_pin == False:
            if different and not default and not self._authoritative:
                self._overrule()
            if different and self._seal is not None:
                msg = "Warning: setting value for cell %s, controlled by %s"
                print(msg % (self._format_path(), self._seal) )

        return different, text_different

    def _overrule(self):
        if not self._overruled:
            print("Warning: overruling (setting value for non-source cell) %s" % self._format_path())
            self._overruled = True

    def _un_overrule(self, different):
        if self._overruled:
            if different:
                print("Warning: cell %s was formerly overruled, now updated by dependency" % self._format_path())
            self._overruled = False

    @property
    def exception(self):
        """The cell's current exception, as returned by sys.exc_info

        Returns None is there is no exception
        """
        return self._exception

    def _set_exception(self, exception):
        """Should normally be invoked by the manager, since it does not notify the manager"""
        if exception is not None:
            self._status = self.StatusFlags.ERROR
            tb, exc, value = exception #to validate
        self._exception = exception

    def _validate(self, value):
        """Won't raise an exception, but may set .exception (TODO: check???)"""
        raise NotImplementedError

    def _serialize(self, mode, submode=None):
        raise NotImplementedError

    def _deserialize(self, value, mode, submode=None):
        raise NotImplementedError

    def _checksum(self, value, *, buffer=False, may_fail=False):
        raise NotImplementedError

    def _text_checksum(self, value, *, buffer=False, may_fail=False):
        return self._checksum(value, buffer=buffer, may_fail=may_fail)

    def checksum(self, *, may_fail=False):
        if self.status() != "OK":
            return None
        assert self._val is not None
        if self._last_checksum is not None:
            return self._last_checksum
        result = self._checksum(self._val, may_fail=may_fail)
        self._last_checksum = result
        return result

    def text_checksum(self, *, may_fail=False):
        if not self._has_text_checksum:
            return self.checksum(may_fail=may_fail)
        if self.status() != "OK":
            return None
        assert self._val is not None
        if self._last_text_checksum is not None:
            return self._last_text_checksum
        result = self._text_checksum(self._val, may_fail=may_fail)
        self._last_text_checksum = result
        return result

    @with_macro_mode
    def connect(self, target):
        """connects to a target cell"""
        manager = self._get_manager()
        manager.connect_cell(self, target)
        return self

    def as_text(self):
        raise NotImplementedError

    def mount(self, path=None, mode="rw", authority="cell", persistent=False):
        """Performs a "lazy mount"; cell is mounted to the file when macro mode ends
        path: file path (can be None if an ancestor context has been mounted)
        mode: "r", "w" or "rw"
        authority: "cell", "file" or "file-strict"
        persistent: whether or not the file persists after the context has been destroyed
        """
        assert self._mount is None #Only the mountmanager may modify this further!
        if self._root()._auto_macro_mode:
            raise Exception("Root context must have been constructed in macro mode")
        if self._mount_kwargs is None:
            raise NotImplementedError #cannot mount this type of cell
        kwargs = self._mount_kwargs
        self._mount = {
            "path": path,
            "mode": mode,
            "authority": authority,
            "persistent": persistent
        }
        self._mount.update(self._mount_kwargs)
        MountItem(None, self, dummy=True, **self._mount) #to validate parameters


class Cell(CellBase):
    """Default class for cells.

Cells contain all the state in text form

Cells can be connected to inputpins, editpins, and other cells.
``cell.connect(pin)`` connects a cell to an inputpin or editpin

Output pins and edit pins can be connected to cells.
``pin.connect(cell)`` connects an outputpin or editpin to a cell

Use ``Cell.value`` to get its value.

Use ``Cell.status()`` to get its status.
"""
    _supported_modes = (
        ("ref", None, None),
        ("copy", None, None),
    )

    def _checksum(self, value, *, buffer=False, may_fail=False):
        return hashlib.md5(str(value).encode("utf-8")).hexdigest()

    def _validate(self, value):
        pass

    def _serialize(self, mode, submode=None):
        if mode == "buffer":
            raise Exception("Cell '%s' cannot be serialized as buffer, use TextCell or JsonCell instead" % self._format_path())
        elif mode == "copy":
            assert submode is None, (mode, submode)
            return deepcopy(self._val)
        else:
            return self._val

    def _deserialize(self, value, mode, submode=None):
        if mode == "buffer":
            raise Exception("Cell '%s' cannot be de-serialized as buffer, use TextCell or JsonCell instead" % self._format_path())
        assert submode is None, (mode, submode)
        return self._assign(value)

    def __str__(self):
        ret = "Seamless cell: " + self._format_path()
        return ret

    def as_text(self):
        if self._val is None:
            return None
        try:
            return str(self._val)
        except:
            return "<Cannot be rendered as text>"

class ArrayCell(Cell):
    """A cell in binary array (Numpy) format"""
    #also provides copy+silk and ref+silk transport, but with an empty schema, and text form

    _mount_kwargs = {"binary": True}

    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "silk", None:
            for celltype in "binary", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    def _checksum(self, value, *, buffer=False, may_fail=False):
        if buffer:
            return super()._checksum(value)
        assert isinstance(value, np.ndarray)
        b = self._value_to_bytes(value)
        return super()._checksum(b, buffer=True)

    def _value_to_bytes(self, value):
        b = BytesIO()
        np.save(b, value, allow_pickle=False)
        return b.getvalue()

    def _validate(self, value):
        assert isinstance(value, np.ndarray)

    def _serialize(self, mode, submode=None):
        #TODO: proper checks
        if mode == "buffer":
            return self._value_to_bytes(self._val)
        elif mode == "copy":
            if submode == "silk":
                data = deepcopy(self._val)
                return Silk(data=data)
            else:
                return deepcopy(self._val)
        elif mode == "ref":
            assert submode in ("json", "silk", None)
            if submode == "silk":
                return Silk(data=self._val)
            else:
                return self._val
        else:
            return self._val

    def _from_buffer(self, value):
        if value is None:
            return None
        b = BytesIO(value)
        return np.load(b)

    def _deserialize(self, value, mode, submode=None):
        if mode == "buffer":
            return self._assign(self._from_buffer(value))
        else:
            return self._assign(value)

    def __str__(self):
        ret = "Seamless array cell: " + self._format_path()
        return ret

class MixedCell(Cell):
    _mount_kwargs = {"binary": True}
    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "silk", None:
            for celltype in "mixed", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    def __init__(self, storage_cell, form_cell):
        super().__init__()
        self.storage_cell = storage_cell
        self.form_cell = form_cell

    def _from_buffer(self, value):
        if value is None:
            return None
        storage = self.storage_cell.value
        form = self.form_cell.value
        return mixed_io.from_stream(value, storage, form)

    def _value_to_bytes(self, value, storage, form):
        if value is None:
            return None
        return mixed_io.to_stream(value, storage, form)

    def _to_bytes(self):
        storage = self.storage_cell.value
        form = self.form_cell.value
        return self._value_to_bytes(self._val, storage, form)

    def _checksum(self, value, *, buffer=False, may_fail=False):
        if buffer:
            b = value
        else:
            #assumes that storage and form are correct!
            storage = self.storage_cell.value
            form = self.form_cell.value
            if may_fail:
                try:
                    b = self._value_to_bytes(value, storage, form)
                except:
                    return None
            else:
                b = self._value_to_bytes(value, storage, form)
        return hashlib.md5(b).hexdigest()

    def _validate(self, value):
        return ###TODO: how to validate?? check that value conforms to form?

    def _serialize(self, mode, submode=None):
        if mode == "buffer":
            return self._to_bytes()
        elif mode == "copy":
            if submode == "silk":
                data = deepcopy(self._val)
                return Silk(data=data)
            elif submode == "cson":
                raise NotImplementedError
            else:
                return deepcopy(self._val)
        elif mode == "ref":
            assert submode in ("json", "silk", None)
            if submode == "silk":
                return Silk(data=self._val)
            else:
                return self._val
        else:
            return self._val

    def _deserialize(self, value, mode, submode=None):
        if mode == "buffer":
            return self._assign(self._from_buffer(value))
        else:
            return self._assign(value)

    def __str__(self):
        ret = "Seamless mixed cell: " + self._format_path()
        return ret


class TextCell(Cell):
    _mount_kwargs = {"encoding": "utf-8", "binary": False}
    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "text", None:
            for celltype in "text", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    def _serialize(self, mode, submode=None):
        if mode in ("buffer", "copy"):
            assert submode is None, (mode, submode)
            return deepcopy(self._val)
        else:
            return self._val

    def _deserialize(self, value, mode, submode=None):
        assert submode is None, (mode, submode)
        v = str(value)
        self._val = v
        return v

    def as_text(self):
        if self._val is None:
            return None
        return str(self._val)

    def __str__(self):
        ret = "Seamless text cell: " + self._format_path()
        return ret


class PythonCell(Cell):
    """Python code object, used for reactors and macros"""
    _mount_kwargs = {"encoding": "utf-8", "binary": False}
    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "text", "pythoncode", None:
            for celltype in "python", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    _naming_pattern = "pythoncell"
    _has_text_checksum = True
    _accept_shell_append = True

    #TODO: for serialization, store ._accept_shell_append
    # OR: make ._accept_shell_append editable as cell

    def _check_mode(self, mode, submode=None):
        CellBase._check_mode(self, mode, submode)

    def _text_checksum(self, value, *, buffer=False, may_fail=False):
        return hashlib.md5(str(value).encode("utf-8")).hexdigest()

    def _checksum(self, value, *, buffer=False, may_fail=False):
        tree = ast.parse(value)
        ### TODO: would ast.dump or pickle be more rigorous?
        #dump = ast.dump(tree).encode("utf-8")
        dump = pickle.dumps(tree)
        return hashlib.md5(dump).hexdigest()

    def _shell_append(self, text):
        if not self._accept_shell_append:
            return
        if self._val is None:
            return
        new_value = self._val + "\n" + text
        self.set(new_value)

    def _validate(self, value):
        ast.parse(value)

    def _serialize(self, mode, submode=None):
        if mode == "buffer":
            assert submode is None, (mode, submode)
            return deepcopy(self._val)
        if mode == "copy":
            assert submode in ("text", None)
            return deepcopy(self._val)
        assert mode == "ref" and submode == "pythoncode", (mode, submode)
        ###return self ### BAD: sensitive to destroy! #TODO
        class FakePythonCell:
            pass
        result = FakePythonCell()
        for attr in dir(self):
            if attr.startswith("_"):
                continue
            setattr(result, attr, getattr(self, attr))
        return result

    def _deserialize(self, value, mode, submode=None):
        if inspect.isfunction(value):
            code = inspect.getsource(value)
            code = strip_source(code)
            value = code
        if mode == "ref":
            self._val = value
            return value
        assert mode in ("buffer", "copy"), mode
        assert submode is None, (mode, submode)
        v = str(value)
        self._val = v
        return v

    def __str__(self):
        ret = "Seamless Python cell: " + self._format_path()
        return ret

class PyTransformerCell(PythonCell):
    """Python code object used for transformers or macros
    Each input will be an argument"""

    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "text", "pythoncode", None:
            for celltype in "python", "pytransformer", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    def _validate(self, value):
        if inspect.isfunction(value):
            code = inspect.getsource(value)
            code = strip_source(code)
            value = code
        ast = cached_compile(value, "transformer", "exec", PyCF_ONLY_AST)
        is_function = (len(ast.body) == 1 and
                       isinstance(ast.body[0], FunctionDef))

        if is_function:
            self.func_name = ast.body[0].name
        else:
            self.func_name = "transform"

        self.is_function = is_function

class JsonCell(Cell):
    """A cell in JSON format (monolithic)"""
    _mount_kwargs = {"encoding": "utf-8", "binary": False}

    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        for submode in "json", "silk", "text", None:
            if submode == "silk" and mode == "buffer":
                continue
            for celltype in "json", None:
                _supported_modes.append((mode, submode, celltype))
    _supported_modes = tuple(_supported_modes)
    del mode, submode, celltype

    _naming_pattern = "jsoncell"

    @staticmethod
    def _json(value):
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, indent=2)

    def _to_json(self):
        return self._json(self._val)

    def _checksum(self, value, *, buffer=False, may_fail=False):
        if buffer:
            return super()._checksum(value)
        j = self._json(value)
        return super()._checksum(j)

    def _validate(self, value):
        #TODO: store validation errors
        json.dumps(value)

    def _serialize(self, mode, submode=None):
        if mode == "buffer":
            return self._to_json()
        elif mode == "copy":
            if submode == "silk":
                data = deepcopy(self._val)
                return Silk(data=data)
            elif submode == "cson":
                return self._to_json()
            else:
                return deepcopy(self._val)
        elif mode == "ref":
            assert submode in ("json", "silk", None)
            if submode == "silk":
                return Silk(data=self._val)
            else:
                return self._val
        else:
            return self._val

    def _from_buffer(self, value):
        return json.loads(value)

    def _deserialize(self, value, mode, submode=None):
        if mode == "buffer":
            return self._assign(self._from_buffer(value))
        else:
            return self._assign(value)

    def as_text(self):
        return self._to_json()

    def __str__(self):
        ret = "Seamless JSON cell: " + self._format_path()
        return ret


class CsonCell(JsonCell):
    """A cell in CoffeeScript Object Notation (CSON) format
    When necessary, the contents of a CSON cell are automatically converted
    to JSON.
    """
    _mount_kwargs = {"encoding": "utf-8", "binary": False}

    _supported_modes = []
    for mode in "buffer", "copy", "ref":
        if mode != "buffer":
            _supported_modes.append((mode, "silk", "cson"))
            _supported_modes.append((mode, "silk", None))
        _supported_modes.append((mode, "json", "cson"))
        _supported_modes.append((mode, "text", None))
        _supported_modes.append((mode, "text", "cson"))
        _supported_modes.append((mode, None, None))
        _supported_modes.append((mode, None, "cson"))
    _supported_modes = tuple(_supported_modes)
    del mode

    _naming_pattern = "csoncell"
    _has_text_checksum = True

    def _text_checksum(self, value, *, buffer=False, may_fail=False):
        return hashlib.md5(str(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _json(value):
        if value is None:
            return None
        d = cson2json(value)
        return json.dumps(d, sort_keys=True, indent=2)

    @property
    def value(self):
        return cson2json(self._val)

    def _validate(self, value):
        #TODO: store validation errors
        cson2json(value)

    def _deserialize(self, value, mode, submode=None):
        if value is None:
            result = None
        elif submode in ("json", "silk"):
            if submode == "silk":
                value = value._data
            result = json.dumps(value, sort_keys=True, indent=2)
        else:
            result = str(value)
        self._val = result
        return result

    def _serialize(self, mode, submode=None):
        value = self._val
        if value is None:
            return None
        if submode in ("json", "silk"):
            j = self._json(value)
            if submode == "silk":
                return Silk(data=j)
            else:
                return j
        return str(value)

    def as_text(self):
        if self._val is None:
            return None
        return str(self._val)

    def __str__(self):
        ret = "Seamless CSON cell: " + self._format_path()
        return ret


class Signal(Cell):
    """ A cell that does not contain any data
    When a signal is set, it is propagated as fast as possible:
      - If set from the main thread: immediately. Downstream workers are
         notified and activated (if synchronous) before set() returns
      - If set from another thread: as soon as run_work is called. Then,
         Downstream workers are notified and activated before any other
         non-signal notification.
    """
    _naming_pattern = "signal"

    def _checksum(self, value, *, buffer=False, may_fail=False):
        return None

    def _validate(self, value):
        pass

    def _serialize(self, mode, submode=None):
        raise NotImplementedError

    def _deserialize(self, value, mode, submode=None):
        raise NotImplementedError

    def __str__(self):
        ret = "Seamless signal: " + self._format_path()
        return ret

def cell(celltype=None, **kwargs):
    if celltype == "text":
        return TextCell()
    elif celltype == "python":
        return PythonCell(**kwargs)
    elif celltype == "pytransformer":
        return PyTransformerCell(**kwargs)
    elif celltype == "json":
        return JsonCell(**kwargs)
    elif celltype == "cson":
        return CsonCell(**kwargs)
    elif celltype == "array":
        return ArrayCell(**kwargs)
    elif celltype == "mixed":
        return MixedCell(**kwargs)
    else:
        return Cell(**kwargs)

def textcell():
    return TextCell()

def pythoncell():
    return PythonCell()

def pytransformercell():
    return PyTransformerCell()

def jsoncell():
    return JsonCell()

def csoncell():
    return CsonCell()

def arraycell():
    return ArrayCell()

def mixedcell():
    return MixedCell()

def signal():
    return Signal()

extensions = {
    TextCell: ".txt",
    JsonCell: ".json",
    CsonCell: ".cson",
    PythonCell: ".py",
    MixedCell: ".mixed",
    ArrayCell: ".npy",
}
from ..mixed import MAGIC_SEAMLESS

from ..silk import Silk
if inspect.ismodule(Silk):
    raise ImportError

from .protocol import cson2json

print("TODO cell: PyModule cell") #cell that does imports, executed already upon code definition, as a module; code injection causes an import of this module
#...and TODO: cache cell, event stream
