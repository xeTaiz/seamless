"""
For now, there is a single _read() and a single _write() method, tied to the
 file system. In the future, these will be code cells in a context, and it
 will be possible to register custom _read() and _write() cells, e.g. for
 storage in a database.
Same for _exists.

_init() is invoked at startup:
 If authority is "file" or "file-strict":
  This may invoke _read(), but only if the file exists
   (if cell is non-empty and is different, a warning is printed)
   ("file-strict": the file must exist)
  If not, this may invoke _write(), but only if the cell is non-empty
 If authority is "cell":
   This may invoke _write(), but only if the cell is non-empty
     (if the file exists and is different, a warning is printed)
   If not, this may invoke _read(), but only if the file exists
Periodically, conditional_read() and conditional_write() are invoked,
 that check if a read/write is necessary, and if so, invoke _read()/_write()

NOTE: resolve_register returns immediately if there has been an exception raised
"""
from .protocol import cson2json, json_encode

from weakref import WeakValueDictionary, WeakKeyDictionary, WeakSet, ref
from threading import Thread, RLock, Event
from collections import deque, OrderedDict
import sys, os
import time
import traceback
import copy
from contextlib import contextmanager
import json

NoStash = 1

def is_dummy_mount(mount):
    if mount is None:
        return True
    assert isinstance(mount, dict), mount
    if list(mount.keys()) == ["extension"]:
        return True
    return False

