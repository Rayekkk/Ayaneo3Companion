import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import controller_backend

backup = "/tmp/aya3-controller-before.json"
action = sys.argv[1]
if action == "test":
    current = controller_backend.read_controller()
    with open(backup, "w", encoding="utf-8") as output:
        json.dump(current, output)
    test = dict(current, vibration="low", rgb_mode="solid", color="ff0000", brightness=100)
    controller_backend.apply_controller(test)
    print("before:", current)
    print("test:", controller_backend.read_controller())
elif action == "restore":
    previous = {"vibration": "high", "rgb_mode": "solid", "color": "6600ff", "brightness": 100}
    controller_backend.apply_controller(previous)
    print("restored:", controller_backend.read_controller())
else:
    raise SystemExit("test or restore required")
