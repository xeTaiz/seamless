"""
All runtime access to cells and workers goes via the manager
also something like .touch(), .set().
Doing .set() on non-authoritative cells will result in a warning
Connecting to a cell with a value (making it non-authoritative), will likewise result in a warning
Cells can have only one outputpin writing to them, this is strictly enforced.

The manager has a notion of the managers of the subcontexts
manager.set_cell and manager.pin_send_update are thread-safe (can be invoked from any thread)
"""

from .connection import Connection, CellToCellConnection, CellToPinConnection, \
 PinToCellConnection
from . import protocol
from ..mixed import MixedBase

import threading
import functools
import weakref
import traceback
import contextlib

def main_thread_buffered(func):
    def main_thread_buffered_wrapper(self, *args, **kwargs):
        if threading.current_thread() != threading.main_thread():
            work = functools.partial(func, self, *args, **kwargs)
            self.workqueue.append(work)
        else:
            func(self, *args, **kwargs)
    return main_thread_buffered_wrapper

def manager_buffered(func):
    def manager_buffered_wrapper(self, *args, **kwargs):
        if not self.active and not self.flushing:
            work = functools.partial(func, self, *args, **kwargs)
            self.buffered_work.append(work)
        else:
            func(self, *args, **kwargs)
    return manager_buffered_wrapper

def with_successor(argname, argindex):
    def with_successor_outer_wrapper(func):
        def with_successor_wrapper(self, *args0, **kwargs0):
            args, kwargs = args0, kwargs0
            if self.destroyed and self.successor:
                raise NotImplementedError #untested! need to implement destroyed, successor
                if len(args) > argindex:
                    target = args[argindex]
                elif target in kwargs:
                    target = kwargs[argname]
                else:
                    raise TypeError((argname, argindex))

                if isinstance(target, CellLikeBase):
                    successor_target = getattr(self.successor, target.name)
                elif isinstance(target, InputPin):
                    worker_name = target.get_workerr().name #pin.get_worker()? can't remember API
                    successor_target = attr(self.successor, worker_name).pinsss[target.name] ##pins[]? can't remember API

                if successor_target._manager.destroyed:
                    return
                if len(args) > argindex:
                    args = args[:argindex] + [successor_target] + args[argindex+1:]
                elif target in kwargs:
                    kwargs = kwargs.copy()
                    kwargs[argname] = successor_target
            return func(self, *args, **kwargs)
        return with_successor_wrapper
    return with_successor_outer_wrapper

