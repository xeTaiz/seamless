Seamless: a cell-based reactive programming framework
Seamless was created on June 3rd, 2016

The eight seamless constructs (sorted from good to ugly):
1. context
2. cell
3. transformer
4. pin
5. reactor
6. macro
7. registrar
8. observer (takes a Python function pointer; never serializable!)

Seamless Zen

Watch the state as it is now, don't watch the news.  
All state is in cells, unless it is local.  
Always be prepared to rebuild from cells.  
Cells are good, files are bad.  
Importing external libraries is good, importing project code is bad.  
Namespaces are good, classes less so.  
GUIs are good, unless they hide the state.  
Edit all state, then edit the editor.  
Code execution is repeatable; if it is not repeatable, you throw away the code, and keep its result.  
Transform lazily, react eagerly.  

TODO:

Technically-oriented releases are marked with *

*0.1
- Make a sane status report system (missing inputs, undefined inputs, missing connections, also in children)
- Python registrar
- Eighth construct: observer
- Integrate protein viewer in browser
- Demos: plotly, OpenGL (code DONE), orca (code DONE), slash+protein  

0.2
- Replace ctx.CHILDREN, ctx.CELLS etc. with ctx.self.children, ctx.self.cells, etc.
- Get rid of seamless.qt
- Composite (JSON) cells
- Expand seamless shell language (slash)
- Logging + dtype/worker documentation.resource system (using composite cells)
- Error message logging system (using composite cells)
- Overhaul dtypes, docson/type registration API, integrate with logging/documentation system
- Update demos

*0.3
- Multiple code cells in transformers/reactors
- Preliminary outputpins (in transformers [as secondary output] and in reactors)
- Preliminary inputpins (pins that accept preliminary values). Right now, all inputpins are preliminary!
- Address shell() memory leak: IPython references may hold onto large amounts of data
- Binary (struct) cells, active switches (connection level; workers don't see it, except that pin becomes undefined/changes value)
- Silk: managing variable-length arrays with allocators (subclass ndarray), C header registrar, fix Bool default value bug
- C interop
- Game of Life demo with Cython and C
- Update OpenGL demo

0.4
- Finalize context graph format and their names, update tofile/fromfile accordingly
- Finalize resource management
- Finalize basic API, also how to change macros
- Cleanup code layout
- Code documentation + dtype/worker documentation system
- Set up user library directory and robogit
- Update demos

*0.5
- Thread reactors, process reactors
- Synchronous transformers (do we need this?)
- Process transformers (now that execution is in a process, do we need this??)

*0.6
- Array cells, channels
- GPU computing (OpenCL)
- Update Game of Life demo

0.7
- Hook API and GUI for cell creation
- Update demos

0.8
- ATC, fold/unfold switches, Silk GUI generation, Silk mvcc hooked up with error message hook API
- More demos (tetris?)

*0.9
- Python debugging, code editor (WIP)

*0.10
- Collaborative protocol / delegated computing

*1.0
- Lazy evaluation, GPU-GPU triggering