class MountItem:
    last_exc = None
    parent = None
    _destroyed = False
    def __init__(self, parent, cell, path, mode, authority, persistent, *,
      dummy=False, **kwargs
    ):
        if parent is not None:
            self.parent = ref(parent)
        self.path = path
        self.cell = ref(cell)
        self.dummy = dummy
        assert mode in ("r", "w", "rw"), mode #read from file, write to file, or both
        self.mode = mode
        assert persistent in (True, False, None)
        assert authority in ("cell", "file", "file-strict"), authority
        if authority == "file-strict":
            assert persistent
        elif authority in ("file", "file-strict"):
            assert "r" in self.mode, (authority, mode)
        self.authority = authority
        self.kwargs = kwargs
        self.last_checksum = None
        self.last_time = None
        self.last_mtime = None
        self.persistent = persistent


    def init(self):
        if self._destroyed:
            return
        assert self.parent is not None
        cell = self.cell()
        if cell is None:
            return
        exists = self._exists()
        cell_empty = (cell.status() != "OK")
        if self.authority in ("file", "file-strict"):
            if exists:
                with self.lock:
                    filevalue = self._read()
                    update_file = True
                    file_checksum = None
                    if not cell_empty:
                        file_checksum = cell._checksum(filevalue, buffer=True)
                        if file_checksum == cell.text_checksum():
                            update_file = False
                        else:
                            print("Warning: File path '%s' has a different value, overwriting cell" % self.path) #TODO: log warning
                    self._after_read(file_checksum)
                if update_file:
                    self.set(filevalue, checksum=file_checksum)
            elif self.authority == "file-strict":
                raise Exception("File path '%s' does not exist, but authority is 'file-strict'" % self.path)
            else:
                if "w" in self.mode and not cell_empty:
                    value = cell.serialize_buffer()
                    checksum = cell.text_checksum()
                    with self.lock:
                        self._write(value)
                        self._after_write(checksum)
        else: #self.authority == "cell"
            must_read = ("r" in self.mode)
            if not must_read and cell._master is not None and \
              cell._master[1] in ("form", "storage"):
                must_read = True
            if not cell_empty:
                value = cell.serialize_buffer()
                checksum = cell.text_checksum()
                if exists and must_read:
                    with self.lock:
                        filevalue = self._read()
                        file_checksum = cell._checksum(filevalue, buffer=True)
                        if file_checksum != checksum:
                            if "w" in self.mode:
                                print("Warning: File path '%s' has a different value, overwriting file" % self.path) #TODO: log warning
                            else:
                                print("Warning: File path '%s' has a different value, no overwriting enabled" % self.path) #TODO: log warning
                        self._after_read(file_checksum)
                if "w" in self.mode:
                    with self.lock:
                        self._write(value)
                        self._after_write(checksum)
            else:
                if exists and must_read:
                    with self.lock:
                        filevalue = self._read()
                        file_checksum = cell._checksum(filevalue, buffer=True)
                        self.set(filevalue, checksum=file_checksum)
                        self._after_read(file_checksum)

    def set(self, filevalue, checksum):
        from .cell import JsonCell
        if self._destroyed:
            return
        cell = self.cell()
        if cell is None:
            return
        #Special mount mode for JSON: whatever is read will be passed through cson2json
        if filevalue is not None and isinstance(cell, JsonCell) and "w" in self.mode:
            d = cson2json(filevalue)
            filevalue2 = json_encode(d, sort_keys=True, indent=2)
            if filevalue2 != filevalue:
                filevalue = filevalue2
                self._write(filevalue)
        if cell._mount_setter is not None:
            cell._mount_setter(filevalue, checksum)
            cell._get_manager().cell_send_update(cell, False, None)
        else:
            cell.from_buffer(filevalue, checksum=checksum)

    @property
    def lock(self):
        assert self.parent is not None
        return self.parent().lock

    def _read(self):
        #print("read", self.cell())
        binary = self.kwargs["binary"]
        encoding = self.kwargs.get("encoding")
        filemode = "rb" if binary else "r"
        with open(self.path.replace("/", os.sep), filemode, encoding=encoding) as f:
            return f.read()

    def _write(self, filevalue, with_none=False):
        assert "w" in self.mode
        binary = self.kwargs["binary"]
        encoding = self.kwargs.get("encoding")
        filemode = "wb" if binary else "w"
        filepath = self.path.replace("/", os.sep)
        if filevalue is None:
            if not with_none:
                if os.path.exists(filepath):
                    os.unlink(filepath)
                return
            filevalue = b"" if binary else ""
        with open(filepath, filemode, encoding=encoding) as f:
            f.write(filevalue)

    def _exists(self):
        return os.path.exists(self.path.replace("/", os.sep))


    def _after_write(self, checksum):
        self.last_checksum = checksum
        self.last_time = time.time()
        try:
            stat = os.stat(self.path)
            self.last_mtime = stat.st_mtime
        except Exception:
            pass

    def conditional_write(self, with_none=False):
        if self._destroyed:
            return
        if not "w" in self.mode:
            return
        cell = self.cell()
        if cell is None:
            return
        status = cell.status()
        if status != "OK":
            if not with_none or status != "UNDEFINED":
                return
        checksum = cell.text_checksum()
        if checksum is None or self.last_checksum != checksum:
            value = cell.serialize_buffer()
            if value is not None:
                assert cell._checksum(value, buffer=True) == checksum, cell._format_path()
            with self.lock:
                self._write(value, with_none=with_none)
                self._after_write(checksum)

    def _after_read(self, checksum, *, mtime=None):
        self.last_checksum = checksum
        if mtime is None:
            stat = os.stat(self.path)
            mtime = stat.st_mtime
        self.last_mtime = mtime

    def conditional_read(self):
        if self._destroyed:
            return
        cell = self.cell()
        if cell is None:
            return
        if not self._exists():
            return
        with self.lock:
            stat = os.stat(self.path)
            mtime = stat.st_mtime
            file_checksum = None
            if self.last_mtime is None or mtime > self.last_mtime:
                filevalue = self._read()
                file_checksum = cell._checksum(filevalue, buffer=True)
                self._after_read(file_checksum, mtime=mtime)
        cell_checksum = None
        if cell.value is not None:
            cell_checksum = cell.text_checksum()
        if file_checksum is not None and file_checksum != cell_checksum:
            if "r" in self.mode:
                self.set(filevalue, checksum=file_checksum)
            else:
                print("Warning: write-only file %s (%s) has changed on disk, overruling" % (self.path, self.cell()))
                value = cell.serialize_buffer()
                assert cell._checksum(value, buffer=True) == cell_checksum, cell._format_path()
                with self.lock:
                    self._write(value)
                    self._after_write(cell_checksum)

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        if self.dummy:
            return
        if self.persistent == False and os.path.exists(self.path):
            #print("remove", self.path)
            os.unlink(self.path)

    def __del__(self):
        if self.dummy:
            return
        if self._destroyed:
            return
        self._destroyed = True
        print("undestroyed mount path %s" % self.path)
        #self.destroy()

