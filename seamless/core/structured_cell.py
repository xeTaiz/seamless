from .cell import CellLikeBase, Cell, JsonCell, TextCell
from ..mixed import MixedBase, OverlayMonitor, MakeParentMonitor
import weakref
import traceback
from copy import deepcopy
import threading, functools

from .macro_mode import get_macro_mode

"""NOTE: data and schema can be edited via mount
If there is buffering, only the buffer can be edit via mount
"""

"""
OverlayMonitor warns if an inchannel is overwritten via handle
 and again when the inchannel overwrites it back
But if the StructuredCell is buffered, this warning is lost
"""

# TODO: re-think mount + slave: read-only?  re-direct to different cell?

class Inchannel(CellLikeBase):
    _authoritative = True
    _mount = None

    def __init__(self, structured_cell, inchannel):
        self.structured_cell = weakref.ref(structured_cell)
        self.inchannel = inchannel
        name = inchannel if inchannel != () else "self"
        self.name = name
        super().__init__()

    def deserialize(self, value, transfer_mode, access_mode, *, from_pin, **kwargs):
        assert from_pin
        if value is None:
            self._status = self.StatusFlags.UNDEFINED
        else:
            self._status = self.StatusFlags.OK
        structured_cell = self.structured_cell()
        if structured_cell is None:
            return
        monitor = structured_cell.monitor
        if structured_cell._is_silk:
            handle = structured_cell._silk
            if structured_cell.buffer is not None:
                bufmonitor = structured_cell.bufmonitor
                assert isinstance(handle.data, MixedBase)
                bufmonitor.set_path(self.inchannel, value, from_pin=True)
                handle.validate()
            else:
                try:
                    with handle.fork():
                        assert isinstance(handle.data, MixedBase)
                        monitor.set_path(self.inchannel, value, from_pin=True)
                    monitor._update_outchannels(self.inchannel)
                except:
                    traceback.print_exc(0)
                    return False
        else:
            monitor.receive_inchannel_value(self.inchannel, value)
        different, text_different = True, True #TODO: keep checksum etc. to see if value really changed
        return different, text_different

    @property
    def authoritative(self):
        return self._authoritative

    @property
    def path(self):
        structured_cell = self.structured_cell()
        name = self.name
        if isinstance(name, str):
            name = (name,)
        if structured_cell is None:
            return ("<None>",) + name
        return structured_cell.path + name

    def status(self):
        """The cell's current status."""
        return self._status.name

    @property
    def _context(self):
        return self.structured_cell().data._context

    @property
    def value(self):
        return self.structured_cell().monitor.get_data(self.inchannel)


class Outchannel(CellLikeBase):
    """
    Behaves like cells
    'worker_ref' actually is a reference to structured_cell
    """
    _mount = None
    transfer_mode = "copy"
    access_mode = None
    _buffered = False
    _last_value = None ###TODO: use checksums; for now, only used for buffered
    def __init__(self, structured_cell, outchannel):
        self.structured_cell = weakref.ref(structured_cell)
        self.outchannel = outchannel
        name = outchannel if outchannel != () else "self"
        self.name = name
        super().__init__()
        if structured_cell.buffer is not None:
            self._buffered = True

    def serialize(self, transfer_mode, access_mode):
        from ..silk import Silk
        structured_cell = self.structured_cell()
        assert structured_cell is not None
        data = structured_cell.monitor.get_data(self.outchannel)
        if (transfer_mode, access_mode) == ("ref", "pythoncode"):
            #TODO: - for now, assert content_type is pytransformer
            #      - single code (for now, it is copied from cell.py)
            import inspect, ast
            from .cached_compile import cached_compile
            from ast import PyCF_ONLY_AST, FunctionDef
            class FakeTransformerCell:
                def __init__(self, value):
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
                    self.value = value
            result = FakeTransformerCell(data)
            return result, None #TODO: checksum?
        data = deepcopy(data) ###TODO: rethink a bit; note that deepcopy also casts data from Silk to dict!
        if access_mode == "silk":
            #Schema-less silk; just for attribute access syntax
            data = Silk(data=data, stateful=isinstance(data, MixedBase))
        return data, None #TODO: checksum?

    def deserialize(self, *args, **kwargs):
        return True, True #dummy

    def send_update(self, value):
        if value is None and self._status == self.StatusFlags.UNDEFINED:
           return
        if value is None:
            self._status = self.StatusFlags.UNDEFINED
        else:
            self._status = self.StatusFlags.OK
        structured_cell = self.structured_cell()
        if self._buffered:
            if structured_cell._plain or structured_cell.storage.value == "pure-plain":
                if value == self._last_value:
                    return value
            self._last_value = deepcopy(value)
        assert structured_cell is not None
        data = structured_cell.data
        manager = data._get_manager()
        manager.set_cell(self, value)
        return value

    @property
    def path(self):
        structured_cell = self.structured_cell()
        name = self.name
        if isinstance(name, str):
            name = (name,)
        if structured_cell is None:
            return ("<None>",) + name
        return structured_cell.path + name

    def status(self):
        """The cell's current status."""
        return self._status.name

    @property
    def _context(self):
        return self.structured_cell().data._context

    @property
    def value(self):
        return self.structured_cell().monitor.get_data(self.outchannel)

