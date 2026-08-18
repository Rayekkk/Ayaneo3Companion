import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main

backup = "/tmp/aya3-controller-before.json"
action = sys.argv[1]
if action == "test":
    current = main.read_controller()
    with open(backup, "w", encoding="utf-8") as output:
        json.dump(current, output)
    test = dict(current, vibration="low", rgb_mode="solid", color="ff0000", brightness=100)
    main.apply_controller(test)
    print("before:", current)
    print("test:", main.read_controller())
elif action == "restore":
    previous = {"vibration": "high", "rgb_mode": "solid", "color": "6600ff", "brightness": 100}
    main.apply_controller(previous)
    print("restored:", main.read_controller())
else:
    raise SystemExit("test or restore required")