class LinkItem:
    _destroyed = False
    linked_path = None
    def __init__(self, link, path, persistent):
        self.link = ref(link)
        self.path = path
        self.persistent = persistent

    def init(self):
        from .context import Context
        if self._destroyed:
            return
        linked = self.get_linked()
        is_dir = (isinstance(linked, Context))
        if is_dummy_mount(linked._mount):
            return
        linked_path = linked._mount["path"]
        os.symlink(linked_path, self.path, is_dir)
        self.linked_path = linked_path

    def get_linked(self):
        if self._destroyed:
            return
        link = self.link()
        if link is None:
            return
        linked = link.get_linked()
        return linked

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        if self.persistent == False:
            filepath = self.path
            unbroken_link = os.path.islink(filepath)
            broken_link = (os.path.lexists(filepath) and not os.path.exists(filepath))
            if unbroken_link or broken_link:
                os.unlink(filepath)

    def __del__(self):
        if self._destroyed:
            return
        self._destroyed = True
        print("undestroyed link path %s" % self.path)


class MountManagerStash:
    """Stashes away a part of the mounts that are all under a single context
    They can later be destroyed or restored, depending on what happens to the context
    NOTE: While the stash is active, there are ._mount objects (in cells and contexts)
     and MountItems that point to the same path, but with different cells and contexts
     Therefore, for the duration of the stash, it is imperative that all those are
      kept alive and not garbage-collected, until the stash is undone.
     This means that stashing must be done in a Python context (= with statement)
    """
    def __init__(self, parent, context):
        self._active = False
        self.root = context._root()
        self.parent = parent
        self.context = context
        self.mounts = WeakKeyDictionary()
        self.contexts = WeakSet()
        self.context_as_parent = WeakKeyDictionary()
        self.paths = set()

    def activate(self):
        assert not self._active
        self._active = True
        parent, context = self.parent, self.context
        for ctx in list(parent.contexts):
            assert not is_dummy_mount(ctx._mount), ctx
            if ctx._root() is self.root and ctx._part_of2(context):
                self.contexts.add(ctx)
                parent.contexts.remove(ctx)
                path = ctx._mount["path"]
                parent.paths[self.root].remove(path)
                self.paths.add(path)
        for cell, mountitem in list(parent.mounts.items()):
            assert not is_dummy_mount(cell._mount), cell
            ctx = cell._context()
            assert ctx is not None, cell
            if ctx._root() is self.root and ctx._part_of2(context):
                self.mounts[cell] = mountitem
                parent.mounts.pop(cell)
                path = cell._mount["path"]
                parent.paths[self.root].remove(path)
                self.paths.add(path)

    def _build_new_paths(self):
        """paths added by the parent since stash activation"""
        new_paths = {}

        parent, context = self.parent, self.context
        for ctx in list(parent.contexts):
            path = ctx._mount["path"]
            if ctx._root() is self.root and ctx._part_of2(context):
                new_paths[path] = ctx
        for cell, mountitem in list(parent.mounts.items()):
            assert not is_dummy_mount(cell._mount), cell
            ctx = cell._context()
            if ctx._root() is self.root and ctx._part_of2(context):
                path = cell._mount["path"]
                new_paths[path] = mountitem
        return new_paths

    def undo(self):
        from .context import Context
        assert self._active
        new_paths = self._build_new_paths()
        parent, context = self.parent, self.context
        for ctx in sorted(self.contexts, key=lambda l: -len(l.path)):
            assert not is_dummy_mount(ctx._mount), ctx
            path = ctx._mount["path"]
            if path in new_paths:
                new_context = new_paths[path]
                object.__setattr__(new_context, "_mount", None) #since we are not in macro mode
                new_paths.pop(path)
            parent.contexts.add(ctx)
            parent.paths[self.root].add(path)
        for cell, mountitem in self.mounts.items():
            assert not is_dummy_mount(cell._mount), cell
            path = cell._mount["path"]
            if path in new_paths:
                new_mountitem = new_paths[path]
                new_mountitem._destroyed = True
                if isinstance(mountitem, LinkItem):
                    new_link = new_mountitem.link()
                    object.__setattr__(new_link, "_mount", None) #since we are not in macro mode
                else:
                    new_cell = new_mountitem.cell()
                    object.__setattr__(new_cell, "_mount", None) #since we are not in macro mode
                new_paths.pop(path)
            parent.mounts[cell] = mountitem
            parent.paths[self.root].add(path)

        context_to_unmount = []
        for path, obj in new_paths.items():
            if isinstance(obj, Context):
                context_to_unmount.append(obj)
            elif isinstance(obj, LinkItem):
                parent.unmount(obj.link())
            else:
                parent.unmount(obj.cell())

        for context in sorted(context_to_unmount, key=lambda l: -len(l.path)):
            parent.unmount_context(context)

    def join(self):
        from .context import Context
        from .cell import Cell
        assert self._active
        new_paths = self._build_new_paths()
        parent, context = self.parent, self.context

        old_mountitems = {}
        for old_cell, old_mountitem in list(self.mounts.items()):
            assert not is_dummy_mount(old_cell._mount), old_cell
            path = old_cell._mount["path"]
            object.__setattr__(old_cell, "_mount", None) #since we are not in macro mode
            if path in new_paths:
                if isinstance(old_mountitem, MountItem):
                    old_mountitem._destroyed = True
                old_mountitems[path] = old_mountitem
            else:
                old_mountitem.destroy()

        old_paths = set()
        for old_ctx in sorted(self.contexts, key=lambda l: -len(l.path)):
            assert not is_dummy_mount(old_ctx._mount), old_ctx
            path = old_ctx._mount["path"]
            if path in new_paths:
                old_paths.add(path)
                new_context = new_paths[path]
                object.__setattr__(old_ctx, "_mount", None) #since we are not in macro mode
        for path in sorted(new_paths.keys(), key=lambda p:len(p)):
            obj = new_paths[path]
            if isinstance(obj, Context):
                new_context = obj
                if path not in old_paths:
                    assert new_context in self.context_as_parent, context
                    parent._check_context(new_context, self.context_as_parent[new_context])
        for path in sorted(new_paths.keys(), key=lambda p:len(p)):
            obj = new_paths[path]
            if isinstance(obj, MountItem):
                new_mountitem = obj
                #print("new_path", obj, hex(id(obj)), path in old_mountitems)
                if path in old_mountitems:
                    old_mountitem = old_mountitems[path]
                    rewrite = False
                    cell = new_mountitem.cell()
                    if cell._val is not None:
                        value = cell.serialize_buffer()
                        checksum = cell.text_checksum()
                        if "w" in old_mountitem.mode:
                            if type(old_mountitem.cell()) != type(cell):
                                rewrite = True
                            else:
                                if checksum != old_mountitem.last_checksum:
                                    rewrite = True
                    if rewrite:
                        with new_mountitem.lock:
                            new_mountitem._write(value)
                            new_mountitem._after_write(checksum)
                    else:
                        new_mountitem.last_mtime = old_mountitem.last_mtime
                        new_mountitem.last_checksum = old_mountitem.last_checksum
                else:
                    new_mountitem.init()
            elif isinstance(obj, LinkItem):
                new_linkitem = obj
                identical = False
                if path in old_mountitems:
                    old_linkitem = old_mountitems[path]
                    linked = new_linkitem.get_linked()
                    if linked._mount["path"] == old_linkitem.linked_path:
                        old = old_linkitem.get_linked()
                        if isinstance(old, Context) and isinstance(linked, Context):
                            identical = True
                        elif isinstance(old, Cell) and isinstance(linked, Cell):
                            identical = True
                    if identical:
                        old_linkitem._destroyed = True
                    else:
                        old_linkitem.destroy()
                if not identical:
                    new_linkitem.init()