class BufferWrapper:
    def __init__(self, data, storage, form):
        self.data = data
        self.storage = storage
        self.form = form

def update_hook(cell):
    cell._reset_checksums()
    if cell._mount is not None:
        cell._get_manager().mountmanager.add_cell_update(cell)

class StructuredCell(CellLikeBase):
    _mount = None
    def __init__(
      self,
      name,
      data,
      storage,
      form,
      buffer,
      schema,
      inchannels,
      outchannels
    ):
        from ..silk import Silk
        if not get_macro_mode():
            if not data._root()._auto_macro_mode:
                raise Exception("This operation requires macro mode, since the toplevel context was constructed in macro mode")
        super().__init__()
        self.name = name

        assert isinstance(data, Cell)
        data._slave = True
        self.data = data
        if storage is None:
            assert isinstance(data, JsonCell)
            self._plain = True
        else:
            assert isinstance(storage, TextCell)
            storage._slave = True
            self._plain = False
        self.storage = storage

        assert isinstance(form, JsonCell)
        form._slave = True
        val = form._val
        assert val is None or isinstance(val, dict)
        self.form = form

        if schema is None:
            self._is_silk = False
        else:
            assert isinstance(schema, JsonCell)
            val = schema._val
            if val is None:
                manager = schema._get_manager()
                manager.set_cell(schema, {})
                val = schema._val
            assert isinstance(val, dict)
            self._is_silk = True
        self.schema = schema

        if buffer is not None:
            assert self._is_silk
            assert isinstance(buffer, BufferWrapper)
            if self._plain:
                assert isinstance(buffer.data, JsonCell)
                buffer.data._slave = True
                assert buffer.storage is None
            else:
                assert isinstance(buffer.data, Cell)
                buffer.data._slave = True
                assert isinstance(buffer.storage, TextCell)
                buffer.storage._slave = True
            assert isinstance(buffer.form, JsonCell)
            buffer.form._slave = True
        self.buffer = buffer

        self.inchannels = {}
        if inchannels is not None:
            for inchannel in inchannels:
                self.inchannels[inchannel] = Inchannel(self, inchannel)
        self.outchannels = {}
        if outchannels is not None:
            for outchannel in outchannels:
                self.outchannels[outchannel] = Outchannel(self, outchannel)

        monitor_data = self.data._val
        assert not isinstance(monitor_data, MixedBase)
        monitor_storage = self.storage._val if self.storage is not None else None
        monitor_form = self.form._val
        monitor_inchannels = list(self.inchannels.keys())
        monitor_outchannels = {ocname:oc.send_update for ocname, oc in self.outchannels.items()}
        data_hook = None
        if not isinstance(monitor_data, (list, dict)):
            data_hook = self._data_hook
        form_hook = None
        if not isinstance(monitor_form, (list, dict)):
            form_hook = self._form_hook
        storage_hook = self._storage_hook
        data_update_hook = functools.partial(update_hook, self.data)
        form_update_hook = functools.partial(update_hook, self.form)

        self.monitor = OverlayMonitor(
            data=monitor_data,
            storage=monitor_storage,
            form=monitor_form,
            inchannels=monitor_inchannels,
            outchannels=monitor_outchannels,
            plain=self._plain,
            attribute_access=self._is_silk,
            data_hook=data_hook,
            form_hook=form_hook,
            storage_hook=storage_hook,
            data_update_hook=data_update_hook,
            form_update_hook=form_update_hook,
        )

        if self._is_silk:
            schema_update_hook = functools.partial(update_hook, self.schema)
            silk_buffer = None
            if buffer is not None:
                monitor_buffer_data = buffer.data._val
                monitor_buffer_storage = self.buffer.storage._val if self.buffer.storage is not None else None
                monitor_buffer_form = self.buffer.form._val
                buffer_data_hook = None
                if not isinstance(monitor_buffer_data, (list, dict)):
                    buffer_data_hook = self._buffer_data_hook
                buffer_form_hook = None
                if not isinstance(monitor_buffer_form, (list, dict)):
                    buffer_form_hook = self._buffer_form_hook
                buffer_storage_hook = self._buffer_storage_hook
                buffer_data_update_hook = functools.partial(update_hook, self.buffer.data)
                buffer_form_update_hook = functools.partial(update_hook, self.buffer.form)
                self.bufmonitor = MakeParentMonitor(
                  data=monitor_buffer_data,
                  storage=monitor_buffer_storage,
                  form=monitor_buffer_form,
                  plain=self._plain,
                  attribute_access=True,
                  data_hook=buffer_data_hook,
                  form_hook=buffer_form_hook,
                  storage_hook=buffer_storage_hook,
                  form_update_hook=buffer_form_update_hook,
                  data_update_hook=buffer_data_update_hook,
                )
                silk_buffer = self.bufmonitor.get_path()
            self._silk = Silk(
                schema=schema._val,
                data=self.monitor.get_path(),
                buffer=silk_buffer,
                stateful=True,
                schema_update_hook=schema_update_hook,
            )
        if self.buffer is not None:
            mountcell = self.buffer.data
        else:
            mountcell = self.data
        mountcell._mount_setter = self._set_from_mounted_file
        if self.schema is not None:
            self.schema._mount_setter = self._set_schema_from_mounted_file

    def connect_inchannel(self, source, inchannel):
        if inchannel == ("self",):
            inchannel = ()
        ic = self.inchannels[inchannel]
        manager = source._get_manager()
        if isinstance(source, Cell):
            manager.connect_cell(source, ic)
        else:
            manager.connect_pin(source, ic)
        v = self.monitor.get_path(inchannel)
        status = ic.StatusFlags.OK if v is not None else ic.StatusFlags.UNDEFINED
        ic._status = status

    def connect_outchannel(self, outchannel, target):
        if outchannel == ("self",):
            outchannel = ()
        oc = self.outchannels[outchannel]
        manager = self.data._get_manager()
        manager.connect_cell(oc, target)
        v = self.monitor.get_path(outchannel)
        status = oc.StatusFlags.OK if v is not None else oc.StatusFlags.UNDEFINED
        oc._status = status

    def _data_hook(self, value):
        cell = self.data
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        result = self.data._val
        return result

    def _form_hook(self, value):
        cell = self.form
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        return self.form._val

    def _storage_hook(self, value):
        cell = self.storage
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        return self.storage._val

    def _buffer_data_hook(self, value):
        cell = self.buffer.data
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        result = self.buffer.data._val
        return result

    def _buffer_form_hook(self, value):
        cell = self.buffer.form
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        return self.buffer.form._val

    def _buffer_storage_hook(self, value):
        cell = self.buffer.storage
        manager = cell._get_manager()
        manager.set_cell(cell, value, force=True)
        return self.buffer.storage._val

    def _set_from_mounted_file(self, filebuffer, checksum):
        cell = self.buffer.data if self.buffer is not None else self.data
        if threading.current_thread() != threading.main_thread():
            work = functools.partial(
              self._set_from_mounted_file,
              filebuffer=filebuffer, checksum=checksum
            )
            manager = cell._get_manager()
            manager.workqueue.append(work)
            return
        value = cell._from_buffer(filebuffer)
        self.set(value)

    def _set_schema_from_mounted_file(self, filebuffer, checksum):
        cell = self.schema
        if threading.current_thread() != threading.main_thread():
            work = functools.partial(
              self._set_schema_from_mounted_file,
              filebuffer=filebuffer, checksum=checksum
            )
            manager = cell._get_manager()
            manager.workqueue.append(work)
            return
        value = cell._from_buffer(filebuffer)
        cell._val.update(value)
        self._silk.validate()


    def set(self, value):
        if self._is_silk:
            self._silk.set(value)
        else:
            self.monitor.set_path((), value)

    def status(self):
        return self.data.status()

    @property
    def value(self):
        """
        Returns the current value
        Unless the schema has changed, this value always conforms
         to schema, even for buffered StructuredCells
        """
        result = self.monitor.get_data()
        return result

    @property
    def handle(self):
        """
        Returns handle for manipulation
        For buffered StructuredCells, its value may be against schema
        """
        if self._is_silk:
            result = self._silk
        else:
            monitor = self.monitor
            result = monitor.get_path()
        return result

print("TODO: Runtime wrapper around StructuredCell that protects against .foo = bar\
 where .handle.foo = bar is intended")

"""
TODO (long-term): a mechanism to overrule checksum computation
_slave takes away the checksum responsibility, this now lies with StructuredCell
By default: serialize the entire value (still to do), and calc a checksum of that
However, this is terribly inefficient if:
 there is a data structure that consists of part X and part Y
 where X is huge and unchanging, and Y is small and changes all the time
In that case, it is much better to delegate the checksum computation to X and Y and
 to return some checksum-of-checksums
The configuration of checksum calculation should probably be another cell
"""

#TODO: schema could become not a slave
# but then, it may be updated from elsewhere; need to listen for that
#  and the schema may be connected to a target, which would require listening as well