class Manager:
    active = True
    destroyed = False
    successor = None
    flushing = False
    filled_objects = []
    on_equilibrate_callbacks = None
    _editpin_origin = None
    _cell_update_hook = None
    def __init__(self, ctx):
        self.ctx = weakref.ref(ctx)
        if ctx._toplevel:
            self.rootmanager = weakref.ref(self)
        else:
            self.rootmanager = weakref.ref(self.ctx()._root()._get_manager())
        self.sub_managers = {}
        self.cell_to_pins = {} #cell => inputpins
        self.cell_to_cells = {} #cell => CellToCellConnection list
        self.cell_from_pin = {} #cell => outputpin
        self.cell_from_cell = {} #alias target cell => (index, source cell, transfer mode)
        self.pin_from_cell = {} #inputpin => (index, cell)
        self.pin_to_cells = {} #outputpin => (index, cell) list
        self._ids = 0
        self.unstable = set()
        self.children_unstable = set()
        #for now, just a single global workqueue
        from .mainloop import workqueue
        self.workqueue = workqueue
        #for now, just a single global mountmanager
        from .mount import mountmanager
        self.mountmanager = mountmanager
        self.buffered_work = []

    @main_thread_buffered
    @manager_buffered
    def set_stable(self, worker, value):
        if self.destroyed:
            return
        if value:
            self.unstable.discard(worker)
            if not len(self.unstable) and not len(self.workqueue):
                self.rootmanager().test_equilibrate()
        else:
            self.unstable.add(worker)

    def activate(self, only_macros, triggered=False):
        if only_macros:
            for f in self.filled_objects:
                f.activate(only_macros=True)
            from .macro import Macro
            for childname, child in self.ctx()._children.items():
                if isinstance(child, Context):
                    child._manager.activate(only_macros=True)
                elif isinstance(child, Macro):
                    child.activate(only_macros=True)
        else:
            self.active = True
            for f in self.filled_objects:
                f.activate(only_macros=False)
            self.filled_objects = []
            for childname, child in self.ctx()._children.items():
                if isinstance(child, Context):
                    child._manager.activate(only_macros=False, triggered=True)
                elif isinstance(child, Worker):
                    child.activate(only_macros=False)
            if not triggered:
                self.flush()

    def deactivate(self):
        self.active = False
        for childname, child in self.ctx()._children.items():
            if isinstance(child, Context):
                child._manager.deactivate()


    def stop_flushing(self):
        self.flushing = False
        for childname, child in self.ctx()._children.items():
            if isinstance(child, Context):
                child._manager.stop_flushing()

    def flush(self, from_parent=False):
        assert threading.current_thread() == threading.main_thread()
        assert self.active or self.destroyed
        self.flushing = True
        for childname, child in self.ctx()._children.items():
            if isinstance(child, Context):
                child._manager.flush(from_parent=True) # need to flush only once
                                            # with self.active or self.destroyed, work buffer shouldn't accumulate
        try:
            self.workqueue.flush()
            while self.active and len(self.buffered_work):
                item = self.buffered_work.pop(0)
                try:
                    item()
                except:
                    traceback.print_exc()
                    #TODO: log exception
        finally:
            if not from_parent:
                self.stop_flushing()

    def destroy(self,from_del=False):
        if self.destroyed:
            return
        self.destroyed = True
        self.ctx().destroy(from_del=from_del)
        #all of the children are now dead
        #  only in the buffered_work and the work queue there is still some function calls to the children

    def get_id(self):
        self._ids += 1
        return self._ids

    def _connect_cell_to_cell(self, cell, target, transfer_mode, duplex=False):
        target0 = target
        if isinstance(target, Link):
            target = target.get_linked()
        if not target.authoritative:
            raise Exception("%s: is non-authoritative (already dependent on another worker/cell)" % target)
        if target0._is_sealed() or cell._is_sealed():
            concrete, con_id = layer.connect_cell(cell, target0, transfer_mode)
        else:
            concrete = True
            con_id = self.get_id()
        if isinstance(cell, (Outchannel, Editchannel)) and \
          isinstance(target, (Inchannel, Editchannel)):
            assert cell.structured_cell() is not target.structured_cell, (cell, target)

        connection = CellToCellConnection(con_id, cell, target, transfer_mode, duplex=duplex)
        if cell not in self.cell_to_cells:
            self.cell_to_cells[cell] = []
        self.cell_to_cells[cell].append(connection)

        if not duplex:
            target._authoritative = False
        if concrete:
            other = target._get_manager()
            other.cell_from_cell[target] = connection

            if cell._status == Cell.StatusFlags.OK:
                connection.fire(only_text=False)

    def _connect_editchannel_to_cell(self, channel, cell, transfer_mode):
        self._connect_cell_to_cell(channel, cell, transfer_mode, duplex=True)
        ###self._connect_cell_to_cell(cell, channel, transfer_mode, duplex=True)

    @main_thread_buffered
    def connect_cell(self, cell, target, transfer_mode=None, duplex=False):
        # "duplex" is for an synchronizing connection
        #  it implies that a reverse connection will be formed
        #  and it doesn't take away authority
        if self.destroyed:
            return
        assert cell._root() is target._root()
        assert isinstance(cell, CellLikeBase)
        assert not isinstance(cell, Inchannel)
        assert cell._get_manager() is self
        target0 = target
        if isinstance(target, Link):
            target = target.get_linked()

        assert isinstance(target, (InputPinBase, EditPinBase, CellLikeBase, Path))

        if isinstance(target, CellLikeBase):
            assert not isinstance(target, Outchannel) or isinstance(target, Editchannel)
            return self._connect_cell_to_cell(cell, target0, transfer_mode, duplex=duplex)

        if cell._is_sealed() or target._is_sealed():
            concrete, con_id = layer.connect_cell(cell, target0, transfer_mode)
        else:
            concrete = True
            con_id = self.get_id()

        connection = CellToPinConnection(con_id, cell, target)
        if concrete:
            if isinstance(target, EditPinBase):
                pass #will be dealt with in connect_pin invocation below
            elif cell._status == Cell.StatusFlags.OK:
                connection.fire()

        if cell not in self.cell_to_pins:
            self.cell_to_pins[cell] = []
        self.cell_to_pins[cell].append(connection)

        if not isinstance(target, Path):
            other = target._get_manager()
            other.pin_from_cell[target] = connection

        if isinstance(target, EditPinBase):
            mgr = target0._get_manager()
            mgr.connect_pin(target0, cell, mirror_connection=connection)

    @main_thread_buffered
    def connect_pin(self, pin, target, mirror_connection=None):
        if self.destroyed:
            return
        target0 = target
        assert pin._root() is target._root()
        assert pin._get_manager() is self
        if isinstance(target, Link):
            target = target.get_linked()
        assert isinstance(target, (CellLikeBase, Path)), (pin, target)
        assert isinstance(pin, (OutputPinBase, EditPinBase)), (pin, target)

        assert (mirror_connection is not None) == (isinstance(pin, EditPinBase))

        if not isinstance(pin, EditPinBase) and not target.authoritative:
            raise Exception("%s: is non-authoritative (already dependent on another worker/cell)" % target)
        if target._is_sealed() or pin._is_sealed():
            concrete, con_id = layer.connect_pin(pin, target)
        else:
            concrete = True
            con_id = self.get_id()
        if concrete:
            worker = pin.worker_ref()
            assert worker is not None #weakref may not be dead

        connection = PinToCellConnection(con_id, pin, target)
        if pin not in self.pin_to_cells:
            self.pin_to_cells[pin] = []
        self.pin_to_cells[pin].append(connection)

        if not isinstance(target, Path):
            other = target._get_manager()
            other.cell_from_pin[target] = connection
            if not isinstance(pin, EditPinBase):
                target._authoritative = False

        if concrete:
            if pin.last_value is not None:
                connection.fire(pin.last_value, pin.last_value_preliminary)
            elif isinstance(pin, EditPinBase):
                if target._status == Cell.StatusFlags.OK:
                    mirror_connection.fire()

    @main_thread_buffered
    def connect_link(self, link, target):
        if self.destroyed:
            return
        assert link._root() is target._root()
        assert link._get_manager() is self
        linked = link.get_linked()
        """
        if linked._is_sealed():
            assert isinstance(linked, (Cell, EditPinBase, OutputPinBase))
            path = Path(linked)
            layer.connect_path(path, target)
            return self
        if isinstance(linked, Path):
            return
        """
        if isinstance(linked, Path):
            layer.connect_path(linked, target)
            return self
        manager = linked._get_manager()
        if isinstance(linked, Cell):
            manager.connect_cell(linked, target)
        elif isinstance(linked, (EditPinBase, OutputPinBase) ):
            manager.connect_pin(linked, target)
        else:
            raise TypeError(linked)
        return self

    @main_thread_buffered
    @manager_buffered
    @with_successor("cell", 0)
    def set_cell(self, cell, value, *,
      default=False, from_buffer=False,
      force=False, from_pin=False, origin=None
    ):
        from .macro_mode import macro_mode_on, get_macro_mode
        from .mount import is_dummy_mount
        if self.destroyed:
            return
        assert isinstance(cell, CellLikeBase)
        assert cell._get_manager() is self
        if isinstance(value, MixedBase):
            value = value.data
        if cell is origin: #update comes from an outchannel
                           #deserialize is not needed
            different, text_different = True, True
        else:
            different, text_different = protocol.set_cell(
              cell, value,
              default=default, from_buffer=from_buffer,
              force=force, from_pin=from_pin
            )
        only_text = (text_different and not different)
        if text_different and not is_dummy_mount(cell._mount) and self.active:
            if not get_macro_mode():
                self.mountmanager.add_cell_update(cell)
        if different or text_different:
            self.cell_send_update(cell, only_text, origin)
            if not from_pin:
                cell_update_hook = self._root()._cell_update_hook
                if cell_update_hook is not None:
                    self._root()._run_cell_update_hook(cell, value)

    @main_thread_buffered
    def _run_cell_update_hook(self, cell, value):
        self._cell_update_hook(cell, value)


    @main_thread_buffered
    @manager_buffered
    @with_successor("cell", 0)
    def touch_cell(self, cell):
        from .mount import is_dummy_mount
        if self.destroyed:
            return
        assert isinstance(cell, CellLikeBase)
        assert cell._get_manager() is self
        self.cell_send_update(cell, only_text=False, origin=None)
        if not is_dummy_mount(cell._mount) and self.active:
            self.mountmanager.add_cell_update(cell)

    @main_thread_buffered
    @manager_buffered
    @with_successor("worker", 0)
    def touch_worker(self, worker):
        if self.destroyed:
            return
        assert isinstance(worker, Worker)
        assert worker._get_manager() is self
        worker._touch()

    @main_thread_buffered
    @manager_buffered
    def notify_attach_child(self, childname, child):
        if isinstance(child, Context):
            assert isinstance(child._manager, Manager)
            self.sub_managers[childname] = child._manager
        elif isinstance(child, Cell):
            if child._prelim_val is not None:
                value, default = child._prelim_val
                self.set_cell(child, value, default=default)
                child._prelim_val = None

    @main_thread_buffered
    @manager_buffered
    @with_successor("pin", 0)
    def pin_send_update(self, pin, value, preliminary, target=None):
        #TODO: explicit support for preliminary values
        assert pin._get_manager() is self
        if isinstance(value, MixedBase):
            value = value.data
        found = False
        for con in self.pin_to_cells.get(pin,[]):
            cell = con.target
            if con.id < 0 and cell is None: #layer connections, may be None
                continue
            if target is not None and cell is not target:
                continue
            found = True
            with self._set_editpin_origin(pin):
                con.fire(value, preliminary)
        if target is not None and not found:
            print("Warning: %s was targeted by triggering pin %s, but not found" % (target, pin))

    @main_thread_buffered
    @manager_buffered
    @with_successor("cell", 0)
    def cell_send_update(self, cell, only_text, origin):
        if self.destroyed:
            return

        #Activates pins
        for con in self.cell_to_pins.get(cell, []):
            pin = con.target
            if pin is origin: #editpin that sent the update
                continue
            if con.id < 0 and pin is None: #layer connections, may be None
                continue
            if con.target is self.ctx()._root()._get_manager()._editpin_origin:
                continue
            con.fire(only_text)

        #Activates aliases
        assert isinstance(cell, CellLikeBase)
        for con in self.cell_to_cells.get(cell, []):
            if con.id < 0 and con.target is None: #layer connections, may be None
                continue
            #from_pin is set to True, also for aliases
            assert con.source._get_manager() is self
            con.fire(only_text)

    def set_filled_objects(self, filled_objects):
        self.filled_objects = filled_objects

    def on_equilibrate(self, callback):
        assert self.ctx()._toplevel
        if self.on_equilibrate_callbacks is None:
            self.on_equilibrate_callbacks = []
        self.on_equilibrate_callbacks.append(callback)

    def _test_equilibrate(self):
        if len(self.unstable):
            return False
        for sub_manager in self.sub_managers.values():
            if not sub_manager._test_equilibrate():
                return False
        return True

    def test_equilibrate(self):
        if not self.on_equilibrate_callbacks:
            return
        if self._test_equilibrate():
            try:
                for callback in self.on_equilibrate_callbacks:
                    callback()
            finally:
                del self.on_equilibrate_callbacks

    @contextlib.contextmanager
    def _set_editpin_origin(self, pin):
        from .worker import EditPin
        mgr = self.ctx()._root()._get_manager()
        assert mgr._editpin_origin is None, mgr._editpin_origin
        if isinstance(pin, EditPin):
            mgr._editpin_origin = pin
        yield
        mgr._editpin_origin = None

    def cell_update_hook(self, update_hook):
        self._root()._cell_update_hook = update_hook

    def _root(self):
        return self.ctx()._root()._get_manager()

from .context import Context
from .cell import Cell, CellLikeBase
from .worker import Worker, InputPin, EditPin, \
  InputPinBase, EditPinBase, OutputPinBase
from .transformer import Transformer
from .structured_cell import Inchannel, Outchannel, Editchannel
from . import layer
from .link import Link
from .layer import Path