class MountManager:
    _running = False
    _last_run = None
    _stop = False
    _mounting = False
    def __init__(self, latency):
        self.latency = latency
        self.mounts = WeakKeyDictionary()
        self.contexts = WeakSet()
        self.lock = RLock()
        self.cell_updates = deque()
        self._tick = Event()
        self.stash = None
        self.paths = WeakKeyDictionary()

    @property
    def reorganizing(self):
        return self.stash is not None

    @contextmanager
    def reorganize(self, context):
        if context is None:
            self.stash = NoStash
            yield
            self.stash = None
            return
        if self.stash is not None:
            assert context._part_of2(self.stash.context)
            yield
            return
        with self.lock:
            self.stash = MountManagerStash(self, context)
            try:
                self.stash.activate()
                yield
                #print("reorganize success")
                self.stash.join()
            except Exception as e:
                #print("reorganize failure")
                self.stash.undo()
                raise e
            finally:
                self.stash = None

    def add_mount(self, cell, path, mode, authority, persistent, **kwargs):
        root = cell._root()
        if root not in self.paths:
            paths = set()
            self.paths[root] = paths
        else:
            paths = self.paths[root]
        assert path not in paths, path
        #print("add mount", path, cell)
        paths.add(path)
        self.mounts[cell] = MountItem(self, cell, path, mode, authority, persistent, **kwargs)
        if self.stash is None or self.stash is NoStash:
            try:
                self._mounting = True
                self.mounts[cell].init()
            finally:
                self._mounting = False

    def add_link(self, link, path, persistent):
        paths = self.paths[link._root()]
        assert path not in paths, path
        #print("add link", path, link)
        paths.add(path)
        self.mounts[link] = LinkItem(link, path, persistent)
        if self.stash is None or self.stash is NoStash:
            self.mounts[link].init()

    def unmount(self, cell_or_link, from_del=False):
        #print("UNMOUNT", cell_or_link, cell_or_link._mount)
        assert not is_dummy_mount(cell_or_link._mount), cell_or_link
        root = cell_or_link._root()
        if from_del and (cell_or_link not in self.mounts or root not in self.paths):
            return
        paths = self.paths[root]
        path = cell_or_link._mount["path"]
        assert path in paths
        paths.remove(path)
        assert cell_or_link in self.mounts, (cell_or_link, path)  #... but path is in paths
        mountitem = self.mounts.pop(cell_or_link)
        mountitem.destroy()

    def unmount_context(self, context, from_del=False):
        #print("unmount context", context)
        self.contexts.discard(context) # may or may not exist, especially at __del__ time
        mount = context._mount
        """context._mount is authoritative!
        If context is destroyed while an unmount is undesired,
          (because of stash replacement)
        context._mount MUST have been set to None!
        """
        if context._root() is context:
            self.paths.pop(context, None)
            if mount is None:
                return
        assert not is_dummy_mount(mount), context
        try:
            paths = self.paths[context._root()]
        except KeyError:
            return
        try:
            paths.remove(mount["path"])
        except KeyError:
            pass
        if mount["persistent"] == False:
            dirpath = mount["path"].replace("/", os.sep)
            try:
                #print("rmdir", dirpath)
                os.rmdir(dirpath)
            except:
                print("Error: cannot remove directory %s" % dirpath)


    def add_context(self, context, path, as_parent):
        #print("add context", path, context, as_parent, context._mount["persistent"])
        paths = self.paths[context._root()]
        if not as_parent:
            assert path not in paths, path
            paths.add(path)
            self.contexts.add(context)
        else:
            if path in paths:
                assert context in self.contexts, (path, context)
        if self.stash is None or self.stash is NoStash:
            self._check_context(context, as_parent)
        else:
            self.stash.context_as_parent[context] = as_parent

    def _check_context(self, context, as_parent):
        mount = context._mount
        assert not is_dummy_mount(mount), context
        dirpath = mount["path"].replace("/", os.sep)
        persistent, authority = mount["persistent"], mount["authority"]
        if os.path.exists(dirpath):
            if authority == "cell" and not as_parent:
                print("Warning: Directory path '%s' already exists" % dirpath) #TODO: log warning
        else:
            if authority == "file-strict":
                raise Exception("Directory path '%s' does not exist, but authority is 'file-strict'" % dirpath)
            os.mkdir(dirpath)

    def add_cell_update(self, cell):
        #print("add_cell_update", cell, self.reorganizing, self.mounting)
        if self.reorganizing or self._mounting:
            return
        assert cell in self.mounts, (cell, hex(id(cell)))
        self.cell_updates.append(cell)

    def _run(self):
        for cell, mount_item in list(self.mounts.items()):
            if isinstance(cell, Link):
                continue
            if cell in self.cell_updates:
                continue
            try:
                mount_item.conditional_read()
            except Exception:
                exc = traceback.format_exc()
                if exc != mount_item.last_exc:
                    print(exc)
                    mount_item.last_exc = exc
        while 1:
            try:
                cell = self.cell_updates.popleft()
            except IndexError:
                break
            mount_item = self.mounts.get(cell)
            if mount_item is None: #cell was deleted
                continue
            try:
                mount_item.conditional_write(with_none=True)
            except Exception:
                exc = traceback.format_exc()
                if exc != mount_item.last_exc:
                    print(exc)
                    mount_item.last_exc = exc
        self._tick.set()

    def run(self):
        try:
            self._running = True
            while not self._stop:
                t = time.time()
                self._run()
                while time.time() - t < self.latency:
                    if not self._tick.is_set():
                        break
                    time.sleep(0.01)
        finally:
            self._running = False

    def start(self):
        self._stop = False
        t = self.thread = Thread(target=self.run)
        t.setDaemon(True)
        t.start()

    def stop(self, wait=False, waiting_loop_period=0.01):
        self._stop = True
        if wait:
            while self._running:
                time.sleep(waiting_loop_period)

    def tick(self):
        """Waits until one iteration of the run() loop has finished"""
        if self._running:
            self._tick.clear()
            self._tick.wait()

    def destroy(self):
        for path in list(self.mounts.keys()):
            self.unmount(path)
        for context in sorted(self.contexts,key=lambda l:-len(l.path)):
            self.unmount_context(context)

