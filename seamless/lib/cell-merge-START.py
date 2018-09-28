from seamless import subprocess
import tempfile, os
from seamless.subprocess import CalledProcessError, PIPE
tokens = "<|>"
labels0 = "UPSTREAM", "BASE", "MODIFIED"

class SeparatorInTextError(Exception):
    pass

def build_labels(upstream, base, modified):
    n = ""
    while 1:
        try:
            for token in tokens:
                tokstr = 7 * token + " "
                for label in labels0:
                    tokstr2 = tokstr + label + str(n)
                    for text in upstream, base, modified:
                        if text is None:
                            continue
                        if text.find(tokstr2) > -1:
                            raise SeparatorInTextError
        except SeparatorInTextError:
            if n == "":
                n = 0
            n += 1
            continue
        break
    return tuple([l+str(n) for l in labels0])

try:
    subprocess.run("diff3 --help", shell=True, check=True,stdout=PIPE,stderr=PIPE)
    has_diff3 = True
except subprocess.CalledProcessError:
    has_diff3 = False
    print("ERROR: need diff3 command line tool")

upstream, base, modified = None, None, None
if PINS.conflict.defined and len(PINS.conflict.value.strip()):
    mode = "conflict"
    upstream = PINS.upstream_stage.value
    base = PINS.base.value
    modified = PINS.modified.value
    labels = build_labels(upstream, base, modified)
elif not PINS.modified.defined or PINS.modified.value == PINS.base.value:
    mode = "passthrough"
else:
    mode = "modify"

if mode != "conflict":
    PINS.conflict.set(None)
fallback = PINS.fallback.value
