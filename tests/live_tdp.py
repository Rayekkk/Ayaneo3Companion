import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main

low = {"spl": 8, "sppt": 10, "fppt": 12}
restore = {"spl": 15, "sppt": 18, "fppt": 25}
try:
    main.apply_tdp(low)
    print("applied low:", low, "gpu power:", main.gpu_power_watts())
    time.sleep(2)
    print("gpu power after 2s:", main.gpu_power_watts())
finally:
    main.apply_tdp(restore)
    print("restored:", restore)
