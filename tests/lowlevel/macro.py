import seamless
from seamless.core import macro_mode_on
from seamless.core import context, cell, transformer, pytransformercell, macro

with macro_mode_on():
    ctx = context(toplevel=True)
    ctx.param = cell("json").set(1)

    ctx.macro = macro({
        "param": "copy",
    })

    ctx.param.connect(ctx.macro.param)
    ctx.macro_code = pytransformercell().set("""
ctx.sub = context(context=ctx,name="sub")
ctx.a = cell("json").set(1000 + param)
ctx.b = cell("json").set(2000 + param)
ctx.result = cell("json")
ctx.tf = transformer({
    "a": "input",
    "b": "input",
    "c": "output"
})
ctx.a.connect(ctx.tf.a)
ctx.b.connect(ctx.tf.b)
ctx.code = cell("pytransformer").set("c = a + b")
ctx.code.connect(ctx.tf.code)
ctx.tf.c.connect(ctx.result)
assert param != 999   # on purpose
if param > 1:
    ctx.d = cell("json").set(42)
    #raise Exception("on purpose") #causes the macro reconstruction to fail; comment it out to make it succeed
""")
    ctx.macro_code.connect(ctx.macro.code)

    ctx.mount("/tmp/mount-test", persistent=None)


print("START")
import time; time.sleep(0.5) #doing this instead of equilibrate() will cause the result update to be delayed until macro reconstruction
# if the macro reconstruction fails, the result update will still be accepted
### ctx.equilibrate()
print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "c"))
print(ctx.macro_gen_macro.result.value) #None instead of 3002, unless you enable ctx.equilibrate above

def mount_check():
    from seamless.core.mount import mountmanager #singleton
    for c in (ctx.macro_code, ctx.param, ctx.macro_gen_macro.a, ctx.macro_gen_macro.b, ctx.macro_gen_macro.code):
        path = c._mount["path"]
        assert c in mountmanager.mounts, c
        assert path in mountmanager.paths, (c, path)
        assert mountmanager.mounts[c].path == path, (c, path, mountmanager.mounts[c].path)

mount_check()

print("Change 1")
ctx.param.set(2)
ctx.equilibrate()
# Note that ctx.macro_gen_macro is now a new context, and
#   any old references to the old context are invalid
# But this is a concern for the high-level!

print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "d"))
if hasattr(ctx.macro_gen_macro, "d"):
    print(ctx.macro_gen_macro.d.value)
print(ctx.macro_gen_macro.result.value) #will never be None! 3002 if the reconstruction failed, 3004 if it succeeded

mount_check()

print("Change 2")
ctx.macro_code.set(
    ctx.macro_code.value + "   "
)
ctx.equilibrate()

mount_check()

print("Change 3")
ctx.macro_code.set(
    ctx.macro_code.value.replace("#raise Exception", "raise Exception")
)
ctx.equilibrate()
print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "d"))
if hasattr(ctx.macro_gen_macro, "d"):
    print(ctx.macro_gen_macro.d.value)
print(ctx.macro_gen_macro.result.value) #will never be None! 3002 if the reconstruction failed, 3004 if it succeeded

mount_check()

print("Change 4")
ctx.macro_code.set(
    ctx.macro_code.value.replace("raise Exception", "#raise Exception")
)
ctx.equilibrate()
print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "d"))
if hasattr(ctx.macro_gen_macro, "d"):
    print(ctx.macro_gen_macro.d.value)
print(ctx.macro_gen_macro.result.value) #will never be None! 3002 if the reconstruction failed, 3004 if it succeeded

print("Change 5")
ctx.macro_code.set(
    ctx.macro_code.value.replace("raise Exception", "#raise Exception")
)
ctx.param.set(0)
ctx.equilibrate()
print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "d"))
if hasattr(ctx.macro_gen_macro, "d"):
    print(ctx.macro_gen_macro.d.value)
print(ctx.macro_gen_macro.result.value) #will never be None! 3002 if the reconstruction failed, 3004 if it succeeded

mount_check()

print("Change 6")
ctx.param.set(999)
print(ctx.macro_gen_macro.a.value)
print(ctx.macro_gen_macro.b.value)
print(hasattr(ctx.macro_gen_macro, "d"))
if hasattr(ctx.macro_gen_macro, "d"):
    print(ctx.macro_gen_macro.d.value)
print(ctx.macro_gen_macro.result.value) #will never be None! 3002 if the reconstruction failed, 3004 if it succeeded

shell = ctx.macro.shell()
shell2 = ctx.macro_gen_macro.tf.shell()

mount_check()

print("STOP")
#import sys; sys.exit()