def resolve_register(reg):
    from .context import Context
    from .cell import Cell
    from . import Worker
    from .structured_cell import Inchannel, Outchannel
    contexts = set([r for r in reg if isinstance(r, Context)])
    cells = set([r for r in reg if isinstance(r, Cell)])
    links = set([r for r in reg if isinstance(r, Link)])
    mounts = mountmanager.mounts.copy()
    if sys.exc_info()[0] is not None:
        return #No mounting if there is an exception
    def find_mount(c, as_parent=False, child=None):
        if as_parent:
            assert child is not None
        if c in mounts:
            result = mounts[c]
        elif not is_dummy_mount(c._mount):
            result = c._mount.copy()
            if result["path"] is None:
                parent = c._context
                assert parent is not None, c
                parent = parent()
                parent_result = find_mount(parent, as_parent=True,child=c)
                if parent_result is None:
                    raise Exception("No path provided for mount of %s, but no ancestor context is mounted" % c)
                result["path"] = parent_result["path"]
                result["autopath"] = True
        elif isinstance(c, (Inchannel, Outchannel)):
            result = None
        elif isinstance(c, Context) and c._toplevel:
            result = None
        else:
            parent = c._context
            assert parent is not None, c
            parent = parent()
            result = None
            cc = c
            if isinstance(c, Link):
                cc = c.get_linked()
            if isinstance(cc, (Context, Cell)):
                result = find_mount(parent, as_parent=True,child=c)
        if not as_parent:
            mounts[c] = result
        if as_parent and result is not None:
            result = copy.deepcopy(result)
            if result["persistent"] is None:
                result["persistent"] = False
            result["autopath"] = True
            result["path"] += "/" + child.name
            if isinstance(child, Link):
                child = child.get_linked()
            extension = None
            if child._mount is not None:
                extension = child._mount.get("extension")
            if extension is not None:
                extension = "." + extension
            else:
                extension = get_extension(child)
            result["path"] += extension
        return result
    for r in reg:
        root = r._root()
        if root not in mountmanager.paths:
            mountmanager.paths[root] = set()
        if isinstance(r, Worker):
            continue
        find_mount(r)

    done_contexts = set()
    contexts_to_mount = {}
    def mount_context_delayed(context, as_parent=False):
        if not context in mounts or mounts[context] is None:
            return
        if context in done_contexts:
            if not as_parent:
                contexts_to_mount[context][1] = False
            return
        parent = context._context
        if parent is not None:
            parent = parent()
            mount_context_delayed(parent, as_parent=True)
        object.__setattr__(context, "_mount", mounts[context]) #not in macro mode
        contexts_to_mount[context] = [mounts[context]["path"], as_parent]
        done_contexts.add(context)

    for context in contexts:
        mount_context_delayed(context)

    def propagate_persistency(c, persistent=False):
        m = c._mount
        if is_dummy_mount(m):
            return
        if persistent:
            m["persistent"] = True
        elif m["persistent"] == True and m["autopath"]:
            persistent = True
        if isinstance(c, Context):
            if c._toplevel:
                return
        parent = c._context
        assert parent is not None, c
        parent = parent()
        propagate_persistency(parent, persistent)
    for r in reg:
        if isinstance(r, Worker):
            continue
        if not is_dummy_mount(r._mount):
            propagate_persistency(r)

    mount_cells = []
    for cell in cells:
        if cell in mounts and not is_dummy_mount(mounts[cell]):
            mount = mounts[cell]
            path = mount["path"]
            if cell._mount_kwargs is None:
                print("Warning: Unable to mount file path '%s': cannot mount this type of cell (%s)" % (path, type(cell).__name__))
                continue
            mount.update(cell._mount_kwargs)
            if cell._master and (cell._mount_setter is None or cell._master[1] in ("form", "storage")):
                if mount.get("mode") == "r":
                    continue
                else:
                    mount["mode"] = "w"
            object.__setattr__(cell, "_mount", mount) #not in macro mode
            mount_cells.append(cell)

    mount_links = []
    for link in links:
        if link in mounts and not is_dummy_mount(mounts[link]):
            mount = mounts[link]
            path = mount["path"]
            object.__setattr__(link, "_mount", mount) #not in macro mode
            mount_links.append(link)

    for context, v in contexts_to_mount.items():
        path, as_parent = v
        mountmanager.add_context(context, path, as_parent=as_parent)
    for cell in mount_cells:
        mountmanager.add_mount(cell, **cell._mount)
    for link in mount_links:
        mount = link._mount
        mountmanager.add_link(link, mount["path"], mount["persistent"])

mountmanager = MountManager(0.2) #TODO: latency in config cell
mountmanager.start()

def get_extension(c):
    from .cell import extensions
    for k,v in extensions.items():
        if type(c) == k:
            return v
    for k,v in extensions.items():
        if isinstance(c, k):
            return v
    return ""

from .link import Link
"""
*****
TODO: filehash option (cell stores hash of the file, necessary for slash-0)
*****
"""
