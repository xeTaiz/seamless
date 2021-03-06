# Generate three-dimensional points (even though we will ignore Z)
assert N > 0
import numpy as np
values = np.random.normal(0.0, 0.2, (N, 3)).astype(np.float32)

return values
